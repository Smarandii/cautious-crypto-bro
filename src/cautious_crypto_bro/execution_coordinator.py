from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from .cancellation import finish_before_cancellation
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
from .entry_ownership import recover_entry_bindings
from .execution import ExecutionPlanner
from .portfolio_risk import open_stop_risk_usdt
from .ports import AccountGateway, ExecutionCoordinatorStore
from .zone_ledger import ZoneOrder, zone_group_id

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
        portfolio_stop_risk_cap_usdt: Decimal | None = None,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if (
            portfolio_stop_risk_cap_usdt is not None
            and portfolio_stop_risk_cap_usdt <= 0
        ):
            raise ValueError("Portfolio stop-risk cap must be positive")

        self._store = store
        self._executor = executor
        self._planner = planner
        self._max_age_seconds = max_age_seconds
        self._portfolio_stop_risk_cap_usdt = portfolio_stop_risk_cap_usdt

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
        expected_remaining = self._expected_remaining(action, result)
        # Another actor can move the position before our order finishes.
        order_confirmed = False

        for attempt in range(20):
            if not order_confirmed:
                order_confirmed = await self._action_order_filled(action, result)
            if order_confirmed:
                state = await self._executor.account_state()
                if self._confirms_position_action(
                    action, result, state, expected_remaining
                ):
                    return

            if attempt < 19:
                await asyncio.sleep(0.25)

        if not order_confirmed:
            detail = "market order fill is unconfirmed"
        elif action.action is PositionActionType.CLOSE:
            detail = "position is still open"
        else:
            detail = "position quantity did not reach the submitted REDUCE target"

        raise PositionActionConfirmationError(
            f"Bybit accepted position action {result.order_id}, but {detail}"
        )

    async def _action_order_filled(
        self, action: PositionActionIntent, result: PositionActionExecutionResult
    ) -> bool:
        order = await self._executor.strategy_order(
            action.symbol, f"ccb-action-{action.action_id.hex[:24]}"
        )
        if order is None:
            return False
        if order.order_id != result.order_id:
            raise PositionActionConfirmationError("Action order identity mismatch")
        if (
            order.status
            in {"Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}
            and order.executed_quantity <= 0
        ):
            raise PositionActionConfirmationError(
                f"Action order ended without a fill: {order.status}"
            )
        return (
            order.status in {"Filled", "Cancelled", "PartiallyFilledCanceled"}
            and order.executed_quantity > 0
        )

    @staticmethod
    def _expected_remaining(
        action: PositionActionIntent,
        result: PositionActionExecutionResult,
    ) -> Decimal | None:
        if action.action is not PositionActionType.REDUCE:
            return None

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

        return expected_remaining

    @staticmethod
    def _confirms_position_action(
        action: PositionActionIntent,
        result: PositionActionExecutionResult,
        state: AccountStateSummary,
        expected_remaining: Decimal | None,
    ) -> bool:
        positions = tuple(
            position
            for position in state.positions
            if (position.symbol == action.symbol)
        )

        label = action.action.value

        if action.action is PositionActionType.CLOSE:
            if not positions:
                return True

        else:
            if not positions:
                raise PositionActionConfirmationError(
                    "REDUCE unexpectedly closed the full position"
                )

        if len(positions) != 1:
            raise PositionActionConfirmationError(
                f"{label} confirmation found multiple live positions"
            )

        position = positions[0]

        if position.side is not result.position_side:
            raise PositionActionConfirmationError(
                f"{label} confirmation found opposite-side exposure"
            )

        if action.action is PositionActionType.CLOSE:
            return False

        assert expected_remaining is not None

        return position.size <= expected_remaining

    async def execute_intent(
        self,
        intent_id: UUID,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None = None,
    ) -> IntentExecutionOutcome:
        async with self._execution_lock:
            return await finish_before_cancellation(
                self._execute_locked_intent(intent_id, approval_mode, user_id)
            )

    async def _execute_locked_intent(
        self, intent_id: UUID, approval_mode: ApprovalMode, user_id: int | None
    ) -> IntentExecutionOutcome:
        # A cancelled waiter must not leave a durable execution claim. Once
        # ownership is acquired, shield preparation through the terminal outcome.
        prepared = await self._prepare_execution(
            intent_id,
            approval_mode,
            user_id,
        )
        if isinstance(prepared, IntentExecutionOutcome):
            return prepared
        intent, plan = prepared
        effective_plan = plan
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

            prepared_plan = await self._executor.prepare_entry_plan(plan)
            if prepared_plan.leverage > plan.leverage or prepared_plan.model_dump(
                exclude={"leverage", "entry_slices"}
            ) != plan.model_dump(exclude={"leverage", "entry_slices"}):
                raise EntryPreflightError(
                    "Entry preparation changed frozen risk geometry or increased leverage"
                )
            if prepared_plan != plan:
                await self._store.update_execution_plan(prepared_plan)
            plan = effective_plan = prepared_plan

            await self._store.ensure_position_strategy(plan)
            strategy_created = True

            staged_market = plan.orders[0].order_type is ExecutionOrderType.MARKET

            if staged_market:
                plan = effective_plan = await self._prepare_physical_primary(plan)
                primary = (
                    await self._executor.execute_prepared_market_primary(plan)
                    if plan.entry_slices
                    else await self._executor.execute_market_primary(plan)
                )
                primary_filled = True

                context = await self._executor.market_context(plan.symbol)

                effective_plan = self._planner.rebase_market_plan(
                    plan.model_copy(update={"entry_slices": ()}),
                    fill_price=(primary.average_fill_price),
                    filled_quantity=(primary.filled_quantity),
                    context=context,
                )

                effective_plan = await self._prepare_physical_remaining(
                    plan, effective_plan, primary
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
                    *(primary.order_ids or (primary.order_id,)),
                    *remaining_ids,
                )

            else:
                if plan.entry_slices:
                    await self._store.register_zone(
                        zone_group_id(plan.symbol, plan.side), plan.intent_id
                    )
                order_ids = await self._executor.execute(plan)

            await self._store.mark_executed(
                intent_id,
                order_ids,
            )

            await self._enroll_executed_zone(
                effective_plan, order_ids, registered=bool(plan.entry_slices)
            )

        except Exception as exc:
            if plan.entry_slices and strategy_created:
                await self._recover_entry_bindings(effective_plan)
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

    async def _prepare_physical_primary(self, plan: ExecutionPlan) -> ExecutionPlan:
        if not plan.entry_slices:
            return plan
        prepared = await self._executor.prepare_market_primary(plan)
        await self._store.update_execution_plan(prepared)
        if prepared.entry_slices:
            await self._store.register_zone(
                zone_group_id(prepared.symbol, prepared.side), prepared.intent_id
            )
        return prepared

    async def _prepare_physical_remaining(
        self, submitted, rebased, primary
    ) -> ExecutionPlan:
        if submitted.entry_slices:
            rebased = rebased.model_copy(
                update={
                    "entry_slices": primary.entry_slices
                    + tuple(s for s in submitted.entry_slices if s.leg_name != "E1")
                }
            )
        allocated = await self._executor.prepare_remaining_entries(rebased)
        if allocated.entry_slices and not submitted.entry_slices:
            if not primary.entry_slices:
                raise RuntimeError(
                    "Physical scale-in requires submitted primary evidence"
                )
            allocated = ExecutionPlan.model_validate(
                allocated.model_dump()
                | {
                    "entry_slices": primary.entry_slices
                    + tuple(s for s in allocated.entry_slices if s.leg_name != "E1")
                }
            )
            await self._store.update_execution_plan(allocated)
            await self._store.register_zone(
                zone_group_id(allocated.symbol, allocated.side), allocated.intent_id
            )
        return allocated

    async def _recover_entry_bindings(self, plan: ExecutionPlan) -> None:
        """Read accepted links after uncertain submission; never retry creation."""
        try:
            plan = await self._store.get_execution_plan(plan.intent_id) or plan
            await recover_entry_bindings(plan, self._store, self._executor)
        except Exception:
            logger.exception(
                "Physical entry binding recovery pending for %s", plan.intent_id
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

    async def _enroll_executed_zone(
        self,
        plan,
        order_ids: tuple[str, ...],
        *,
        registered: bool = False,
    ) -> None:
        """Freeze the executed zone and bind its entry orders durably (#123).

        The ledger is durable evidence, never permission to mutate: orders are
        already live on the exchange, so an evidence-write failure must not
        turn a filled execution into a failure outcome or trigger cleanup.
        """
        if plan.strategy_version < 2:
            return
        try:
            group_id = zone_group_id(plan.symbol, plan.side)
            if not registered:
                await self._store.register_zone(group_id, plan.intent_id)
            prefix = f"ccb-v2-{plan.intent_id.hex[:20]}-"
            for order, order_id in zip(plan.entry_orders, order_ids, strict=True):
                await self._store.bind_zone_order(
                    ZoneOrder(
                        zone_id=plan.intent_id,
                        order_id=str(order_id),
                        order_link_id=prefix + order.name.lower(),
                        role="ENTRY",
                        quantity=order.quantity,
                    )
                )
        except Exception:
            logger.exception(
                "Zone enrollment failed for %s; orders remain live and "
                "supervision must reconcile the unenrolled allocation",
                plan.intent_id,
            )

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
        if self._portfolio_stop_risk_cap_usdt is not None:
            try:
                open_risk = open_stop_risk_usdt(live_state, active)
            except ValueError as exc:
                raise EntryPreflightError(
                    f"Portfolio stop-risk cap cannot be verified: {exc}"
                ) from exc
            projected_risk = open_risk + plan.planned_max_loss_usdt
            if projected_risk > self._portfolio_stop_risk_cap_usdt:
                raise EntryPreflightError(
                    "Portfolio stop-risk cap exceeded: "
                    f"open={open_risk}, new={plan.planned_max_loss_usdt}, "
                    f"cap={self._portfolio_stop_risk_cap_usdt}"
                )
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

        strategy_status = None
        if strategy_created:
            strategy_status = (
                StrategyStatus.CLOSED
                if isinstance(exc, EntryPreflightError) and not primary_filled
                else StrategyStatus.UNCERTAIN
            )
        await self._store.record_entry_failure(
            intent_id, message, strategy_status=strategy_status
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

        async with self._execution_lock:
            return await finish_before_cancellation(
                self._execute_locked_position_action(
                    action, approval_mode=approval_mode, user_id=user_id
                )
            )

    async def _execute_locked_position_action(
        self,
        action: PositionActionIntent,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None,
    ) -> PositionActionExecutionOutcome:
        # Check age and claim only after queueing, then finish the durable
        # transition under the same cancellation shield as exchange work.
        action, terminal = await self._claim_position_action(
            action.action_id, action, approval_mode=approval_mode, user_id=user_id
        )
        if terminal is not None:
            return terminal
        action_id = action.action_id
        result: PositionActionExecutionResult | None = None
        if action.action is PositionActionType.CANCEL_ENTRIES:
            return await self._cancel_entries(action)
        try:
            result = await self._executor.execute_position_action(action)
            await self._confirm_position_action(action, result)
            # Final persistence can fail after the exchange action is confirmed.
            # Retain the acknowledged order and quarantine before releasing the lock.
            await self._store.complete_position_action(action, result.order_id)

        except PositionActionPreflightError as exc:
            return await self._preflight_failure_outcome(action_id, action, exc)

        except Exception as exc:
            # The exchange may have accepted a market reduction even
            # when its response or the later confirmation timed out.
            logger.exception(
                "Position action %s has an uncertain exchange outcome",
                action_id,
            )
            return await self._uncertain_outcome(action_id, action, result, exc)

        assert result is not None
        return PositionActionExecutionOutcome(
            status=IntentStatus.EXECUTED,
            message="Executed on Bybit Demo",
            action=action,
            result=result,
        )

    async def _claim_position_action(
        self,
        action_id: UUID,
        action: PositionActionIntent,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None,
    ) -> tuple[PositionActionIntent, PositionActionExecutionOutcome | None]:
        # Re-extraction after an outage must not freshen an old close instruction.
        action_time = min(action.created_at, action.source.published_at)
        age = (datetime.now(UTC) - action_time).total_seconds()

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

            return (
                action,
                PositionActionExecutionOutcome(
                    status=IntentStatus.FAILED,
                    message=message,
                    action=action,
                ),
            )

        claimed = await self._store.claim_position_action_for_execution(
            action_id,
            user_id,
            expected_approval_mode=(approval_mode),
        )

        if not claimed:
            current = await self._store.get_position_action(action_id)

            return (
                action,
                PositionActionExecutionOutcome(
                    status=(
                        current.status if current is not None else IntentStatus.FAILED
                    ),
                    message=(
                        "Position action was already handled or approval mode changed"
                    ),
                    action=current or action,
                ),
            )

        return action, None

    async def _preflight_failure_outcome(
        self,
        action_id: UUID,
        action: PositionActionIntent,
        exc: PositionActionPreflightError,
    ) -> PositionActionExecutionOutcome:
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

    async def _uncertain_outcome(
        self,
        action_id: UUID,
        action: PositionActionIntent,
        result: PositionActionExecutionResult | None,
        exc: Exception,
    ) -> PositionActionExecutionOutcome:
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
            # Separate position/order reads can miss a fill between requests.
            # Confirm again before making a flat strategy terminal.
            state = await self._executor.account_state()
            if any(owned(order) for order in state.open_orders):
                raise RuntimeError("Entry cancellation not confirmed")
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
