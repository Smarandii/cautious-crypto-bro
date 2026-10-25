"""Lifecycle actions wait for exit cancellation through real SQLite and HTTP."""

import asyncio
import json
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, source

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
@pytest.mark.parametrize("settles", [False, True])
def test_action_waits_for_owned_exit_cancellation(tmp_path, kind, settles):
    class DelayedExchange(Exchange):
        def __init__(self):
            super().__init__()
            self.pending = set()
            self.confirmation_reads = 0
            self.action_before_cancellation = False

        def handle(self, request):
            if request.url.path == "/v5/order/cancel":
                body = json.loads(request.content)
                order = next(
                    o
                    for o in self.orders.values()
                    if o["orderId"] == body.get("orderId")
                    or o["orderLinkId"] == body.get("orderLinkId")
                )
                if order["reduceOnly"]:
                    self.requests.append((request.url.path, body))
                    self.pending.add(order["orderId"])
                    return httpx.Response(
                        200,
                        json={"retCode": 0, "result": {"orderId": order["orderId"]}},
                    )
            if self.pending and request.url.path == "/v5/order/realtime":
                self.confirmation_reads += 1
                if settles and self.confirmation_reads >= 2:
                    for order in self.orders.values():
                        if order["orderId"] in self.pending:
                            order["orderStatus"] = "Cancelled"
                    self.pending.clear()
            if request.url.path == "/v5/order/create":
                body = json.loads(request.content)
                if body["orderLinkId"].startswith("ccb-action-"):
                    self.action_before_cancellation = bool(self.pending)
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = DelayedExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await supervisor.reconcile_once()
            initial_size = exchange.size
            assert len(exchange.exits()) == 3
            exchange.create(
                {
                    "symbol": "BTCUSDT",
                    "orderLinkId": "manual-exit",
                    "side": "Sell",
                    "orderType": "Limit",
                    "qty": "0.1",
                    "price": "160",
                    "reduceOnly": True,
                }
            )
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
            before = len(exchange.requests)
            with patch("cautious_crypto_bro.bybit.executor.time.sleep"):
                result = await coordinator.execute_position_action(
                    action.action_id, approval_mode=ApprovalMode.MANUAL
                )
            expected = IntentStatus.EXECUTED if settles else IntentStatus.UNCERTAIN
            assert result.status is expected
            assert not exchange.action_before_cancellation
            reopened = IntentStore(database)
            assert (
                await reopened.get_position_action(action.action_id)
            ).status is expected
            if settles:
                assert exchange.size == (
                    initial_size * Decimal("0.75")
                    if kind is PositionActionType.REDUCE
                    else 0
                )
            else:
                assert exchange.size == initial_size
                assert (await reopened.get_active_position_strategies())[0][
                    0
                ].status is StrategyStatus.UNCERTAIN
                assert not any(
                    path == "/v5/order/create" for path, _ in exchange.requests[before:]
                )
                assert exchange.stop == plan.stop_loss
            assert exchange.orders["manual-exit"]["orderStatus"] == "New"
            before = len(exchange.requests)
            repeated = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert repeated.status is expected and not exchange.requests[before:]

    asyncio.run(run())
