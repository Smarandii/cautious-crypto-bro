"""Recovery behaviour for the SignalService.

These paths only run on startup or after a crash, which is exactly why
they rot: nothing in normal traffic reaches them. They decide whether a
pending AUTO order is executed on restart or failed closed, so they are
worth pinning even though the branches are narrow.
"""

import asyncio
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from decimal import Decimal

import pytest

from cautious_crypto_bro.domain import (
    AutoApprovalMode,
    IntentExecutionOutcome,
    IntentStatus,
    PositionActionExecutionOutcome,
)
from cautious_crypto_bro.service import (
    SignalService,
)


class ServiceStore:
    """Default PnL surface so every fake survives the post-execution preview."""

    async def get_account_pnl_sync_state(self):
        return None

    async def upsert_closed_pnl(self, records):
        return None

    async def mark_account_pnl_synced(self, *, history_start_at, last_synced_at):
        return None

    async def get_account_pnl_summary(self):
        return None


class RecoveryStore(ServiceStore):
    def __init__(
        self,
        intent_ids=(),
        action_ids=(),
    ) -> None:
        self.intent_ids = list(intent_ids)
        self.action_ids = list(action_ids)
        self.failed_intents = []
        self.failed_actions = []

    async def get_pending_auto_intent_ids(self):
        return list(self.intent_ids)

    async def get_pending_auto_action_ids(self):
        return list(self.action_ids)

    async def mark_failed(self, intent_id, message):
        self.failed_intents.append((intent_id, message))

    async def mark_position_action_failed(self, action_id, message):
        self.failed_actions.append((action_id, message))


class RecordingCoordinator:
    def __init__(self, *, intent_error=None, action_error=None) -> None:
        self.intents = []
        self.actions = []
        self.intent_error = intent_error
        self.action_error = action_error

    async def execute_intent(self, intent_id, *, approval_mode):
        self.intents.append((intent_id, approval_mode))

        if self.intent_error is not None:
            raise self.intent_error

        return IntentExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="done",
        )

    async def execute_position_action(self, action_id, *, approval_mode):
        self.actions.append((action_id, approval_mode))

        if self.action_error is not None:
            raise self.action_error

        return PositionActionExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="done",
        )


class NullExecutor:
    async def account_state(self):
        return None


class ExplodingExecutor:
    async def account_state(self):
        raise RuntimeError("exchange unreachable")


class RecordingBot:
    def __init__(self) -> None:
        self.snapshots = 0
        self.intent_outcomes = []
        self.action_outcomes = []
        self.snapshot_error = False

    async def send_account_snapshot(self, state, *, state_error, pnl, pnl_error):
        self.snapshots += 1

        if self.snapshot_error:
            raise RuntimeError("telegram unreachable")

    async def send_auto_intent_outcome(self, outcome):
        self.intent_outcomes.append(outcome)

    async def send_auto_action_outcome(self, outcome):
        self.action_outcomes.append(outcome)


def build_service(
    *,
    mode,
    store,
    coordinator,
    executor=None,
    approval_bot=None,
    lease=300,
):
    bot = approval_bot or RecordingBot()

    service = SignalService(
        store=store,
        extractor=None,
        planner=None,
        executor=executor or NullExecutor(),
        approval_bot=bot,
        coordinator=coordinator,
        context_provider=None,
        auto_approval_mode=mode,
        source_processing_lease_seconds=lease,
    )

    return service, bot


def test_constructor_rejects_non_positive_lease() -> None:
    for lease in (0, -1):
        with pytest.raises(ValueError, match="lease must be positive"):
            SignalService(
                store=None,
                extractor=None,
                planner=None,
                executor=None,
                approval_bot=None,
                coordinator=None,
                context_provider=None,
                source_processing_lease_seconds=lease,
            )


def test_auto_recovery_fails_everything_when_disabled() -> None:
    """Disabling AUTO must fail pending work, never silently execute it."""

    async def run() -> None:
        store = RecoveryStore(
            intent_ids=["intent-1", "intent-2"],
            action_ids=["action-1"],
        )
        coordinator = RecordingCoordinator()

        service, _ = build_service(
            mode=AutoApprovalMode.DISABLED,
            store=store,
            coordinator=coordinator,
        )

        await service.recover_auto_execution()

        assert coordinator.intents == []
        assert coordinator.actions == []
        assert [i for i, _ in store.failed_intents] == ["intent-1", "intent-2"]
        assert [a for a, _ in store.failed_actions] == ["action-1"]

        for _, message in store.failed_intents + store.failed_actions:
            assert message == "AUTO recovery cancelled: auto approval is disabled"

    asyncio.run(run())


def test_auto_recovery_open_only_executes_entries_and_fails_actions() -> None:
    async def run() -> None:
        store = RecoveryStore(
            intent_ids=["intent-1"],
            action_ids=["action-1"],
        )
        coordinator = RecordingCoordinator()

        service, bot = build_service(
            mode=AutoApprovalMode.OPEN_ONLY,
            store=store,
            coordinator=coordinator,
        )

        await service.recover_auto_execution()

        assert coordinator.intents == [("intent-1", "AUTO")]
        assert coordinator.actions == []
        assert store.failed_intents == []
        assert store.failed_actions == [
            (
                "action-1",
                "AUTO recovery cancelled: current mode does not allow position actions",
            )
        ]

        # The account preview is fetched after execution, not before.
        assert bot.snapshots == 1
        assert len(bot.intent_outcomes) == 1

    asyncio.run(run())


def test_auto_recovery_all_executes_intents_and_actions() -> None:
    async def run() -> None:
        store = RecoveryStore(
            intent_ids=["intent-1"],
            action_ids=["action-1"],
        )
        coordinator = RecordingCoordinator()

        service, bot = build_service(
            mode=AutoApprovalMode.ALL,
            store=store,
            coordinator=coordinator,
        )

        await service.recover_auto_execution()

        assert coordinator.intents == [("intent-1", "AUTO")]
        assert coordinator.actions == [("action-1", "AUTO")]
        assert store.failed_intents == []
        assert store.failed_actions == []
        assert len(bot.intent_outcomes) == 1
        assert len(bot.action_outcomes) == 1

    asyncio.run(run())


def test_auto_recovery_survives_a_failing_outcome_delivery() -> None:
    """A dead Telegram must not abandon the rest of the recovery queue."""

    async def run() -> None:
        store = RecoveryStore(
            intent_ids=["intent-1"],
            action_ids=["action-1"],
        )
        coordinator = RecordingCoordinator()

        bot = RecordingBot()
        bot.snapshot_error = True

        service, _ = build_service(
            mode=AutoApprovalMode.ALL,
            store=store,
            coordinator=coordinator,
            approval_bot=bot,
        )

        await service.recover_auto_execution()

        assert coordinator.intents == [("intent-1", "AUTO")]
        assert coordinator.actions == [("action-1", "AUTO")]
        assert store.failed_intents == []
        assert store.failed_actions == []

    asyncio.run(run())


def test_auto_recovery_reports_unreachable_exchange_without_aborting() -> None:
    """Execution happened; only the preview failed, and that is not fatal."""

    async def run() -> None:
        store = RecoveryStore(intent_ids=["intent-1"])
        coordinator = RecordingCoordinator()

        service, bot = build_service(
            mode=AutoApprovalMode.OPEN_ONLY,
            store=store,
            coordinator=coordinator,
            executor=ExplodingExecutor(),
        )

        await service.recover_auto_execution()

        assert coordinator.intents == [("intent-1", "AUTO")]
        assert bot.snapshots == 1
        assert len(bot.intent_outcomes) == 1

    asyncio.run(run())


def test_auto_recovery_with_empty_queue_touches_nothing() -> None:
    async def run() -> None:
        store = RecoveryStore()
        coordinator = RecordingCoordinator()

        service, bot = build_service(
            mode=AutoApprovalMode.ALL,
            store=store,
            coordinator=coordinator,
        )

        await service.recover_auto_execution()

        assert coordinator.intents == []
        assert coordinator.actions == []
        assert store.failed_intents == []
        assert store.failed_actions == []
        assert bot.snapshots == 0

    asyncio.run(run())


class PnlStore(ServiceStore):
    def __init__(self, state, *, history=()) -> None:
        self.state = state
        self.history = list(history)
        self.upserts = []
        self.marked = []

    async def get_account_pnl_sync_state(self):
        return self.state

    async def upsert_closed_pnl(self, records):
        self.upserts.append(list(records))

    async def mark_account_pnl_synced(self, *, history_start_at, last_synced_at):
        self.marked.append((history_start_at, last_synced_at))

    async def get_account_pnl_summary(self):
        return "summary"


class HistoryExecutor:
    def __init__(self) -> None:
        self.calls = []

    async def closed_pnl_history(self, start, end):
        self.calls.append((start, end))
        return [("fill", Decimal("1"))]


class SyncState:
    def __init__(self, history_start_at, last_synced_at) -> None:
        self.history_start_at = history_start_at
        self.last_synced_at = last_synced_at


def test_first_pnl_sync_backfills_seven_days() -> None:
    """With no sync state, history starts a week back."""

    async def run() -> None:
        store = PnlStore(state=None)
        executor = HistoryExecutor()

        service, _ = build_service(
            mode=AutoApprovalMode.DISABLED,
            store=store,
            coordinator=RecordingCoordinator(),
            executor=executor,
        )

        before = datetime.now(UTC)
        assert await service._sync_account_pnl() == "summary"

        assert len(executor.calls) == 1
        start, end = executor.calls[0]
        assert before - start >= timedelta(days=7, seconds=-5)
        assert start < end

        history_start, last_synced = store.marked[0]
        assert history_start <= before
        assert last_synced >= before

    asyncio.run(run())


def test_subsequent_pnl_sync_replays_from_last_sync_minus_a_day() -> None:
    async def run() -> None:
        history_start = datetime(2026, 1, 1, tzinfo=UTC)
        last_synced = datetime(2026, 6, 1, tzinfo=UTC)

        store = PnlStore(state=SyncState(history_start, last_synced))
        executor = HistoryExecutor()

        service, _ = build_service(
            mode=AutoApprovalMode.DISABLED,
            store=store,
            coordinator=RecordingCoordinator(),
            executor=executor,
        )

        await service._sync_account_pnl()

        start, _ = executor.calls[0]

        # Replays from one day before the previous sync, and never moves the
        # history boundary forward.
        assert start == last_synced - timedelta(days=1)
        assert store.marked[0][0] == history_start

    asyncio.run(run())
