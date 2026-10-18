"""Action age and durable ownership are checked at the mutation boundary."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, source, state_for

from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
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
