from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from .domain import (
    AccountStateSummary,
    ApprovalMode,
    EntryPreflightError,
    ExecutionOrderType,
    ExecutionPlan,
    IntentExecutionOutcome,
    IntentStatus,
    OpenRelation,
    PositionActionExecutionOutcome,
    PositionActionExecutionResult,
    PositionActionIntent,
    PositionActionPreflightError,
    PositionActionType,
    StrategyStatus,
    TradingIntent,
)
from .execution import ExecutionPlanner
from .ports import AccountGateway, ExecutionCoordinatorStore

logger = logging.getLogger(__name__)


class AutoExecutionSafetyError(RuntimeError):
    pass


class PositionActionConfirmationError(RuntimeError):
    pass


class ExecutionCoordinator:
    def __init__(
        self,
        *,
        store: ExecutionCoordinatorStore,
        executor: AccountGateway,
        planner: ExecutionPlanner,
        max_age_seconds: int,
        execution_lock: asyncio.Lock | None = None,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")

        self._store = store
        self._executor = executor
        self._planner = planner
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
        prepared = await self._prepare_execution(
            intent_id,
            approval_mode,
            user_id,
        )
        if isinstance(prepared, IntentExecutionOutcome):
            return prepared
        intent, plan = prepared

        effective_plan = plan

        async with self._execution_lock:
            current = await self._store.get_intent(intent_id)
            if current is not None and current.status is IntentStatus.SKIPPED:
                return IntentExecutionOutcome(
                    status=current.status,
                    message="Entry withdrawn by source",
                    intent=current,
                    plan=plan,
                )
            strategy_created = False
            primary_filled = False
            try:
                await self._assert_open_preflight(intent, plan, approval_mode)

                await self._store.ensure_position_strategy(plan)
                strategy_created = True

                staged_market = plan.orders[0].order_type is ExecutionOrderType.MARKET

                if staged_market:
                    primary = await self._executor.execute_market_primary(plan)
                    primary_filled = True

                    context = await self._executor.market_context(plan.symbol)

                    effective_plan = self._planner.rebase_market_plan(
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

                await self._store.mark_executed(
                    intent_id,
                    order_ids,
                )

            except Exception as exc:
                logger.exception(
                    "Execution failed for %s",
                    intent_id,
                )
                return await self._execution_failure_outcome(
                    intent_id,
                    exc,
                    strategy_created=strategy_created,
                    primary_filled=primary_filled,
                    intent=intent,
                    effective_plan=effective_plan,
                )

        return IntentExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="Executed on Bybit Demo",
            intent=intent,
            plan=effective_plan,
            order_ids=order_ids,
        )

    async def _prepare_execution(
        self,
        intent_id: UUID,
        approval_mode: ApprovalMode,
        user_id: int | None,
    ) -> tuple[TradingIntent, ExecutionPlan] | IntentExecutionOutcome:
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

        if plan.strategy_version != 2:
            message = (
                f"Historical Strategy V{plan.strategy_version} plans are read-only; "
                "only Strategy V2 can execute"
            )
            await self._store.mark_failed(intent_id, message)

            return IntentExecutionOutcome(
                status=IntentStatus.FAILED,
                message=message,
                intent=intent,
                plan=plan,
            )

        return intent, plan

    async def _assert_open_preflight(
        self,
        intent: TradingIntent,
        plan: ExecutionPlan,
        approval_mode: ApprovalMode,
    ) -> None:
        active = await self._store.get_active_position_strategies()
        if any(state.symbol == plan.symbol for state, _ in active):
            raise EntryPreflightError(
                f"Existing strategy owns {plan.symbol}; resolve it before opening another"
            )
        live_state = await self._executor.account_state()
        # V2 owns a whole net position. Manual approval cannot make two
        # independent entry/exit ladders safe on the same symbol.
        conflict = self.auto_open_safety_reason(
            intent.model_copy(update={"relation": OpenRelation.NEW}), live_state
        )
        if conflict is not None:
            raise EntryPreflightError(conflict)
        if approval_mode is ApprovalMode.AUTO:
            safety_reason = self.auto_open_safety_reason(
                intent,
                live_state,
            )

            if safety_reason is not None:
                raise AutoExecutionSafetyError(safety_reason)

    async def _execution_failure_outcome(
        self,
        intent_id: UUID,
        exc: Exception,
        *,
        strategy_created: bool,
        primary_filled: bool,
        intent: TradingIntent,
        effective_plan: ExecutionPlan,
    ) -> IntentExecutionOutcome:
        message = f"{type(exc).__name__}: {exc}"

        await self._store.mark_failed(
            intent_id,
            message,
        )

        if strategy_created:
            await self._store.set_position_strategy_status(
                intent_id,
                StrategyStatus.CLOSED
                if isinstance(exc, EntryPreflightError) and not primary_filled
                else StrategyStatus.UNCERTAIN,
            )

        return IntentExecutionOutcome(
            status=IntentStatus.FAILED,
            message=message,
            intent=intent,
            plan=effective_plan,
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

        async with self._execution_lock:
            if action.action is PositionActionType.CANCEL_ENTRIES:
                return await self._cancel_entries(action)
            try:
                result = await self._executor.execute_position_action(action)
                await self._confirm_position_action(action, result)

            except PositionActionPreflightError as exc:
                # A typed preflight error is raised only before any mutation.
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

            except Exception as exc:
                # The exchange may have accepted a market reduction even
                # when its response or the later confirmation timed out.
                logger.exception(
                    "Position action %s has an uncertain exchange outcome",
                    action_id,
                )
                message = f"{type(exc).__name__}: {exc}"
                await self._store.mark_position_action_uncertain(
                    action_id,
                    action.symbol,
                    result.order_id if result is not None else None,
                    message,
                )
                return PositionActionExecutionOutcome(
                    status=IntentStatus.UNCERTAIN,
                    message=message,
                    action=action,
                    result=result,
                )

            assert result is not None
            # Status and resulting strategy rebalance/close must be durable
            # before releasing the lock to the position supervisor.
            await self._store.complete_position_action(
                action,
                result.order_id,
            )

        return PositionActionExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="Executed on Bybit Demo",
            action=action,
            result=result,
        )

    async def _cancel_entries(
        self, action: PositionActionIntent
    ) -> PositionActionExecutionOutcome:
        targets = await self._store.entry_cancellation_targets(action)
        if not targets:
            message = "No earlier entries owned by this source for this symbol"
            await self._store.mark_position_action_failed(action.action_id, message)
            return PositionActionExecutionOutcome(
                status=IntentStatus.FAILED, message=message, action=action
            )
        prefixes = tuple(f"ccb-v2-{ident.hex[:20]}-e" for ident in targets)

        def owned(order):
            return (
                order.symbol == action.symbol
                and order.order_link_id.startswith(prefixes)
                and not order.reduce_only
                and not order.is_protective
            )

        cancelled = 0
        try:
            await self._store.record_entry_cancellation(action, targets)
            state = await self._executor.account_state()
            for order in state.open_orders:
                if owned(order):
                    await self._executor.cancel_order(action.symbol, order.order_id)
                    cancelled += 1
            for attempt in range(20):
                state = await self._executor.account_state()
                if not any(owned(o) for o in state.open_orders):
                    break
                if attempt == 19:
                    raise RuntimeError("Entry cancellation not confirmed")
                await asyncio.sleep(0.25)
            await self._store.record_entry_cancellation(
                action,
                targets,
                complete=True,
                flat=not any(p.symbol == action.symbol for p in state.positions),
            )
        except Exception as exc:
            message = f"Entry cancellation uncertain: {exc}"
            await self._store.record_entry_cancellation(action, targets, error=message)
            return PositionActionExecutionOutcome(
                status=IntentStatus.UNCERTAIN, message=message, action=action
            )
        return PositionActionExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message=f"Cancelled {cancelled} source-owned entry orders; positions and protection preserved",
            action=action,
        )
