from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_DOWN,
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
        stop_loss = self._round_price(
            Decimal(str(intent.stop_loss)),
            context.tick_size,
        )

        strategy = policy.strategy_v2

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

        return ExecutionPlan(
            strategy_version=2,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            orders=orders,
            stop_loss=stop_loss,
            take_profit=(take_profit_targets[-1].price),
            take_profit_targets=(take_profit_targets),
            take_profit_source=(take_profit_source),
            runner_pct=(strategy.runner_pct),
            policy=policy.model_copy(deep=True),
            planned_max_loss_usdt=(planned_max_loss),
        )

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
