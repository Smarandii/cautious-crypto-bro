"""Confirm terminal E1 fills before submitting scale-ins, through SQLite and HTTP."""

import asyncio
from decimal import Decimal
from unittest.mock import patch

import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro.domain import ApprovalMode, IntentStatus
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("status", ["Filled", "Cancelled", "PartiallyFilled"])
def test_market_scale_ins_wait_for_terminal_primary_fill(tmp_path, status):
    class PrimaryExchange(Exchange):
        primary = None
        primary_reads = 0
        terminal_seen = False
        batches = 0

        def create(self, body):
            result = super().create(body)
            if body["orderType"] == "Market" and not body.get("reduceOnly"):
                self.primary = self.orders[body["orderLinkId"]]
                if status != "Filled":
                    half = Decimal(self.primary["qty"]) / 2
                    self.size -= half
                    self.primary["cumExecQty"] = str(half)
                    self.primary["leavesQty"] = str(
                        half if status == "Cancelled" else 0
                    )
                    self.primary["orderStatus"] = status
            return result

        def handle(self, request):
            if (
                request.url.path == "/v5/order/realtime"
                and request.url.params.get("orderId")
                and self.primary is not None
            ):
                self.primary_reads += 1
                if status == "PartiallyFilled" and self.primary_reads == 2:
                    remaining = Decimal(self.primary["qty"]) - Decimal(
                        self.primary["cumExecQty"]
                    )
                    self.primary["leavesQty"] = str(remaining)
                    self.fill(self.primary["orderLinkId"])
                self.terminal_seen = self.primary["orderStatus"] in {
                    "Filled",
                    "Cancelled",
                }
            if request.url.path == "/v5/order/create-batch":
                self.batches += 1
                assert self.terminal_seen, "Scale-ins submitted before E1 was terminal"
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = PrimaryExchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, _ = await prepare(
                database, exchange, executor
            )
            with patch("cautious_crypto_bro.bybit.executor.time.sleep"):
                outcome = await coordinator.execute_intent(
                    intent.intent_id, approval_mode=ApprovalMode.MANUAL
                )
            assert outcome.status is IntentStatus.EXECUTED
            assert exchange.batches == 1
            assert exchange.primary_reads == (2 if status == "PartiallyFilled" else 1)
            assert exchange.size == Decimal(exchange.primary["cumExecQty"])
            assert outcome.plan.orders[0].quantity == exchange.size
            assert exchange.stop == outcome.plan.stop_loss
            before = len(exchange.requests)
            repeated = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert repeated.status is IntentStatus.EXECUTED
            assert not exchange.requests[before:]
            assert (
                await IntentStore(database).get_intent(intent.intent_id)
            ).status is IntentStatus.EXECUTED

    asyncio.run(run())
