"""Malformed batch acknowledgements must not leave owned scale-in orders live."""

import asyncio
import json

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro.domain import ApprovalMode, IntentStatus, StrategyStatus
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "fault",
    [
        "null-result-list",
        "null-extension",
        "null-status-list",
        "nonobject-result",
        "nonobject-status",
        "missing-code",
    ],
)
def test_malformed_batch_cancels_submitted_entries_without_replay(tmp_path, fault):
    class MalformedBatchExchange(Exchange):
        submissions = 0
        rollback_links = ()

        def handle(self, request):
            if request.url.path == "/v5/order/cancel-batch":
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
                if fault == "null-result-list":
                    data["result"]["list"] = None
                elif fault == "null-extension":
                    data["retExtInfo"] = None
                elif fault == "null-status-list":
                    data["retExtInfo"]["list"] = None
                elif fault == "nonobject-result":
                    data["result"]["list"][0] = None
                elif fault == "nonobject-status":
                    data["retExtInfo"]["list"][0] = None
                else:
                    data["retExtInfo"]["list"][0] = {}
                return httpx.Response(200, json=data)
            return response

    async def run():
        database = tmp_path / "state.db"
        exchange = MalformedBatchExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, _ = await prepare(
                database, exchange, executor
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
            assert exchange.orders["manual-entry"]["orderId"] == foreign
            assert exchange.orders["manual-entry"]["orderStatus"] == "New"
            assert exchange.size > 0 and exchange.stop == plan.stop_loss
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
