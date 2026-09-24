from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Decimal,
)

from .domain import (
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    PlannedOrder,
    PlannedTakeProfit,
    Side,
    StopLossSource,
    StrategyV2Policy,
    TakeProfitSource,
    TradingIntent,
)


class ExecutionPlanningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InstrumentContext:
    market_price: Decimal
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True, slots=True)
class EntrySpec:
    name: str
    order_type: ExecutionOrderType
    reference_price: Decimal
    risk_pct: Decimal


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

        orders = tuple(
            self._planned_order(
                spec,
                policy,
                context,
                stop_loss,
            )
            for spec in entry_specs
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

        total_quantity = sum(
            (order.quantity for order in orders),
            Decimal("0"),
        )

        weighted_entry = (
            sum(
                (order.reference_price * order.quantity for order in orders),
                Decimal("0"),
            )
            / total_quantity
        )

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

    def rebase_market_plan(
        self,
        plan: ExecutionPlan,
        *,
        fill_price: Decimal,
        filled_quantity: Decimal,
        context: InstrumentContext,
    ) -> ExecutionPlan:
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
        strategy = plan.policy.strategy_v2

        if plan.side is Side.LONG and fill_price <= stop_loss:
            raise ExecutionPlanningError("Actual LONG E1 fill is outside stop geometry")

        if plan.side is Side.SHORT and fill_price >= stop_loss:
            raise ExecutionPlanningError(
                "Actual SHORT E1 fill is outside stop geometry"
            )

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

        risk_budget = plan.policy.risk_budget_usdt

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

        primary = plan.orders[0].model_copy(
            update={
                "quantity": filled_quantity,
                "reference_price": fill_price,
                "price": None,
            }
        )

        def scale_in(
            *,
            name: str,
            reference_price: Decimal,
            risk_pct: Decimal,
        ) -> PlannedOrder:
            distance = abs(reference_price - stop_loss)

            if distance <= 0:
                raise ExecutionPlanningError(f"{name} has no entry-to-stop distance")

            nominal_budget = risk_budget * risk_pct / Decimal("100")

            effective_budget = nominal_budget * remaining_scale

            quantity = self._round_down(
                effective_budget / distance,
                context.qty_step,
            )

            if quantity <= 0 or quantity < context.min_qty:
                raise ExecutionPlanningError(
                    f"{name} actual-fill rebasing rounds below Bybit minimum quantity"
                )

            if context.min_notional and (
                quantity * reference_price < context.min_notional
            ):
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

        secondary = scale_in(
            name="E2",
            reference_price=second_price,
            risk_pct=(strategy.secondary_entry_risk_pct),
        )

        tertiary = scale_in(
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

        total_quantity = sum(
            (order.quantity for order in orders),
            Decimal("0"),
        )

        weighted_entry = (
            sum(
                (order.reference_price * order.quantity for order in orders),
                Decimal("0"),
            )
            / total_quantity
        )

        risk_distance = abs(weighted_entry - stop_loss)

        if risk_distance <= 0:
            raise ExecutionPlanningError(
                "Actual-fill weighted entry has no stop distance"
            )

        rules = list(strategy.exit_rules)

        take_profit_source = TakeProfitSource.POLICY

        if plan.take_profit_source is TakeProfitSource.TRADER:
            trader_tp = plan.take_profit

            if plan.side is Side.LONG:
                trader_reward = trader_tp - weighted_entry
            else:
                trader_reward = weighted_entry - trader_tp

            trader_r = trader_reward / risk_distance

            if trader_r <= strategy.first_take_profit_r:
                raise ExecutionPlanningError(
                    "Trader take profit became too close after actual E1 fill"
                )

            if trader_r < strategy.third_take_profit_r:
                take_profit_source = TakeProfitSource.TRADER

                middle_r = (strategy.first_take_profit_r + trader_r) / Decimal("2")

                rules = [
                    (
                        "TP1",
                        strategy.first_take_profit_r,
                        strategy.first_take_profit_pct,
                    ),
                    (
                        "TP2",
                        middle_r,
                        strategy.second_take_profit_pct,
                    ),
                    (
                        "TP3",
                        trader_r,
                        strategy.third_take_profit_pct,
                    ),
                ]

        targets: list[PlannedTakeProfit] = []

        previous_price: Decimal | None = None

        for (
            name,
            configured_r,
            close_pct,
        ) in rules:
            reward_distance = risk_distance * configured_r

            if plan.side is Side.LONG:
                raw_price = weighted_entry + reward_distance
            else:
                raw_price = weighted_entry - reward_distance

            price = self._round_price(
                raw_price,
                context.tick_size,
            )

            if price <= 0:
                raise ExecutionPlanningError("Derived take profit is not positive")

            if previous_price is not None:
                if plan.side is Side.LONG and price <= previous_price:
                    raise ExecutionPlanningError(
                        "Actual-fill V2 take-profit levels collapse after rounding"
                    )

                if plan.side is Side.SHORT and price >= previous_price:
                    raise ExecutionPlanningError(
                        "Actual-fill V2 take-profit levels collapse after rounding"
                    )

            targets.append(
                PlannedTakeProfit(
                    name=name,
                    price=price,
                    close_pct=close_pct,
                    r_multiple=configured_r,
                )
            )

            previous_price = price

        if len(targets) != 3:
            raise ExecutionPlanningError("Strategy V2 requires three fixed exits")

        target_tuple = (
            targets[0],
            targets[1],
            targets[2],
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

        payload = plan.model_dump()

        payload.update(
            {
                "orders": orders,
                "take_profit": (target_tuple[-1].price),
                "take_profit_targets": (target_tuple),
                "take_profit_source": (take_profit_source),
                "trader_take_profit": (
                    plan.trader_take_profit
                    or (
                        plan.take_profit
                        if plan.take_profit_source is TakeProfitSource.TRADER
                        else None
                    )
                ),
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
                EntrySpec(
                    name="E1",
                    order_type=ExecutionOrderType.LIMIT,
                    reference_price=prices[0],
                    risk_pct=weights[0],
                ),
                EntrySpec(
                    name="E2",
                    order_type=ExecutionOrderType.LIMIT,
                    reference_price=prices[1],
                    risk_pct=weights[1],
                ),
                EntrySpec(
                    name="E3",
                    order_type=ExecutionOrderType.LIMIT,
                    reference_price=prices[2],
                    risk_pct=weights[2],
                ),
            )

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

        return (
            EntrySpec(
                name="E1",
                order_type=primary_type,
                reference_price=primary_price,
                risk_pct=weights[0],
            ),
            EntrySpec(
                name="E2",
                order_type=(ExecutionOrderType.LIMIT),
                reference_price=second,
                risk_pct=weights[1],
            ),
            EntrySpec(
                name="E3",
                order_type=(ExecutionOrderType.LIMIT),
                reference_price=third,
                risk_pct=weights[2],
            ),
        )

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

        rules = list(strategy.exit_rules)

        source = TakeProfitSource.POLICY

        if intent.take_profit is not None:
            trader_tp = self._round_price(
                Decimal(str(intent.take_profit)),
                tick_size,
            )

            if intent.side is Side.LONG:
                trader_reward = trader_tp - weighted_entry
            else:
                trader_reward = weighted_entry - trader_tp

            trader_r = trader_reward / risk_distance

            if trader_r <= strategy.first_take_profit_r:
                raise ExecutionPlanningError(
                    "Trader take profit is too close for Strategy V2 partial exits"
                )

            if trader_r < strategy.third_take_profit_r:
                source = TakeProfitSource.TRADER

                middle_r = (strategy.first_take_profit_r + trader_r) / Decimal("2")

                rules = [
                    (
                        "TP1",
                        strategy.first_take_profit_r,
                        strategy.first_take_profit_pct,
                    ),
                    (
                        "TP2",
                        middle_r,
                        strategy.second_take_profit_pct,
                    ),
                    (
                        "TP3",
                        trader_r,
                        strategy.third_take_profit_pct,
                    ),
                ]

        targets: list[PlannedTakeProfit] = []

        previous_price: Decimal | None = None

        for (
            name,
            configured_r,
            close_pct,
        ) in rules:
            reward_distance = risk_distance * configured_r

            if intent.side is Side.LONG:
                raw_price = weighted_entry + reward_distance
            else:
                raw_price = weighted_entry - reward_distance

            price = self._round_price(
                raw_price,
                tick_size,
            )

            if price <= 0:
                raise ExecutionPlanningError("Derived take profit is not positive")

            if previous_price is not None:
                if intent.side is Side.LONG and price <= previous_price:
                    raise ExecutionPlanningError(
                        "V2 take-profit levels collapse after rounding"
                    )

                if intent.side is Side.SHORT and price >= previous_price:
                    raise ExecutionPlanningError(
                        "V2 take-profit levels collapse after rounding"
                    )

            targets.append(
                PlannedTakeProfit(
                    name=name,
                    price=price,
                    close_pct=close_pct,
                    # Keep the semantic Strategy V2
                    # target. The preview price is
                    # tick-rounded and may be rebuilt
                    # later from the actual live
                    # average entry.
                    r_multiple=configured_r,
                )
            )

            previous_price = price

        if len(targets) != 3:
            raise ExecutionPlanningError("Strategy V2 requires three fixed exits")

        return (
            (
                targets[0],
                targets[1],
                targets[2],
            ),
            source,
        )

    @classmethod
    def _validate_initial_exit_capacity(
        cls,
        side: Side,
        primary_order: PlannedOrder,
        stop_loss: Decimal,
        targets: tuple[
            PlannedTakeProfit,
            PlannedTakeProfit,
            PlannedTakeProfit,
        ],
        context: InstrumentContext,
    ) -> None:
        position_quantity = primary_order.quantity

        entry_price = primary_order.reference_price

        risk_distance = abs(entry_price - stop_loss)

        placeable_quantity = Decimal("0")
        placeable_count = 0

        total_exit_pct = Decimal("0")

        for target in targets:
            total_exit_pct += target.close_pct

            quantity = cls._round_down(
                (position_quantity * target.close_pct / Decimal("100")),
                context.qty_step,
            )

            if quantity <= 0 or quantity < context.min_qty:
                continue

            reward_distance = risk_distance * target.r_multiple

            if side is Side.LONG:
                raw_price = entry_price + reward_distance
            else:
                raw_price = entry_price - reward_distance

            price = cls._round_price(
                raw_price,
                context.tick_size,
            )

            if context.min_notional and (quantity * price < context.min_notional):
                continue

            placeable_count += 1
            placeable_quantity += quantity

        runner_quantity = position_quantity - placeable_quantity

        if placeable_count == 0:
            raise ExecutionPlanningError(
                "Primary V2 entry is too small to support any fixed partial exit"
            )

        if runner_quantity <= 0 or runner_quantity < context.min_qty:
            raise ExecutionPlanningError(
                "Primary V2 entry is too small to preserve a runner after fixed exits"
            )

        if total_exit_pct >= Decimal("100"):
            raise ExecutionPlanningError(
                "Strategy V2 fixed exits leave no configured runner"
            )

    @staticmethod
    def _validate_entry_geometry(
        side: Side,
        reference_prices: tuple[
            Decimal,
            ...,
        ],
        stop_loss: Decimal,
    ) -> None:
        if side is Side.LONG:
            if any(price <= stop_loss for price in reference_prices):
                raise ExecutionPlanningError("Rounded LONG stop geometry is invalid")

            if not (reference_prices[0] > reference_prices[1] > reference_prices[2]):
                raise ExecutionPlanningError(
                    "LONG V2 entry levels must move toward the stop"
                )

        else:
            if any(price >= stop_loss for price in reference_prices):
                raise ExecutionPlanningError("Rounded SHORT stop geometry is invalid")

            if not (reference_prices[0] < reference_prices[1] < reference_prices[2]):
                raise ExecutionPlanningError(
                    "SHORT V2 entry levels must move toward the stop"
                )

    @staticmethod
    def _validate_target_geometry(
        side: Side,
        weighted_entry: Decimal,
        take_profits: tuple[
            Decimal,
            ...,
        ],
    ) -> None:
        if side is Side.LONG:
            if any(target <= weighted_entry for target in take_profits):
                raise ExecutionPlanningError("LONG V2 take-profit geometry is invalid")

        else:
            if any(target >= weighted_entry for target in take_profits):
                raise ExecutionPlanningError("SHORT V2 take-profit geometry is invalid")

    @staticmethod
    def _validate_not_passed_through(
        intent: TradingIntent,
        reference_prices: tuple[
            Decimal,
            ...,
        ],
        market_price: Decimal,
    ) -> None:
        if intent.entry.type is EntryType.MARKET:
            return

        primary = reference_prices[0]

        if intent.side is Side.LONG and market_price < primary:
            raise ExecutionPlanningError(
                "Market is already below the primary LONG entry"
            )

        if intent.side is Side.SHORT and market_price > primary:
            raise ExecutionPlanningError(
                "Market is already above the primary SHORT entry"
            )

    @staticmethod
    def _round_price(
        value: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        if tick_size <= 0:
            raise ExecutionPlanningError("Bybit tick size must be positive")

        return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size

    @staticmethod
    def _round_down(
        value: Decimal,
        step: Decimal,
    ) -> Decimal:
        if step <= 0:
            raise ExecutionPlanningError("Bybit quantity step must be positive")

        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
