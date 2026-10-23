"""Execution claims and action age are checked at the mutation boundary."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from test_exchange_cancellation import DelayedExchange
from test_remaining_concerns import (
    Exchange,
    executor_for,
    make_intent,
    prepare,
    source,
    state_for,
)

from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    Side,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class ObservedLock(asyncio.Lock):
    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self):
        self.waiting.set()
        return await super().acquire()


@pytest.mark.parametrize("mode", [ApprovalMode.MANUAL, ApprovalMode.AUTO])
def test_queued_different_symbol_entries_share_portfolio_capacity(tmp_path, mode):
    async def run():
        database = tmp_path / "state.db"
        exchange = DelayedExchange("/v5/order/create")
        with executor_for(exchange) as executor:
            store, first, plan, _, _ = await prepare(
                database, exchange, executor, mode=mode, relation=OpenRelation.NEW
            )
            second = make_intent(mode=mode).model_copy(
                update={
                    "symbol": "ETHUSDT",
                    "source": source(message_id=2),
                    "relation": OpenRelation.NEW,
                }
            )
            second_plan = plan.model_copy(
                update={
                    "intent_id": second.intent_id,
                    "symbol": second.symbol,
                }
            )
            claim = await store.claim_source(second.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source(
                ((second, second_plan),), (), claim
            )
            lock = ObservedLock()
            coordinator = ExecutionCoordinator(
                store=store,
                executor=executor,
                planner=ExecutionPlanner(),
                max_age_seconds=3600,
                execution_lock=lock,
                portfolio_stop_risk_cap_usdt=plan.planned_max_loss_usdt
                * Decimal("1.5"),
            )
            first_task = asyncio.create_task(
                coordinator.execute_intent(first.intent_id, approval_mode=mode)
            )
            second_task = None
            try:
                assert await asyncio.to_thread(exchange.started.wait, 3)
                lock.waiting.clear()
                second_task = asyncio.create_task(
                    coordinator.execute_intent(second.intent_id, approval_mode=mode)
                )
                await asyncio.wait_for(lock.waiting.wait(), 3)
                assert not second_task.done()
                assert (
                    await store.get_intent(second.intent_id)
                ).status is IntentStatus.PENDING
                exchange.release.set()
                first_result, second_result = await asyncio.wait_for(
                    asyncio.gather(first_task, second_task), 10
                )
                assert first_result.status is IntentStatus.EXECUTED
                assert second_result.status is IntentStatus.FAILED
                assert "Portfolio stop-risk cap exceeded" in second_result.message
                assert {order["symbol"] for order in exchange.orders.values()} == {
                    "BTCUSDT"
                }
                assert len(exchange.orders) == 3
                reopened = IntentStore(database)
                assert (
                    await reopened.get_intent(second.intent_id)
                ).status is IntentStatus.FAILED
                active = await reopened.get_active_position_strategies()
                assert len(active) == 1 and active[0][0].symbol == "BTCUSDT"
                before = len(exchange.requests)
                await coordinator.execute_intent(second.intent_id, approval_mode=mode)
                assert len(exchange.requests) == before
            finally:
                exchange.release.set()
                await asyncio.gather(
                    first_task,
                    *([second_task] if second_task else []),
                    return_exceptions=True,
                )

    asyncio.run(run())


@pytest.mark.parametrize("mode", [ApprovalMode.MANUAL, ApprovalMode.AUTO])
def test_cancelled_entry_waiter_remains_pending_and_executes_once_after_restart(
    tmp_path, mode
):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, _, _ = await prepare(
                database, exchange, executor, mode=mode, relation=OpenRelation.NEW
            )
            lock = ObservedLock()
            coordinator = ExecutionCoordinator(
                store=store,
                executor=executor,
                planner=ExecutionPlanner(),
                max_age_seconds=3600,
                execution_lock=lock,
            )
            await lock.acquire()
            lock.waiting.clear()
            task = asyncio.create_task(
                coordinator.execute_intent(intent.intent_id, approval_mode=mode)
            )
            try:
                await asyncio.wait_for(lock.waiting.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                reopened = IntentStore(database)
                saved = await reopened.get_intent(intent.intent_id)
                assert saved.status is IntentStatus.PENDING
                assert not exchange.requests and not exchange.orders
                assert await reopened.get_active_position_strategies() == ()
                assert await reopened.quarantine_interrupted_executions() == ((), ())

                # Retry through a new coordinator as on restart; it must wait too.
                restarted = ExecutionCoordinator(
                    store=reopened,
                    executor=executor,
                    planner=ExecutionPlanner(),
                    max_age_seconds=3600,
                    execution_lock=lock,
                )
                lock.waiting.clear()
                task = asyncio.create_task(
                    restarted.execute_intent(intent.intent_id, approval_mode=mode)
                )
                await asyncio.wait_for(lock.waiting.wait(), timeout=5)
                assert not exchange.requests
                lock.release()
                outcome = await asyncio.wait_for(task, timeout=10)
                assert outcome.status is IntentStatus.EXECUTED
                assert len(exchange.orders) == 3
                saved = await reopened.get_intent(intent.intent_id)
                assert saved.status is IntentStatus.EXECUTED
                before = len(exchange.requests)
                await restarted.execute_intent(intent.intent_id, approval_mode=mode)
                assert len(exchange.requests) == before
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if lock.locked():
                    lock.release()

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["expired", "fresh", "cancelled"])
def test_action_waiting_for_mutation_lock_obeys_age_and_cancellation(
    tmp_path, scenario
):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, entry_coordinator, _ = await prepare(
                database, exchange, executor
            )
            outcome = await entry_coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
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
            lock = ObservedLock()
            coordinator = ExecutionCoordinator(
                store=store,
                executor=executor,
                planner=ExecutionPlanner(),
                max_age_seconds=900,
                execution_lock=lock,
            )
            exchange.requests.clear()
            action_time = min(action.created_at, action.source.published_at)
            await lock.acquire()
            lock.waiting.clear()
            task = None
            try:
                with patch(
                    "cautious_crypto_bro.execution_coordinator.datetime"
                ) as clock:
                    clock.now.return_value = action_time + timedelta(seconds=1)
                    task = asyncio.create_task(
                        coordinator.execute_position_action(
                            action.action_id, approval_mode=ApprovalMode.MANUAL
                        )
                    )
                    await asyncio.wait_for(lock.waiting.wait(), timeout=5)
                    assert not exchange.requests
                    if scenario == "cancelled":
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                        expected = IntentStatus.PENDING
                    else:
                        clock.now.return_value = action_time + timedelta(
                            seconds=901 if scenario == "expired" else 899
                        )
                        lock.release()
                        outcome = await asyncio.wait_for(task, timeout=10)
                        expected = (
                            IntentStatus.FAILED
                            if scenario == "expired"
                            else IntentStatus.EXECUTED
                        )
                        assert outcome.status is expected
                        if scenario == "expired":
                            assert outcome.message == "Position action is stale (901s)"
                    saved = await IntentStore(database).get_position_action(
                        action.action_id
                    )
                    assert saved.status is expected
                    if scenario == "fresh":
                        assert exchange.size == Decimal("0")
                    else:
                        assert not exchange.requests
                        assert await state_for(store) == before
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if lock.locked():
                    lock.release()

    asyncio.run(run())
