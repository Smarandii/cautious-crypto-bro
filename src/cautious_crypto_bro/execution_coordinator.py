from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from .bybit import (
    AccountStateSummary,
    BybitDemoExecutor,
    PositionActionExecutionResult,
)
from .domain import (
    ApprovalMode,
    ExecutionPlan,
    IntentStatus,
    OpenRelation,
    PositionActionIntent,
    TradingIntent,
)
from .storage import IntentStore

logger = logging.getLogger(__name__)


class AutoExecutionSafetyError(RuntimeError):
    pass


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

        # Serialize account mutations. This closes the
        # race where two concurrent NEW signals could
        # both inspect an empty account and then execute.
        self._execution_lock = asyncio.Lock()

    @staticmethod
    def auto_open_safety_reason(
        intent: TradingIntent,
        account_state: AccountStateSummary,
    ) -> str | None:
        if intent.relation not in {
            OpenRelation.NEW,
            OpenRelation.ADD_OR_REENTRY,
        }:
            return (
                "OPEN relation is not eligible "
                "for automatic execution: "
                f"{intent.relation.value}"
            )

        positions = tuple(
            position
            for position in account_state.positions
            if position.symbol == intent.symbol
        )

        entry_orders = tuple(
            order
            for order in account_state.open_orders
            if (
                order.symbol == intent.symbol
                and order.remaining_quantity > 0
                and not order.reduce_only
                and not order.close_on_trigger
                and not order.is_protective
            )
        )

        if len(positions) > 1:
            return f"Multiple live positions exist for {intent.symbol}"

        existing_sides = {
            item.side
            for item in (
                *positions,
                *entry_orders,
            )
        }

        if intent.relation is OpenRelation.NEW:
            if positions or entry_orders:
                return (
                    "NEW signal conflicts with "
                    "existing account exposure "
                    f"for {intent.symbol}"
                )

            return None

        # ADD_OR_REENTRY is the only relation that
        # authorizes adding to existing exposure.
        if any(side is not intent.side for side in existing_sides):
            return (
                "ADD_OR_REENTRY conflicts with "
                "opposite-side account exposure "
                f"for {intent.symbol}"
            )

        return None

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
                message=("Intent was already handled or approval mode changed"),
                intent=current or intent,
                plan=plan,
            )

        try:
            async with self._execution_lock:
                if approval_mode is ApprovalMode.AUTO:
                    live_state = await self._executor.account_state()

                    safety_reason = self.auto_open_safety_reason(
                        intent,
                        live_state,
                    )

                    if safety_reason is not None:
                        raise (AutoExecutionSafetyError(safety_reason))

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
                message=(
                    "Position action was already handled or approval mode changed"
                ),
                action=current or action,
            )

        try:
            async with self._execution_lock:
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
