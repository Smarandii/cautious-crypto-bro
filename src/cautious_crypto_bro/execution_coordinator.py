from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from .bybit import (
    AccountStateSummary,
    BybitDemoExecutor,
    PositionActionExecutionResult,
)
from .domain import (
    ApprovalMode,
    ExecutionOrderType,
    ExecutionPlan,
    IntentStatus,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    StrategyStatus,
    TradingIntent,
)
from .execution import ExecutionPlanner
from .storage import IntentStore

logger = logging.getLogger(__name__)


class AutoExecutionSafetyError(RuntimeError):
    pass


class PositionActionConfirmationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IntentExecutionOutcome:
    status: IntentStatus
    message: str
    intent: TradingIntent | None = None
    plan: ExecutionPlan | None = None
    order_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
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
        execution_lock: asyncio.Lock | None = None,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")

        self._store = store
        self._executor = executor
        self._max_age_seconds = max_age_seconds

        # Serialize every account mutation, including
        # supervisor reconciliation.
        self._execution_lock = (
            execution_lock if execution_lock is not None else asyncio.Lock()
        )

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

    async def _confirm_position_action(
        self,
        action: PositionActionIntent,
        result: PositionActionExecutionResult,
    ) -> None:
        expected_remaining: Decimal | None = None

        if action.action is PositionActionType.REDUCE:
            submitted = result.submitted_quantity

            if submitted is None:
                raise PositionActionConfirmationError(
                    "REDUCE returned no submitted quantity"
                )

            expected_remaining = result.position_size_before - submitted

            if expected_remaining <= 0:
                raise PositionActionConfirmationError(
                    "REDUCE confirmation geometry is invalid"
                )

        for attempt in range(20):
            state = await self._executor.account_state()

            positions = tuple(
                position
                for position in state.positions
                if (position.symbol == action.symbol)
            )

            if action.action is PositionActionType.CLOSE:
                if not positions:
                    return

                if len(positions) != 1:
                    raise PositionActionConfirmationError(
                        "CLOSE confirmation found multiple live positions"
                    )

                position = positions[0]

                if position.side is not result.position_side:
                    raise PositionActionConfirmationError(
                        "CLOSE confirmation found opposite-side exposure"
                    )

            else:
                assert expected_remaining is not None

                if not positions:
                    raise PositionActionConfirmationError(
                        "REDUCE unexpectedly closed the full position"
                    )

                if len(positions) != 1:
                    raise PositionActionConfirmationError(
                        "REDUCE confirmation found multiple live positions"
                    )

                position = positions[0]

                if position.side is not result.position_side:
                    raise PositionActionConfirmationError(
                        "REDUCE confirmation found opposite-side exposure"
                    )

                if position.size <= expected_remaining:
                    return

            if attempt < 19:
                await asyncio.sleep(0.25)

        if action.action is PositionActionType.CLOSE:
            detail = "position is still open"
        else:
            detail = "position quantity did not reach the submitted REDUCE target"

        raise PositionActionConfirmationError(
            f"Bybit accepted position action {result.order_id}, but {detail}"
        )

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

        effective_plan = plan

        try:
            async with self._execution_lock:
                if approval_mode is ApprovalMode.AUTO:
                    live_state = await self._executor.account_state()

                    safety_reason = self.auto_open_safety_reason(
                        intent,
                        live_state,
                    )

                    if safety_reason is not None:
                        raise AutoExecutionSafetyError(safety_reason)

                if plan.strategy_version >= 2:
                    await self._store.ensure_position_strategy(plan)

                staged_market = (
                    plan.strategy_version >= 2
                    and plan.orders[0].order_type is ExecutionOrderType.MARKET
                )

                if staged_market:
                    primary = await self._executor.execute_market_primary(plan)

                    context = await self._executor.market_context(plan.symbol)

                    effective_plan = ExecutionPlanner().rebase_market_plan(
                        plan,
                        fill_price=(primary.average_fill_price),
                        filled_quantity=(primary.filled_quantity),
                        context=context,
                    )

                    # Persist the fill-derived geometry
                    # before E2/E3 are submitted. The
                    # supervisor shares this mutation lock,
                    # so it can never observe the new orders
                    # against the old snapshot plan.
                    await self._store.update_execution_plan(effective_plan)

                    remaining_ids = await self._executor.execute_remaining_entries(
                        effective_plan
                    )

                    order_ids = (
                        primary.order_id,
                        *remaining_ids,
                    )

                else:
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

            if plan.strategy_version >= 2:
                await self._store.set_position_strategy_status(
                    intent_id,
                    StrategyStatus.UNCERTAIN,
                )

            return IntentExecutionOutcome(
                status=IntentStatus.FAILED,
                message=message,
                intent=intent,
                plan=effective_plan,
            )

        await self._store.mark_executed(
            intent_id,
            order_ids,
        )

        return IntentExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="Executed on Bybit Demo",
            intent=intent,
            plan=effective_plan,
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

        result: PositionActionExecutionResult | None = None

        try:
            async with self._execution_lock:
                result = await self._executor.execute_position_action(action)

                await self._confirm_position_action(
                    action,
                    result,
                )

        except PositionActionConfirmationError as exc:
            assert result is not None

            logger.error(
                "Position action %s is uncertain: %s",
                action_id,
                exc,
            )

            message = f"{type(exc).__name__}: {exc}"

            await self._store.mark_position_action_uncertain(
                action_id,
                result.order_id,
                message,
            )

            records = await self._store.get_active_position_strategies()

            for state, _ in records:
                if state.symbol != action.symbol:
                    continue

                await self._store.set_position_strategy_status(
                    state.strategy_id,
                    StrategyStatus.UNCERTAIN,
                )

            return PositionActionExecutionOutcome(
                status=IntentStatus.UNCERTAIN,
                message=message,
                action=action,
                result=result,
            )

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

            # Execution may already have cancelled
            # V2 exits before the final market action
            # failed. Ask the supervisor to rebuild.
            await self._store.request_strategy_rebalance(action.symbol)

            return PositionActionExecutionOutcome(
                status=IntentStatus.FAILED,
                message=message,
                action=action,
            )

        assert result is not None

        await self._store.mark_position_action_executed(
            action_id,
            result.order_id,
        )

        if action.action is PositionActionType.REDUCE:
            await self._store.request_strategy_rebalance(action.symbol)

        elif action.action is PositionActionType.CLOSE:
            await self._store.request_strategy_close(action.symbol)

        return PositionActionExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="Executed on Bybit Demo",
            action=action,
            result=result,
        )
