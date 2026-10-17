"""Cancellation must not abandon accepted exchange work or release ownership early."""

import asyncio
import threading

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


class DelayedExchange(Exchange):
    def __init__(self, path, *, response_loss=False):
        super().__init__()
        self.path = path
        self.response_loss = response_loss
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def handle(self, request):
        response = super().handle(request)
        if request.url.path == self.path and not self.started.is_set():
            self.started.set()
            try:
                if not self.release.wait(5):
                    raise RuntimeError("Offline exchange probe timed out")
                if self.response_loss:
                    raise httpx.ReadTimeout(
                        "Accepted request; response lost", request=request
                    )
            finally:
                self.finished.set()
        return response


async def assert_cancellation_waits(task, exchange, lock):
    assert await asyncio.to_thread(exchange.started.wait, 2)
    for _ in range(2):
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.03)
        assert not done, "Cancellation escaped before exchange work finished"
        assert lock.locked(), "Account lock released while exchange work was running"
    assert not exchange.finished.is_set()


@pytest.mark.parametrize("response_loss", [False, True])
def test_canceled_entry_commits_known_outcome_before_releasing_lock(
    tmp_path, response_loss
):
    async def run():
        exchange = DelayedExchange("/v5/order/create", response_loss=response_loss)
        with executor_for(exchange) as executor:
            database = tmp_path / "state.db"
            store, intent, _, coordinator, _ = await prepare(
                database, exchange, executor
            )
            lock = coordinator._execution_lock
            task = asyncio.create_task(
                coordinator.execute_intent(
                    intent.intent_id, approval_mode=ApprovalMode.MANUAL
                )
            )
            observer = None
            try:
                await assert_cancellation_waits(task, exchange, lock)

                async def next_lock_owner():
                    async with lock:
                        saved = await IntentStore(database).get_intent(intent.intent_id)
                        state = (await store.get_active_position_strategies())[0][0]
                        return saved.status, state.status

                observer = asyncio.create_task(next_lock_owner())
                await asyncio.sleep(0)
                assert not observer.done()
                exchange.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                observed_intent, observed_strategy = await observer
                assert observed_intent is (
                    IntentStatus.FAILED if response_loss else IntentStatus.EXECUTED
                )
                assert observed_strategy is (
                    StrategyStatus.UNCERTAIN
                    if response_loss
                    else StrategyStatus.ENTERING
                )
                assert exchange.stop == 90 and exchange.size > 0
                assert len(exchange.open_entries()) == (0 if response_loss else 2)
                before = len(exchange.requests)
                await coordinator.execute_intent(
                    intent.intent_id, approval_mode=ApprovalMode.MANUAL
                )
                assert len(exchange.requests) == before, "Terminal work was replayed"
            finally:
                exchange.release.set()
                await asyncio.gather(task, return_exceptions=True)
                if observer is not None:
                    await asyncio.gather(observer, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["cancel", "cancel_response_loss", "read"])
def test_gateway_cancellation_waits_for_its_worker(operation, caplog):
    async def run():
        exchange = DelayedExchange(
            "/v5/position/list" if operation == "read" else "/v5/order/cancel",
            response_loss=operation == "cancel_response_loss",
        )
        exchange.create(
            dict(
                symbol="BTCUSDT",
                side="Buy",
                qty="1",
                price="100",
                orderLinkId="offline-entry",
                orderType="Limit",
            )
        )
        with executor_for(exchange) as executor:
            lock = asyncio.Lock()

            async def operation_under_lock():
                async with lock:
                    if operation == "read":
                        await executor.account_state()
                    else:
                        await executor.cancel_order("BTCUSDT", "id-1")

            task = asyncio.create_task(operation_under_lock())
            try:
                await assert_cancellation_waits(task, exchange, lock)
                exchange.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert exchange.finished.is_set() and not lock.locked()
                if operation != "read":
                    assert not exchange.open_entries()
                if operation == "cancel_response_loss":
                    assert (
                        "Operation failed while deferring caller cancellation"
                        in caplog.text
                    )
            finally:
                exchange.release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("kind", "response_loss"),
    [
        (PositionActionType.REDUCE, False),
        (PositionActionType.REDUCE, True),
        (PositionActionType.CANCEL_ENTRIES, False),
    ],
)
def test_canceled_position_action_finishes_durable_transition(
    tmp_path, kind, response_loss
):
    async def run():
        exchange = DelayedExchange("/disabled", response_loss=response_loss)
        with executor_for(exchange) as executor:
            database = tmp_path / "state.db"
            store, intent, _, coordinator, _ = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            starting_size = exchange.size
            action = PositionActionIntent(
                source=source(message_id=2),
                symbol="BTCUSDT",
                action=kind,
                expected_side=Side.LONG,
                close_pct=50 if kind is PositionActionType.REDUCE else None,
                summary="Offline cancellation regression",
                confidence=1,
            )
            claim = await store.claim_source(action.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source((), (action,), claim)
            exchange.path = (
                "/v5/order/cancel"
                if kind is PositionActionType.CANCEL_ENTRIES
                else "/v5/order/create"
            )
            lock = coordinator._execution_lock
            task = asyncio.create_task(
                coordinator.execute_position_action(
                    action.action_id, approval_mode=ApprovalMode.MANUAL
                )
            )
            try:
                await assert_cancellation_waits(task, exchange, lock)
                exchange.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                saved = await IntentStore(database).get_position_action(
                    action.action_id
                )
                state = (await store.get_active_position_strategies())[0][0]
                assert saved.status is (
                    IntentStatus.UNCERTAIN if response_loss else IntentStatus.EXECUTED
                )
                if response_loss:
                    assert state.status is StrategyStatus.UNCERTAIN
                else:
                    assert state.rebalance_needed
                assert exchange.stop == 90
                if kind is PositionActionType.CANCEL_ENTRIES:
                    assert (
                        exchange.size == starting_size and not exchange.open_entries()
                    )
                else:
                    assert 0 < exchange.size < starting_size
                before = len(exchange.requests)
                await coordinator.execute_position_action(
                    action.action_id, approval_mode=ApprovalMode.MANUAL
                )
                assert len(exchange.requests) == before
            finally:
                exchange.release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
