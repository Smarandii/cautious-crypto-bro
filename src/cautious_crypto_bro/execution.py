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
    TakeProfitSource,
    TradingIntent,
)

MAX_BYBIT_BATCH_ORDERS = 20


class ExecutionPlanningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InstrumentContext:
    market_price: Decimal
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal


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

        order_type, reference_prices = (
            self._entry_prices(
                intent,
                policy,
                context,
            )
        )

        (
            take_profit_targets,
            take_profit_source,
        ) = self._take_profit_targets(
            intent,
            policy,
            reference_prices,
            stop_loss,
            context.tick_size,
        )

        self._validate_geometry(
            intent.side,
            reference_prices,
            stop_loss,
            tuple(
                target.price
                for target
                in take_profit_targets
            ),
        )

        self._validate_not_passed_through(
            intent,
            reference_prices,
            context.market_price,
        )

        planned_order_count = (
            len(reference_prices)
            * len(take_profit_targets)
        )

        if (
            planned_order_count
            > MAX_BYBIT_BATCH_ORDERS
        ):
            raise ExecutionPlanningError(
                "Execution plan would create "
                f"{planned_order_count} Bybit orders; "
                f"maximum supported in one batch is "
                f"{MAX_BYBIT_BATCH_ORDERS}. "
                "Reduce range_order_count."
            )

        total_stop_distance = sum(
            (
                abs(
                    price
                    - stop_loss
                )
                for price
                in reference_prices
            ),
            Decimal("0"),
        )

        if total_stop_distance <= 0:
            raise ExecutionPlanningError(
                "Entry-to-stop distance "
                "must be positive"
            )

        raw_base_qty = (
            policy.risk_budget_usdt
            / total_stop_distance
        )

        base_qty = self._round_down(
            raw_base_qty,
            context.qty_step,
        )

        if (
            base_qty <= 0
            or base_qty < context.min_qty
        ):
            raise ExecutionPlanningError(
                "Configured risk budget is too small "
                "for Bybit minimum quantity"
            )

        orders: list[
            PlannedOrder
        ] = []

        for price in reference_prices:
            allocations = (
                self._split_quantity(
                    base_qty,
                    take_profit_targets,
                    context.qty_step,
                )
            )

            for (
                target,
                quantity,
            ) in zip(
                take_profit_targets,
                allocations,
                strict=True,
            ):
                if (
                    quantity <= 0
                    or quantity
                    < context.min_qty
                ):
                    raise ExecutionPlanningError(
                        "TP ladder creates a child order "
                        "below Bybit minimum quantity. "
                        "Increase risk/capital, reduce "
                        "entry orders, or adjust TP shares."
                    )

                if (
                    context.min_notional
                    and (
                        quantity
                        * price
                    )
                    < context.min_notional
                ):
                    raise ExecutionPlanningError(
                        "TP ladder creates a child order "
                        "below Bybit minimum notional. "
                        "Increase risk/capital, reduce "
                        "entry orders, or adjust TP shares."
                    )

                orders.append(
                    PlannedOrder(
                        order_type=order_type,
                        quantity=quantity,
                        price=(
                            price
                            if (
                                order_type
                                is ExecutionOrderType.LIMIT
                            )
                            else None
                        ),
                        reference_price=price,
                        take_profit=(
                            target.price
                        ),
                    )
                )

        planned_max_loss = sum(
            (
                order.quantity
                * abs(
                    order.reference_price
                    - stop_loss
                )
                for order
                in orders
            ),
            Decimal("0"),
        )

        return ExecutionPlan(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            orders=tuple(orders),
            stop_loss=stop_loss,
            take_profit=(
                take_profit_targets[-1].price
            ),
            take_profit_targets=(
                take_profit_targets
            ),
            take_profit_source=(
                take_profit_source
            ),
            policy=policy.model_copy(
                deep=True
            ),
            planned_max_loss_usdt=(
                planned_max_loss
            ),
        )

    def _take_profit_targets(
        self,
        intent: TradingIntent,
        policy: ExecutionPolicy,
        reference_prices: tuple[
            Decimal,
            ...,
        ],
        stop_loss: Decimal,
        tick_size: Decimal,
    ) -> tuple[
        tuple[PlannedTakeProfit, ...],
        TakeProfitSource,
    ]:
        reference_entry = (
            sum(
                reference_prices,
                Decimal("0"),
            )
            / Decimal(
                len(reference_prices)
            )
        )

        risk_distance = abs(
            reference_entry
            - stop_loss
        )

        if risk_distance <= 0:
            raise ExecutionPlanningError(
                "Reference entry and stop "
                "must be different"
            )

        if intent.take_profit is not None:
            price = self._round_price(
                Decimal(
                    str(
                        intent.take_profit
                    )
                ),
                tick_size,
            )

            actual_r = (
                abs(
                    price
                    - reference_entry
                )
                / risk_distance
            )

            return (
                (
                    PlannedTakeProfit(
                        name="TRADER",
                        price=price,
                        close_pct=(
                            Decimal("100")
                        ),
                        r_multiple=actual_r,
                    ),
                ),
                TakeProfitSource.TRADER,
            )

        exit_policy = (
            policy.exit_policy
        )

        minimum_reward = (
            reference_entry
            * exit_policy.minimum_reward_bps
            / Decimal("10000")
        )

        targets: list[
            PlannedTakeProfit
        ] = []

        previous_price: (
            Decimal | None
        ) = None

        for (
            name,
            configured_r,
            close_pct,
        ) in exit_policy.rules:
            reward_distance = max(
                risk_distance
                * configured_r,
                minimum_reward,
            )

            if intent.side is Side.LONG:
                raw_price = (
                    reference_entry
                    + reward_distance
                )
            else:
                raw_price = (
                    reference_entry
                    - reward_distance
                )

            price = self._round_price(
                raw_price,
                tick_size,
            )

            if previous_price is not None:
                if (
                    intent.side is Side.LONG
                    and price
                    <= previous_price
                ):
                    price = (
                        previous_price
                        + tick_size
                    )

                if (
                    intent.side is Side.SHORT
                    and price
                    >= previous_price
                ):
                    price = (
                        previous_price
                        - tick_size
                    )

            if price <= 0:
                raise ExecutionPlanningError(
                    "Derived take profit "
                    "is not positive"
                )

            actual_r = (
                abs(
                    price
                    - reference_entry
                )
                / risk_distance
            )

            targets.append(
                PlannedTakeProfit(
                    name=name,
                    price=price,
                    close_pct=close_pct,
                    r_multiple=actual_r,
                )
            )

            previous_price = price

        return (
            tuple(targets),
            TakeProfitSource.POLICY,
        )

    def _entry_prices(
        self,
        intent: TradingIntent,
        policy: ExecutionPolicy,
        context: InstrumentContext,
    ) -> tuple[
        ExecutionOrderType,
        tuple[Decimal, ...],
    ]:
        entry = intent.entry

        if entry.type is EntryType.MARKET:
            return (
                ExecutionOrderType.MARKET,
                (context.market_price,),
            )

        if entry.type is EntryType.LIMIT:
            assert entry.price is not None

            price = self._round_price(
                Decimal(
                    str(entry.price)
                ),
                context.tick_size,
            )

            return (
                ExecutionOrderType.LIMIT,
                (price,),
            )

        assert (
            entry.range_low
            is not None
        )
        assert (
            entry.range_high
            is not None
        )

        low = Decimal(
            str(entry.range_low)
        )
        high = Decimal(
            str(entry.range_high)
        )

        count = (
            policy.range_order_count
        )

        if count == 1:
            raw_prices = (
                (
                    low + high
                )
                / Decimal("2"),
            )
        else:
            spacing = (
                high - low
            ) / Decimal(
                count - 1
            )

            raw_prices = tuple(
                low
                + spacing * index
                for index
                in range(count)
            )

        prices = tuple(
            self._round_price(
                price,
                context.tick_size,
            )
            for price in raw_prices
        )

        if len(set(prices)) != count:
            raise ExecutionPlanningError(
                "Entry range is too narrow for "
                "the configured number of distinct "
                "tick-aligned limit orders"
            )

        return (
            ExecutionOrderType.LIMIT,
            prices,
        )

    @staticmethod
    def _split_quantity(
        base_qty: Decimal,
        targets: tuple[
            PlannedTakeProfit,
            ...,
        ],
        qty_step: Decimal,
    ) -> tuple[
        Decimal,
        ...,
    ]:
        if len(targets) == 1:
            return (
                base_qty,
            )

        allocations: list[
            Decimal
        ] = []

        allocated = Decimal("0")

        for target in targets[:-1]:
            quantity = (
                ExecutionPlanner
                ._round_down(
                    base_qty
                    * target.close_pct
                    / Decimal("100"),
                    qty_step,
                )
            )

            allocations.append(
                quantity
            )

            allocated += quantity

        allocations.append(
            base_qty
            - allocated
        )

        return tuple(
            allocations
        )

    @staticmethod
    def _validate_geometry(
        side: Side,
        reference_prices: tuple[
            Decimal,
            ...,
        ],
        stop_loss: Decimal,
        take_profits: tuple[
            Decimal,
            ...,
        ],
    ) -> None:
        low = min(
            reference_prices
        )
        high = max(
            reference_prices
        )

        if side is Side.LONG:
            if not stop_loss < low:
                raise ExecutionPlanningError(
                    "Rounded LONG stop geometry "
                    "is invalid"
                )

            if any(
                target <= high
                for target
                in take_profits
            ):
                raise ExecutionPlanningError(
                    "Rounded LONG take-profit "
                    "geometry is invalid"
                )

        else:
            if not high < stop_loss:
                raise ExecutionPlanningError(
                    "Rounded SHORT stop geometry "
                    "is invalid"
                )

            if any(
                target >= low
                for target
                in take_profits
            ):
                raise ExecutionPlanningError(
                    "Rounded SHORT take-profit "
                    "geometry is invalid"
                )

    @staticmethod
    def _validate_not_passed_through(
        intent: TradingIntent,
        reference_prices: tuple[
            Decimal,
            ...,
        ],
        market_price: Decimal,
    ) -> None:
        if (
            intent.entry.type
            is EntryType.MARKET
        ):
            return

        if (
            intent.side is Side.LONG
            and market_price
            < min(reference_prices)
        ):
            raise ExecutionPlanningError(
                "Market is already below "
                "the LONG entry area"
            )

        if (
            intent.side is Side.SHORT
            and market_price
            > max(reference_prices)
        ):
            raise ExecutionPlanningError(
                "Market is already above "
                "the SHORT entry area"
            )

    @staticmethod
    def _round_price(
        value: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        if tick_size <= 0:
            raise ExecutionPlanningError(
                "Bybit tick size must be positive"
            )

        return (
            (
                value / tick_size
            ).to_integral_value(
                rounding=ROUND_HALF_UP
            )
            * tick_size
        )

    @staticmethod
    def _round_down(
        value: Decimal,
        step: Decimal,
    ) -> Decimal:
        if step <= 0:
            raise ExecutionPlanningError(
                "Bybit quantity step "
                "must be positive"
            )

        return (
            (
                value / step
            ).to_integral_value(
                rounding=ROUND_DOWN
            )
            * step
        )
