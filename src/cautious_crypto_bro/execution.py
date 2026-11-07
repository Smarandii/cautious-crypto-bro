from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    Decimal,
)

from .domain import (
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    InstrumentContext,
    PlannedOrder,
    PlannedTakeProfit,
    Side,
    StopLossSource,
    StrategyV2Policy,
    TakeProfitSource,
    TradingIntent,
)
from .execution_geometry import (
    ExecutionPlanningError,
    TakeProfitRequest,
    build_take_profit_targets,
    derived_take_profits,
    round_down,
    round_price,
    trader_capped_rules,
    validate_entry_geometry,
    validate_initial_exit_capacity,
    validate_not_passed_through,
    validate_target_geometry,
)


@dataclass(frozen=True, slots=True)
class EntrySpec:
    name: str
    order_type: ExecutionOrderType
    reference_price: Decimal
    risk_pct: Decimal


@dataclass(frozen=True, slots=True)
class _RebaseGeometry:
    """Values that hold for every scale-in leg of one actual-fill rebase."""

    stop_loss: Decimal
    risk_budget: Decimal
    remaining_scale: Decimal
    context: InstrumentContext


def _weighted_entry(orders: Sequence[PlannedOrder]) -> Decimal:
    total_quantity = sum(
        (order.quantity for order in orders),
        Decimal("0"),
    )

    return (
        sum(
            (order.reference_price * order.quantity for order in orders),
            Decimal("0"),
        )
        / total_quantity
    )


class ExecutionPlanner:
    def plan(
        self,
        intent: TradingIntent,
        policy: ExecutionPolicy,
        context: InstrumentContext,
    ) -> ExecutionPlan:
        strategy = policy.strategy_v2
        stop_loss, stop_source = self._resolve_stop_loss(intent, strategy, context)

        entry_specs = self._entry_specs(
            intent,
            strategy,
            context,
            stop_loss,
        )

        reference_prices = tuple(spec.reference_price for spec in entry_specs)

        self._validate_entry_geometry(
            intent.side,
            reference_prices,
            stop_loss,
        )

        self._validate_not_passed_through(
            intent,
            reference_prices,
            context.market_price,
        )

        orders, planned_max_loss = self._risk_checked_orders(
            entry_specs,
            policy,
            context,
            stop_loss,
        )

        weighted_entry = _weighted_entry(orders)

        (
            take_profit_targets,
            take_profit_source,
        ) = self._take_profit_targets(
            intent,
            strategy,
            weighted_entry,
            stop_loss,
            context.tick_size,
        )

        self._validate_target_geometry(
            intent.side,
            weighted_entry,
            tuple(target.price for target in take_profit_targets),
        )

        self._validate_initial_exit_capacity(
            intent.side,
            orders[0],
            stop_loss,
            take_profit_targets,
            context,
        )

        return ExecutionPlan(
            strategy_version=2,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            leverage=self._plan_leverage(intent, context),
            orders=orders,
            stop_loss=stop_loss,
            stop_loss_source=stop_source,
            take_profit=(take_profit_targets[-1].price),
            take_profit_targets=(take_profit_targets),
            take_profit_source=(take_profit_source),
            trader_take_profit=(
                Decimal(str(intent.take_profit))
                if intent.take_profit is not None
                else None
            ),
            runner_pct=(strategy.runner_pct),
            policy=policy.model_copy(deep=True),
            planned_max_loss_usdt=(planned_max_loss),
        )

    @staticmethod
    def _plan_leverage(intent: TradingIntent, context: InstrumentContext) -> Decimal:
        requested = (
            Decimal(str(intent.leverage))
            if intent.leverage is not None
            else Decimal("10")
        )
        limits = context.leverage_filter
        # Offline contexts created before leverage constraints retain their old
        # behavior. The live adapter requires and validates the exchange filter.
        if limits is None:
            return requested
        try:
            return limits.select(requested)
        except ValueError as exc:
            raise ExecutionPlanningError(str(exc)) from exc

    def _risk_checked_orders(
        self,
        entry_specs: tuple[
            EntrySpec,
            EntrySpec,
            EntrySpec,
        ],
        policy: ExecutionPolicy,
        context: InstrumentContext,
        stop_loss: Decimal,
    ) -> tuple[
        tuple[PlannedOrder, PlannedOrder, PlannedOrder],
        Decimal,
    ]:
        (
            primary,
            secondary,
            tertiary,
        ) = (
            self._planned_order(
                spec,
                policy,
                context,
                stop_loss,
            )
            for spec in entry_specs
        )

        orders = (
            primary,
            secondary,
            tertiary,
        )

        planned_max_loss = sum(
            (
                order.quantity * abs(order.reference_price - stop_loss)
                for order in orders
            ),
            Decimal("0"),
        )

        if planned_max_loss <= 0:
            raise ExecutionPlanningError("Planned maximum loss must be positive")

        if planned_max_loss > policy.risk_budget_usdt:
            raise ExecutionPlanningError(
                "Strategy V2 entry ladder exceeds the configured risk budget"
            )

        return orders, planned_max_loss

    def _resolve_stop_loss(
        self,
        intent: TradingIntent,
        strategy: StrategyV2Policy,
        context: InstrumentContext,
    ) -> tuple[Decimal, StopLossSource]:
        if intent.stop_loss is not None:
            stop = self._round_price(Decimal(str(intent.stop_loss)), context.tick_size)
            source = StopLossSource.TRADER
        else:
            if intent.entry.type is EntryType.MARKET:
                anchor = context.market_price
            elif intent.entry.type is EntryType.LIMIT:
                anchor = Decimal(str(intent.entry.price))
            else:
                anchor = Decimal(
                    str(
                        intent.entry.range_low
                        if intent.side is Side.LONG
                        else intent.entry.range_high
                    )
                )
            distance = strategy.fallback_stop_distance_pct / Decimal("100")
            raw_stop = anchor * (
                1 - distance if intent.side is Side.LONG else 1 + distance
            )
            rounding = ROUND_FLOOR if intent.side is Side.LONG else ROUND_CEILING
            stop = (raw_stop / context.tick_size).to_integral_value(
                rounding=rounding
            ) * context.tick_size
            source = StopLossSource.POLICY
        if stop <= 0:
            raise ExecutionPlanningError(
                "Stop loss must be positive after tick rounding"
            )
        return stop, source

    def refresh_market_plan(
        self,
        plan: ExecutionPlan,
        context: InstrumentContext,
    ) -> ExecutionPlan:
        """Downsize E1 at the submission quote, keeping the frozen stop and budget."""
        if (
            plan.strategy_version != 2
            or plan.orders[0].order_type is not ExecutionOrderType.MARKET
        ):
            raise ExecutionPlanningError(
                "Only Strategy V2 MARKET plans can be refreshed"
            )

        price = context.market_price
        if not price.is_finite() or price <= 0:
            raise ExecutionPlanningError("Market price must be positive and finite")
        if (plan.side is Side.LONG and price <= plan.stop_loss) or (
            plan.side is Side.SHORT and price >= plan.stop_loss
        ):
            raise ExecutionPlanningError("Market moved outside V2 stop geometry")

        primary_budget = (
            plan.policy.risk_budget_usdt
            * plan.policy.strategy_v2.primary_entry_risk_pct
            / Decimal("100")
        )
        quantity = self._round_down(
            min(
                plan.orders[0].quantity,
                primary_budget / abs(price - plan.stop_loss),
            ),
            context.qty_step,
        )
        if quantity <= 0 or quantity < context.min_qty:
            raise ExecutionPlanningError(
                "Refreshed E1 rounds below Bybit minimum quantity"
            )
        if context.min_notional and quantity * price < context.min_notional:
            raise ExecutionPlanningError(
                "Refreshed E1 rounds below Bybit minimum notional"
            )

        # Validate the prospective scale-ins and exits before submitting E1.
        # The coordinator repeats this calculation with the actual fill.
        return self.rebase_market_plan(
            plan,
            fill_price=price,
            filled_quantity=quantity,
            context=context,
        )

    def rebase_market_plan(
        self,
        plan: ExecutionPlan,
        *,
        fill_price: Decimal,
        filled_quantity: Decimal,
        context: InstrumentContext,
    ) -> ExecutionPlan:
        self._validate_rebase_inputs(plan, fill_price, filled_quantity)

        stop_loss = plan.stop_loss
        strategy = plan.policy.strategy_v2
        risk_budget = plan.policy.risk_budget_usdt

        primary_distance, second_price, third_price = self._rebase_reference_prices(
            plan,
            fill_price,
            stop_loss,
            strategy,
            context,
        )

        remaining_scale = self._rebase_remaining_scale(
            plan,
            filled_quantity,
            primary_distance,
        )

        primary = plan.orders[0].model_copy(
            update={
                "quantity": filled_quantity,
                "reference_price": fill_price,
                "price": None,
            }
        )

        geometry = _RebaseGeometry(
            stop_loss=stop_loss,
            risk_budget=risk_budget,
            remaining_scale=remaining_scale,
            context=context,
        )

        secondary = self._scale_in_order(
            geometry,
            name="E2",
            reference_price=second_price,
            risk_pct=(strategy.secondary_entry_risk_pct),
        )

        tertiary = self._scale_in_order(
            geometry,
            name="E3",
            reference_price=third_price,
            risk_pct=(strategy.tertiary_entry_risk_pct),
        )

        orders = (
            primary,
            secondary,
            tertiary,
        )

        planned_max_loss = sum(
            (
                order.quantity * abs(order.reference_price - stop_loss)
                for order in orders
            ),
            Decimal("0"),
        )

        if planned_max_loss > risk_budget:
            raise ExecutionPlanningError(
                "Actual-fill V2 plan exceeds the configured risk budget"
            )

        weighted_entry = _weighted_entry(orders)

        target_tuple, take_profit_source, trader_target = self._resolve_rebase_targets(
            plan,
            strategy,
            weighted_entry,
            stop_loss,
            primary,
            context,
        )

        return self._rebuild_rebased_plan(
            plan,
            orders,
            target_tuple,
            take_profit_source,
            trader_target,
            planned_max_loss,
        )

    def _scale_in_order(
        self,
        geometry: _RebaseGeometry,
        *,
        name: str,
        reference_price: Decimal,
        risk_pct: Decimal,
    ) -> PlannedOrder:
        context = geometry.context

        distance = abs(reference_price - geometry.stop_loss)

        if distance <= 0:
            raise ExecutionPlanningError(f"{name} has no entry-to-stop distance")

        nominal_budget = geometry.risk_budget * risk_pct / Decimal("100")

        effective_budget = nominal_budget * geometry.remaining_scale

        quantity = self._round_down(
            effective_budget / distance,
            context.qty_step,
        )

        if quantity <= 0 or quantity < context.min_qty:
            raise ExecutionPlanningError(
                f"{name} actual-fill rebasing rounds below Bybit minimum quantity"
            )

        if context.min_notional and (quantity * reference_price < context.min_notional):
            raise ExecutionPlanningError(
                f"{name} actual-fill rebasing rounds below Bybit minimum notional"
            )

        return PlannedOrder(
            name=name,
            risk_pct=risk_pct,
            order_type=(ExecutionOrderType.LIMIT),
            quantity=quantity,
            price=reference_price,
            reference_price=reference_price,
            take_profit=None,
        )

    def _validate_rebase_inputs(
        self,
        plan: ExecutionPlan,
        fill_price: Decimal,
        filled_quantity: Decimal,
    ) -> None:
        if plan.strategy_version < 2:
            raise ExecutionPlanningError("Only Strategy V2 plans can be rebased")

        if len(plan.orders) != 3:
            raise ExecutionPlanningError("Strategy V2 requires three entry legs")

        if plan.orders[0].order_type is not ExecutionOrderType.MARKET:
            raise ExecutionPlanningError("Only MARKET-primary V2 plans can be rebased")

        if any(
            order.order_type is not ExecutionOrderType.LIMIT
            for order in plan.orders[1:]
        ):
            raise ExecutionPlanningError("V2 MARKET scale-ins must be LIMIT orders")

        if fill_price <= 0:
            raise ExecutionPlanningError("Actual E1 fill price must be positive")

        if filled_quantity <= 0:
            raise ExecutionPlanningError("Actual E1 fill quantity must be positive")

        stop_loss = plan.stop_loss

        if plan.side is Side.LONG and fill_price <= stop_loss:
            raise ExecutionPlanningError("Actual LONG E1 fill is outside stop geometry")

        if plan.side is Side.SHORT and fill_price >= stop_loss:
            raise ExecutionPlanningError(
                "Actual SHORT E1 fill is outside stop geometry"
            )

    def _rebase_reference_prices(
        self,
        plan: ExecutionPlan,
        fill_price: Decimal,
        stop_loss: Decimal,
        strategy: StrategyV2Policy,
        context: InstrumentContext,
    ) -> tuple[Decimal, Decimal, Decimal]:
        primary_distance = abs(fill_price - stop_loss)

        direction = Decimal("-1") if plan.side is Side.LONG else Decimal("1")

        second_price = self._round_price(
            fill_price
            + (direction * strategy.secondary_entry_depth_r * primary_distance),
            context.tick_size,
        )

        third_price = self._round_price(
            fill_price
            + (direction * strategy.tertiary_entry_depth_r * primary_distance),
            context.tick_size,
        )

        reference_prices = (
            fill_price,
            second_price,
            third_price,
        )

        self._validate_entry_geometry(
            plan.side,
            reference_prices,
            stop_loss,
        )

        return primary_distance, second_price, third_price

    def _rebase_remaining_scale(
        self,
        plan: ExecutionPlan,
        filled_quantity: Decimal,
        primary_distance: Decimal,
    ) -> Decimal:
        risk_budget = plan.policy.risk_budget_usdt
        strategy = plan.policy.strategy_v2

        primary_risk = filled_quantity * primary_distance

        if primary_risk >= risk_budget:
            raise ExecutionPlanningError(
                "Actual E1 fill consumes the full Strategy V2 risk budget"
            )

        nominal_remaining_budget = (
            risk_budget
            * (strategy.secondary_entry_risk_pct + strategy.tertiary_entry_risk_pct)
            / Decimal("100")
        )

        available_remaining_budget = risk_budget - primary_risk

        remaining_scale = min(
            Decimal("1"),
            (available_remaining_budget / nominal_remaining_budget),
        )

        if remaining_scale <= 0:
            raise ExecutionPlanningError("No risk budget remains for V2 scale-ins")

        return remaining_scale

    def _resolve_rebase_targets(
        self,
        plan: ExecutionPlan,
        strategy: StrategyV2Policy,
        weighted_entry: Decimal,
        stop_loss: Decimal,
        primary: PlannedOrder,
        context: InstrumentContext,
    ) -> tuple[
        tuple[PlannedTakeProfit, PlannedTakeProfit, PlannedTakeProfit],
        TakeProfitSource,
        Decimal | None,
    ]:
        risk_distance = abs(weighted_entry - stop_loss)

        if risk_distance <= 0:
            raise ExecutionPlanningError(
                "Actual-fill weighted entry has no stop distance"
            )

        trader_target = plan.trader_take_profit or (
            plan.take_profit
            if plan.take_profit_source is TakeProfitSource.TRADER
            else None
        )
        target_tuple, take_profit_source = self._build_take_profit_targets(
            TakeProfitRequest(
                side=plan.side,
                strategy=strategy,
                weighted_entry=weighted_entry,
                risk_distance=risk_distance,
                tick_size=context.tick_size,
                trader_take_profit=(
                    self._round_price(trader_target, context.tick_size)
                    if trader_target is not None
                    else None
                ),
                trader_target_error=(
                    "Trader take profit became too close after actual E1 fill"
                ),
                collapsed_targets_error=(
                    "Actual-fill V2 take-profit levels collapse after rounding"
                ),
            ),
        )

        self._validate_target_geometry(
            plan.side,
            weighted_entry,
            tuple(target.price for target in target_tuple),
        )

        self._validate_initial_exit_capacity(
            plan.side,
            primary,
            stop_loss,
            target_tuple,
            context,
        )

        return target_tuple, take_profit_source, trader_target

    def _rebuild_rebased_plan(
        self,
        plan: ExecutionPlan,
        orders: tuple[PlannedOrder, PlannedOrder, PlannedOrder],
        target_tuple: tuple[PlannedTakeProfit, PlannedTakeProfit, PlannedTakeProfit],
        take_profit_source: TakeProfitSource,
        trader_target: Decimal | None,
        planned_max_loss: Decimal,
    ) -> ExecutionPlan:
        payload = plan.model_dump()

        payload.update(
            {
                "orders": orders,
                "take_profit": (target_tuple[-1].price),
                "take_profit_targets": (target_tuple),
                "take_profit_source": (take_profit_source),
                "trader_take_profit": trader_target,
                "planned_max_loss_usdt": (planned_max_loss),
            }
        )

        return ExecutionPlan.model_validate(payload)

    def _entry_specs(
        self,
        intent: TradingIntent,
        strategy: StrategyV2Policy,
        context: InstrumentContext,
        stop_loss: Decimal,
    ) -> tuple[
        EntrySpec,
        EntrySpec,
        EntrySpec,
    ]:
        weights = (
            strategy.primary_entry_risk_pct,
            strategy.secondary_entry_risk_pct,
            strategy.tertiary_entry_risk_pct,
        )

        entry = intent.entry

        if entry.type is EntryType.RANGE:
            prices = self._range_entry_prices(intent, context)

            primary_type = ExecutionOrderType.LIMIT

        else:
            prices, primary_type = self._ladder_entry_prices(
                intent,
                strategy,
                context,
                stop_loss,
            )

        return (
            EntrySpec(
                name="E1",
                order_type=primary_type,
                reference_price=prices[0],
                risk_pct=weights[0],
            ),
            EntrySpec(
                name="E2",
                order_type=(ExecutionOrderType.LIMIT),
                reference_price=prices[1],
                risk_pct=weights[1],
            ),
            EntrySpec(
                name="E3",
                order_type=(ExecutionOrderType.LIMIT),
                reference_price=prices[2],
                risk_pct=weights[2],
            ),
        )

    def _range_entry_prices(
        self,
        intent: TradingIntent,
        context: InstrumentContext,
    ) -> tuple[Decimal, Decimal, Decimal]:
        entry = intent.entry

        assert entry.range_low is not None
        assert entry.range_high is not None

        low = Decimal(str(entry.range_low))
        high = Decimal(str(entry.range_high))

        middle = (low + high) / Decimal("2")

        if intent.side is Side.LONG:
            raw_prices = (
                high,
                middle,
                low,
            )
        else:
            raw_prices = (
                low,
                middle,
                high,
            )

        prices = tuple(
            self._round_price(
                price,
                context.tick_size,
            )
            for price in raw_prices
        )

        if len(set(prices)) != 3:
            raise ExecutionPlanningError(
                "Entry range is too narrow for three distinct V2 legs"
            )

        return (
            prices[0],
            prices[1],
            prices[2],
        )

    def _ladder_entry_prices(
        self,
        intent: TradingIntent,
        strategy: StrategyV2Policy,
        context: InstrumentContext,
        stop_loss: Decimal,
    ) -> tuple[tuple[Decimal, Decimal, Decimal], ExecutionOrderType]:
        entry = intent.entry

        if entry.type is EntryType.MARKET:
            primary_price = context.market_price
            primary_type = ExecutionOrderType.MARKET

        else:
            assert entry.price is not None

            primary_price = self._round_price(
                Decimal(str(entry.price)),
                context.tick_size,
            )
            primary_type = ExecutionOrderType.LIMIT

        primary_distance = abs(primary_price - stop_loss)

        if primary_distance <= 0:
            raise ExecutionPlanningError("Primary entry and stop must be different")

        direction = Decimal("-1") if intent.side is Side.LONG else Decimal("1")

        second = self._round_price(
            primary_price
            + (direction * strategy.secondary_entry_depth_r * primary_distance),
            context.tick_size,
        )

        third = self._round_price(
            primary_price
            + (direction * strategy.tertiary_entry_depth_r * primary_distance),
            context.tick_size,
        )

        prices = (
            primary_price,
            second,
            third,
        )

        if len(set(prices)) != 3:
            raise ExecutionPlanningError(
                "V2 entry levels collapse after tick-size rounding"
            )

        return prices, primary_type

    def _planned_order(
        self,
        spec: EntrySpec,
        policy: ExecutionPolicy,
        context: InstrumentContext,
        stop_loss: Decimal,
    ) -> PlannedOrder:
        stop_distance = abs(spec.reference_price - stop_loss)

        if stop_distance <= 0:
            raise ExecutionPlanningError(f"{spec.name} has no entry-to-stop distance")

        leg_budget = policy.risk_budget_usdt * spec.risk_pct / Decimal("100")

        raw_quantity = leg_budget / stop_distance

        quantity = self._round_down(
            raw_quantity,
            context.qty_step,
        )

        if quantity <= 0 or quantity < context.min_qty:
            raise ExecutionPlanningError(
                f"{spec.name} risk allocation rounds below Bybit minimum quantity"
            )

        if (
            context.min_notional
            and (quantity * spec.reference_price) < context.min_notional
        ):
            raise ExecutionPlanningError(
                f"{spec.name} risk allocation rounds below Bybit minimum notional"
            )

        return PlannedOrder(
            name=spec.name,
            risk_pct=spec.risk_pct,
            order_type=spec.order_type,
            quantity=quantity,
            price=(
                spec.reference_price
                if (spec.order_type is ExecutionOrderType.LIMIT)
                else None
            ),
            reference_price=(spec.reference_price),
            take_profit=None,
        )

    def _take_profit_targets(
        self,
        intent: TradingIntent,
        strategy: StrategyV2Policy,
        weighted_entry: Decimal,
        stop_loss: Decimal,
        tick_size: Decimal,
    ) -> tuple[
        tuple[
            PlannedTakeProfit,
            PlannedTakeProfit,
            PlannedTakeProfit,
        ],
        TakeProfitSource,
    ]:
        risk_distance = abs(weighted_entry - stop_loss)

        if risk_distance <= 0:
            raise ExecutionPlanningError("Weighted entry and stop must be different")

        trader_tp = (
            self._round_price(Decimal(str(intent.take_profit)), tick_size)
            if intent.take_profit is not None
            else None
        )
        return self._build_take_profit_targets(
            TakeProfitRequest(
                side=intent.side,
                strategy=strategy,
                weighted_entry=weighted_entry,
                risk_distance=risk_distance,
                tick_size=tick_size,
                trader_take_profit=trader_tp,
                trader_target_error=(
                    "Trader take profit is too close for Strategy V2 partial exits"
                ),
                collapsed_targets_error="V2 take-profit levels collapse after rounding",
            ),
        )

    _build_take_profit_targets = staticmethod(build_take_profit_targets)
    _trader_capped_rules = staticmethod(trader_capped_rules)
    _derived_take_profits = staticmethod(derived_take_profits)
    _validate_initial_exit_capacity = staticmethod(validate_initial_exit_capacity)
    _validate_entry_geometry = staticmethod(validate_entry_geometry)
    _validate_target_geometry = staticmethod(validate_target_geometry)
    _validate_not_passed_through = staticmethod(validate_not_passed_through)
    _round_price = staticmethod(round_price)
    _round_down = staticmethod(round_down)
