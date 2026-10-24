"""Malformed batch acknowledgements must not leave owned scale-in orders live."""

import asyncio
import json

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    IntentStatus,
    StrategyStatus,
)
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("entry_type", [EntryType.MARKET, EntryType.LIMIT])
@pytest.mark.parametrize(
    "fault",
    [
        "null-result-list",
        "null-extension",
        "null-status-list",
        "nonobject-result",
        "nonobject-status",
        "missing-code",
        "invalid-json",
        "nonobject-envelope",
        "missing-retcode",
        "null-result",
        "read-timeout",
        "http-error",
        "duplicate-order-id",
        "nonstring-order-id",
        "blank-order-id",
    ],
)
def test_malformed_batch_cancels_submitted_entries_without_replay(
    tmp_path, fault, entry_type
):
    class MalformedBatchExchange(Exchange):
        submissions = 0
        rollback_links = ()
        rollbacks = 0

        def handle(self, request):
            if request.url.path == "/v5/order/cancel-batch":
                self.rollbacks += 1
                body = json.loads(request.content)
                self.rollback_links = tuple(
                    item["orderLinkId"] for item in body["request"]
                )
                for link in self.rollback_links:
                    self.orders[link]["orderStatus"] = "Cancelled"
                return httpx.Response(
                    200,
                    json={
                        "retCode": 0,
                        "result": {},
                        "retExtInfo": {
                            "list": [{"code": 0} for _ in self.rollback_links]
                        },
                    },
                )
            response = super().handle(request)
            if request.url.path == "/v5/order/create-batch":
                self.submissions += 1
                data = response.json()
                if fault == "invalid-json":
                    return httpx.Response(200, text="invalid-json")
                if fault == "nonobject-envelope":
                    return httpx.Response(200, json=[])
                if fault == "read-timeout":
                    raise httpx.ReadTimeout("synthetic response lost", request=request)
                if fault == "http-error":
                    return httpx.Response(503, text="synthetic unavailable")
                if fault == "missing-retcode":
                    del data["retCode"]
                elif fault == "null-result":
                    data["result"] = None
                elif fault == "null-result-list":
                    data["result"]["list"] = None
                elif fault == "null-extension":
                    data["retExtInfo"] = None
                elif fault == "null-status-list":
                    data["retExtInfo"]["list"] = None
                elif fault == "nonobject-result":
                    data["result"]["list"][0] = None
                elif fault == "nonobject-status":
                    data["retExtInfo"]["list"][0] = None
                elif fault == "duplicate-order-id":
                    data["result"]["list"][1]["orderId"] = data["result"]["list"][0][
                        "orderId"
                    ]
                elif fault == "nonstring-order-id":
                    data["result"]["list"][0]["orderId"] = {"invalid": "id"}
                elif fault == "blank-order-id":
                    data["result"]["list"][0]["orderId"] = "   "
                else:
                    data["retExtInfo"]["list"][0] = {}
                return httpx.Response(200, json=data)
            return response

    async def run():
        database = tmp_path / "state.db"
        exchange = MalformedBatchExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, _ = await prepare(
                database,
                exchange,
                executor,
                entry=Entry(type=entry_type, price=100)
                if entry_type is EntryType.LIMIT
                else None,
            )
            foreign = exchange.create(
                {
                    "symbol": "ETHUSDT",
                    "orderLinkId": "manual-entry",
                    "side": "Buy",
                    "orderType": "Limit",
                    "qty": "1",
                    "price": "80",
                }
            )["result"]["orderId"]
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.FAILED
            owned = [
                o
                for o in exchange.orders.values()
                if o["orderLinkId"].startswith("ccb-v2-") and o["orderType"] == "Limit"
            ]
            assert owned and all(o["orderStatus"] == "Cancelled" for o in owned)
            assert set(exchange.rollback_links) == {o["orderLinkId"] for o in owned}
            assert exchange.rollbacks == 1
            assert exchange.orders["manual-entry"]["orderId"] == foreign
            assert exchange.orders["manual-entry"]["orderStatus"] == "New"
            if entry_type is EntryType.MARKET:
                assert exchange.size > 0 and exchange.stop == plan.stop_loss
            else:
                assert exchange.size == 0
            reopened = IntentStore(database)
            assert (await reopened.get_active_position_strategies())[0][
                0
            ].status is StrategyStatus.UNCERTAIN
            before = exchange.submissions
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert exchange.submissions == before == 1

    asyncio.run(run())
