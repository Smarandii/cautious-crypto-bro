from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from .bybit import (
    BybitDemoExecutor,
    PositionActionExecutionResult,
)
from .domain import (
    ApprovalMode,
    ExecutionPlan,
    IntentStatus,
    PositionActionIntent,
    TradingIntent,
)
from .storage import IntentStore

logger = logging.getLogger(__name__)


@dataclass(
    frozen=True,
    slots=True,
)
class IntentExecutionOutcome:
    status: IntentStatus
    message: str
    intent: TradingIntent | None = None
    plan: ExecutionPlan | None = None
    order_ids: tuple[str, ...] = ()


@dataclass(
    frozen=True,
    slots=True,
)
class PositionActionExecutionOutcome:
    status: IntentStatus
    message: str
    action: PositionActionIntent | None = None
    result: PositionActionExecutionResult | None = None


class ExecutionCoordinator:
    def __init__(
        self,
        *,
        store: IntentStore,
        executor: BybitDemoExecutor,
        max_age_seconds: int,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")

        self._store = store
        self._executor = executor
        self._max_age_seconds = max_age_seconds

    async def execute_intent(
        self,
        intent_id: UUID,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None = None,
    ) -> IntentExecutionOutcome:
        intent = await self._store.get_intent(intent_id)

        if intent is None:
            return IntentExecutionOutcome(
                status=IntentStatus.FAILED,
                message="Intent no longer exists",
            )

        plan = await self._store.get_execution_plan(intent_id)

        if plan is None:
            return IntentExecutionOutcome(
                status=IntentStatus.FAILED,
                message=("Execution plan no longer exists"),
                intent=intent,
            )

        if intent.status is not IntentStatus.PENDING:
            return IntentExecutionOutcome(
                status=intent.status,
                message=("Intent was already handled"),
                intent=intent,
                plan=plan,
            )

        age = (datetime.now(UTC) - intent.created_at).total_seconds()

        if age > self._max_age_seconds:
            claimed = await self._store.claim_for_execution(
                intent_id,
                user_id,
                expected_approval_mode=(approval_mode),
            )

            message = f"Intent is stale ({int(age)}s)"

            if claimed:
                await self._store.mark_failed(
                    intent_id,
                    message,
                )

            return IntentExecutionOutcome(
                status=IntentStatus.FAILED,
                message=message,
                intent=intent,
                plan=plan,
            )

        claimed = await self._store.claim_for_execution(
            intent_id,
            user_id,
            expected_approval_mode=(approval_mode),
        )

        if not claimed:
            current = await self._store.get_intent(intent_id)

            return IntentExecutionOutcome(
                status=(current.status if current is not None else IntentStatus.FAILED),
                message=("Intent was already handled"),
                intent=current or intent,
                plan=plan,
            )

        try:
            order_ids = await self._executor.execute(plan)
        except Exception as exc:
            logger.exception(
                "Execution failed for %s",
                intent_id,
            )

            message = f"{type(exc).__name__}: {exc}"

            await self._store.mark_failed(
                intent_id,
                message,
            )

            return IntentExecutionOutcome(
                status=IntentStatus.FAILED,
                message=message,
                intent=intent,
                plan=plan,
            )

        await self._store.mark_executed(
            intent_id,
            order_ids,
        )

        return IntentExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="Executed on Bybit Demo",
            intent=intent,
            plan=plan,
            order_ids=order_ids,
        )

    async def execute_position_action(
        self,
        action_id: UUID,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None = None,
    ) -> PositionActionExecutionOutcome:
        action = await self._store.get_position_action(action_id)

        if action is None:
            return PositionActionExecutionOutcome(
                status=IntentStatus.FAILED,
                message=("Position action no longer exists"),
            )

        if action.status is not IntentStatus.PENDING:
            return PositionActionExecutionOutcome(
                status=action.status,
                message=("Position action was already handled"),
                action=action,
            )

        age = (datetime.now(UTC) - action.created_at).total_seconds()

        if age > self._max_age_seconds:
            claimed = await self._store.claim_position_action_for_execution(
                action_id,
                user_id,
                expected_approval_mode=(approval_mode),
            )

            message = f"Position action is stale ({int(age)}s)"

            if claimed:
                await self._store.mark_position_action_failed(
                    action_id,
                    message,
                )

            return PositionActionExecutionOutcome(
                status=IntentStatus.FAILED,
                message=message,
                action=action,
            )

        claimed = await self._store.claim_position_action_for_execution(
            action_id,
            user_id,
            expected_approval_mode=(approval_mode),
        )

        if not claimed:
            current = await self._store.get_position_action(action_id)

            return PositionActionExecutionOutcome(
                status=(current.status if current is not None else IntentStatus.FAILED),
                message=("Position action was already handled"),
                action=current or action,
            )

        try:
            result = await self._executor.execute_position_action(action)
        except Exception as exc:
            logger.exception(
                "Position action execution failed for %s",
                action_id,
            )

            message = f"{type(exc).__name__}: {exc}"

            await self._store.mark_position_action_failed(
                action_id,
                message,
            )

            return PositionActionExecutionOutcome(
                status=IntentStatus.FAILED,
                message=message,
                action=action,
            )

        await self._store.mark_position_action_executed(
            action_id,
            result.order_id,
        )

        return PositionActionExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="Executed on Bybit Demo",
            action=action,
            result=result,
        )
