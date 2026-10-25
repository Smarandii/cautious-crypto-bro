"""Accepted exchange actions must survive final SQLite completion failures."""

import asyncio
import sqlite3

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
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", [PositionActionType.REDUCE, PositionActionType.CLOSE])
@pytest.mark.parametrize("failed_write", ["action", "strategy"])
def test_confirmed_action_commit_failure_is_durable_and_never_replayed(
    tmp_path, kind, failed_write
):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await supervisor.reconcile_once()
            previous = await state_for(store)
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
            table, condition = (
                ("position_actions", "NEW.status = 'EXECUTED'")
                if failed_write == "action"
                else (
                    "position_strategies",
                    "NEW.status = 'CLOSING' OR NEW.rebalance_needed = 1",
                )
            )
            with sqlite3.connect(database) as db:
                db.execute(
                    f"CREATE TRIGGER abort_action_completion BEFORE UPDATE ON {table} "
                    f"WHEN {condition} BEGIN "
                    "SELECT RAISE(ABORT, 'synthetic completion failure'); END"
                )
            outcome = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.UNCERTAIN
            assert "synthetic completion failure" in outcome.message
            assert outcome.result is not None
            order = exchange.orders[f"ccb-action-{action.action_id.hex[:24]}"]
            assert order["orderStatus"] == "Filled"
            assert outcome.result.order_id == order["orderId"]
            reopened = IntentStore(database)
            assert (
                await reopened.get_position_action(action.action_id)
            ).status is IntentStatus.UNCERTAIN
            with sqlite3.connect(database) as db:
                saved = db.execute(
                    "SELECT bybit_order_id FROM position_actions WHERE action_id = ?",
                    (str(action.action_id),),
                ).fetchone()
            assert saved == (order["orderId"],)
            state = await state_for(reopened)
            assert state.status is StrategyStatus.UNCERTAIN
            assert state.entry_frozen == previous.entry_frozen
            assert state.rebalance_needed == previous.rebalance_needed
            attention = await reopened.get_operator_attention(limit=10, offset=0)
            assert any(
                item.kind == "action" and item.record_id == action.action_id
                for item in attention
            )
            before = len(exchange.requests)
            lock = asyncio.Lock()
            restarted = ExecutionCoordinator(
                store=reopened,
                executor=executor,
                planner=ExecutionPlanner(),
                max_age_seconds=3600,
                execution_lock=lock,
            )
            repeated = await restarted.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert repeated.status is IntentStatus.UNCERTAIN
            await PositionSupervisor(
                store=reopened, executor=executor, mutation_lock=lock
            ).reconcile_once()
            assert not exchange.requests[before:]
            assert (
                sum(
                    path == "/v5/order/create"
                    and body.get("orderLinkId") == order["orderLinkId"]
                    for path, body in exchange.requests
                )
                == 1
            )

    asyncio.run(run())
