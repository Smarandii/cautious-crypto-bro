"""Read failures cannot quarantine strategies before action mutations begin."""

import asyncio

import aiosqlite
import httpx
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
@pytest.mark.parametrize("expected_side", [None, Side.LONG])
def test_action_does_not_follow_position_side_change_during_cancellation(
    tmp_path, kind, expected_side
):
    class ReversedExchange(Exchange):
        reverse_on_cancel = False

        def handle(self, request):
            if self.reverse_on_cancel and request.url.path == "/v5/order/cancel":
                self.reverse_on_cancel = False
                self.side = Side.SHORT
                self.stop = 110
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = ReversedExchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await supervisor.reconcile_once()
            initial_size = exchange.size
            action = PositionActionIntent(
                source=source(message_id=2),
                symbol="BTCUSDT",
                action=kind,
                expected_side=expected_side,
                close_pct=25 if kind is PositionActionType.REDUCE else None,
                summary="Lifecycle action",
                confidence=1,
            )
            claim = await store.claim_source(action.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source((), (action,), claim)
            exchange.reverse_on_cancel = True
            before = len(exchange.requests)
            result = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert result.status is IntentStatus.UNCERTAIN
            assert "side" in result.message.casefold()
            assert exchange.side is Side.SHORT and exchange.size == initial_size
            assert exchange.stop == 110
            assert not any(
                path == "/v5/order/create" for path, _ in exchange.requests[before:]
            )
            reopened = IntentStore(database)
            assert (
                await reopened.get_position_action(action.action_id)
            ).status is IntentStatus.UNCERTAIN
            assert (await state_for(reopened)).status is StrategyStatus.UNCERTAIN
            before = len(exchange.requests)
            repeated = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert repeated.status is IntentStatus.UNCERTAIN
            assert not exchange.requests[before:]

    asyncio.run(run())


class ActionReadFailureExchange(Exchange):
    fault = None

    def handle(self, request):
        if self.fault and request.url.path == "/v5/position/list":
            if self.fault == "quantity":
                return httpx.Response(
                    200,
                    json={
                        "retCode": 0,
                        "result": {"list": [self.position() | {"size": None}]},
                    },
                )
            if self.fault == "json":
                return httpx.Response(200, text="not-json")
            if self.fault == "read-timeout":
                raise httpx.ReadTimeout("synthetic preflight read timeout")
            if self.fault == "late-read" and any(
                path == "/v5/order/cancel" for path, _ in self.requests
            ):
                raise httpx.ReadTimeout("synthetic read timeout after cancellation")
        if self.fault == "cancel-timeout" and request.url.path == "/v5/order/cancel":
            # The request may have reached the exchange: retain quarantine.
            raise httpx.ReadTimeout("synthetic cancellation response loss")
        return super().handle(request)


@pytest.mark.parametrize(
    "fault", ["quantity", "json", "read-timeout", "cancel-timeout", "late-read"]
)
def test_action_failure_classification_preserves_strategy_until_mutation(
    tmp_path, fault
):
    async def run():
        database = tmp_path / "state.db"
        exchange = ActionReadFailureExchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, _ = await prepare(
                database, exchange, executor
            )
            result = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert result.status is IntentStatus.EXECUTED
            before = await state_for(store)
            action = PositionActionIntent(
                source=source(message_id=2),
                symbol="BTCUSDT",
                action=PositionActionType.CLOSE,
                expected_side=Side.LONG,
                summary="Close position",
                confidence=1,
            )
            claim = await store.claim_source(action.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source((), (action,), claim)
            exchange.requests.clear()
            exchange.fault = fault
            outcome = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            expected = (
                IntentStatus.UNCERTAIN
                if fault in {"cancel-timeout", "late-read"}
                else IntentStatus.FAILED
            )
            assert outcome.status is expected
            # Reopen storage: prove the durable action and strategy transition.
            reopened = IntentStore(database)
            saved = await reopened.get_position_action(action.action_id)
            assert saved.status is expected
            async with aiosqlite.connect(database) as db:
                row = await (
                    await db.execute(
                        "SELECT bybit_order_id FROM position_actions WHERE action_id = ?",
                        (str(action.action_id),),
                    )
                ).fetchone()
            assert row == (None,)
            after = await state_for(reopened)
            if fault in {"cancel-timeout", "late-read"}:
                assert after.status is StrategyStatus.UNCERTAIN
            else:
                assert after == before
                assert "PositionActionPreflightError" in outcome.message
                assert not any(
                    path
                    in {
                        "/v5/order/cancel",
                        "/v5/order/create",
                        "/v5/order/create-batch",
                    }
                    for path, _ in exchange.requests
                )

    asyncio.run(run())
