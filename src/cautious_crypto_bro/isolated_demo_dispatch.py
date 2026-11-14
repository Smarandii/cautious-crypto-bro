"""Dispatch an isolated OPEN using its durable intake assignment."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from .domain import (
    ApprovalMode,
    ExecutionPlan,
    IntentExecutionOutcome,
    IntentStatus,
    PositionActionExecutionOutcome,
    PositionActionIntent,
    StoreError,
    TradeExecutionError,
    TradingIntent,
)
from .execution import ExecutionPlanner
from .execution_coordinator import ExecutionCoordinator
from .isolated_demo_risk import IsolatedDemoPortfolio
from .isolated_lifecycle import find_lifecycle_targets
from .ports import AccountGateway
from .storage import SQLiteStore
from .storage.isolated_intake import DemoIntentAssignment, IsolatedDemoIntake

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DemoWorkerSelection:
    assignments: tuple[DemoIntentAssignment, ...]
    unresolved_intent_ids: tuple[UUID, ...]


class IsolatedDemoDispatcher:
    def __init__(
        self,
        intake: IsolatedDemoIntake,
        workers: dict[int, tuple[AccountGateway, SQLiteStore]],
        portfolio: IsolatedDemoPortfolio,
        planner: ExecutionPlanner,
        max_age_seconds: int,
        *,
        worker_ready: Callable[[int], bool] | None = None,
    ) -> None:
        self._intake = intake
        self._worker_ready = worker_ready
        self._accounts = {uid: executor for uid, (executor, _) in workers.items()}
        self._workers = {
            uid: (
                store,
                ExecutionCoordinator(
                    store=store,
                    executor=executor,
                    planner=planner,
                    max_age_seconds=max_age_seconds,
                    isolated_demo_portfolio=portfolio,
                    worker_uid=uid,
                ),
            )
            for uid, (executor, store) in workers.items()
        }

    async def select_workers(
        self, plans: Sequence[tuple[TradingIntent, ExecutionPlan]]
    ) -> DemoWorkerSelection:
        """Select accounts for eligible manual plans before transactional reservation."""
        if not plans:
            return DemoWorkerSelection((), ())

        occupied_by_worker: dict[int, set[str]] = {}
        for uid, executor in sorted(self._accounts.items()):
            if self._worker_ready is not None and not self._worker_ready(uid):
                continue
            try:
                account = await executor.account_state()
            except TradeExecutionError:
                logger.warning("Demo worker account state unavailable during selection")
                continue
            occupied = {position.symbol for position in account.positions}
            occupied.update(order.symbol for order in account.open_orders)
            occupied.update(
                symbol for _, symbol in await self._intake.reserved_symbols(uid)
            )
            if self._worker_ready is None or self._worker_ready(uid):
                occupied_by_worker[uid] = occupied

        assignments = []
        unresolved = []
        for intent, plan in plans:
            for uid, occupied in occupied_by_worker.items():
                if intent.symbol not in occupied:
                    assignments.append(DemoIntentAssignment(uid, intent, plan))
                    occupied.add(intent.symbol)
                    break
            else:
                unresolved.append(intent.intent_id)
        return DemoWorkerSelection(tuple(assignments), tuple(unresolved))

    async def lifecycle_targets(
        self, action: PositionActionIntent
    ) -> tuple[tuple[int, UUID], ...]:
        if self._worker_ready is not None and not all(
            self._worker_ready(uid) for uid in self._workers
        ):
            raise StoreError("Demo lifecycle ownership is unavailable")
        return await find_lifecycle_targets(
            action, {uid: store for uid, (store, _) in self._workers.items()}
        )

    async def _import(
        self, intent_id: UUID
    ) -> tuple[SQLiteStore, ExecutionCoordinator]:
        assignment = await self._intake.get_assignment(intent_id)
        if assignment is None:
            raise StoreError("Isolated Demo intent has no account assignment")
        worker = self._workers.get(assignment.uid)
        if worker is None or (
            self._worker_ready is not None and not self._worker_ready(assignment.uid)
        ):
            raise StoreError("Assigned Demo worker is unavailable")
        store, _ = worker
        await store.import_isolated_intent(assignment)
        return worker

    async def execute_intent(
        self,
        intent_id: UUID,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None = None,
    ) -> IntentExecutionOutcome:
        try:
            _, coordinator = await self._import(intent_id)
        except StoreError as exc:
            return IntentExecutionOutcome(status=IntentStatus.FAILED, message=str(exc))
        return await coordinator.execute_intent(
            intent_id, approval_mode=approval_mode, user_id=user_id
        )

    async def mark_skipped(self, intent_id: UUID, user_id: int) -> bool:
        store, _ = await self._import(intent_id)
        skipped = await store.mark_skipped(intent_id, user_id)
        current = await store.get_intent(intent_id)
        if current is not None and current.status is IntentStatus.SKIPPED:
            await self._intake.release(intent_id)
        return skipped

    async def _import_action(
        self, action: PositionActionIntent
    ) -> tuple[SQLiteStore, ExecutionCoordinator]:
        uid = action.target_demo_uid
        if uid is None:
            raise StoreError("Position action has no Demo account target")
        worker = self._workers.get(uid)
        if worker is None or (
            self._worker_ready is not None and not self._worker_ready(uid)
        ):
            raise StoreError("Assigned Demo worker is unavailable")
        store, _ = worker
        await store.import_isolated_position_action(uid, action)
        return worker

    async def execute_position_action(
        self, action: PositionActionIntent, user_id: int
    ) -> PositionActionExecutionOutcome:
        try:
            _, coordinator = await self._import_action(action)
        except StoreError as exc:
            return PositionActionExecutionOutcome(
                status=IntentStatus.FAILED, message=str(exc)
            )
        return await coordinator.execute_position_action(
            action.action_id, approval_mode=ApprovalMode.MANUAL, user_id=user_id
        )

    async def skip_position_action(
        self, action: PositionActionIntent, user_id: int
    ) -> bool:
        store, _ = await self._import_action(action)
        return await store.mark_position_action_skipped(action.action_id, user_id)
