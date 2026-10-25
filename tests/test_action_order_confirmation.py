"""An external position change cannot substitute for our action order's fill."""

import asyncio
import sqlite3
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, source, state_for

from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
    StrategyStatus,
)
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", [PositionActionType.REDUCE, PositionActionType.CLOSE])
@pytest.mark.parametrize(
    "status",
    [
        "Filled",
        "New",
        "PartiallyFilled",
        "Cancelled",
        "CancelledWithFill",
        "DelayedFill",
    ],
)
def test_position_target_alone_cannot_confirm_action(tmp_path, kind, status):
    class ExternalReductionExchange(Exchange):
        action_order = None
        action_reads = 0

        def create(self, body):
            result = super().create(body)
            if body["orderLinkId"].startswith("ccb-action-"):
                self.action_order = self.orders[body["orderLinkId"]]
                if status != "Filled":
                    # The overall position reaches the target, but part/all
                    # of that change came from another exchange actor.
                    quantity = Decimal(self.action_order["qty"])
                    executed = (
                        quantity / 2
                        if status in {"PartiallyFilled", "CancelledWithFill"}
                        else 0
                    )
                    self.action_order.update(
                        orderStatus={
                            "CancelledWithFill": "Cancelled",
                            "DelayedFill": "New",
                        }.get(status, status),
                        cumExecQty=str(executed),
                        leavesQty=str(quantity - executed),
                        avgPrice=str(self.avg if executed else 0),
                    )
                    if status == "DelayedFill":
                        # This control has no external reduction; our fill arrives later.
                        self.size += quantity
            return result

        def handle(self, request):
            if (
                status == "DelayedFill"
                and self.action_order is not None
                and request.url.path == "/v5/order/realtime"
                and request.url.params.get("orderLinkId")
                == self.action_order["orderLinkId"]
            ):
                self.action_reads += 1
                if self.action_reads == 3:
                    self.fill(self.action_order["orderLinkId"])
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = ExternalReductionExchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await supervisor.reconcile_once()
            action = PositionActionIntent(
                source=source(message_id=2),
                symbol="BTCUSDT",
                action=kind,
                expected_side=Side.LONG,
                close_pct=25 if kind is PositionActionType.REDUCE else None,
                summary="Lifecycle action",
                confidence=1,
            )
            claim = await store.claim_source(action.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source((), (action,), claim)
            with patch(
                "cautious_crypto_bro.execution_coordinator.asyncio.sleep",
                new=AsyncMock(),
            ):
                result = await coordinator.execute_position_action(
                    action.action_id, approval_mode=ApprovalMode.MANUAL
                )
            expected = (
                IntentStatus.EXECUTED
                if status in {"Filled", "CancelledWithFill", "DelayedFill"}
                else IntentStatus.UNCERTAIN
            )
            assert result.status is expected
            assert result.result is not None
            assert result.result.order_id == exchange.action_order["orderId"]
            reopened = IntentStore(database)
            with sqlite3.connect(database) as db:
                saved = db.execute(
                    "SELECT status,bybit_order_id FROM position_actions WHERE action_id=?",
                    (str(action.action_id),),
                ).fetchone()
            assert saved == (expected.value, exchange.action_order["orderId"])
            if expected is IntentStatus.UNCERTAIN:
                assert (await state_for(reopened)).status is StrategyStatus.UNCERTAIN
            before = len(exchange.requests)
            repeated = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert repeated.status is expected and not exchange.requests[before:]

    asyncio.run(run())
