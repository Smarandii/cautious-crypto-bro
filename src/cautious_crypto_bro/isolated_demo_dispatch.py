"""Dispatch an isolated OPEN using its durable intake assignment."""

from uuid import UUID

from .domain import ApprovalMode, IntentExecutionOutcome, IntentStatus, StoreError
from .execution import ExecutionPlanner
from .execution_coordinator import ExecutionCoordinator
from .isolated_demo_risk import IsolatedDemoPortfolio
from .ports import AccountGateway
from .storage import SQLiteStore
from .storage.isolated_intake import IsolatedDemoIntake


class IsolatedDemoDispatcher:
    def __init__(
        self,
        intake: IsolatedDemoIntake,
        workers: dict[int, tuple[AccountGateway, SQLiteStore]],
        portfolio: IsolatedDemoPortfolio,
        planner: ExecutionPlanner,
        max_age_seconds: int,
    ) -> None:
        self._intake = intake
        self._workers = {
            uid: (
                store,
                ExecutionCoordinator(
                    store=store,
                    executor=executor,
                    planner=planner,
                    max_age_seconds=max_age_seconds,
                    isolated_demo_portfolio=portfolio,
                ),
            )
            for uid, (executor, store) in workers.items()
        }

    async def _import(
        self, intent_id: UUID
    ) -> tuple[SQLiteStore, ExecutionCoordinator]:
        assignment = await self._intake.get_assignment(intent_id)
        if assignment is None:
            raise StoreError("Isolated Demo intent has no account assignment")
        worker = self._workers.get(assignment.uid)
        if worker is None:
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
        return await store.mark_skipped(intent_id, user_id)
