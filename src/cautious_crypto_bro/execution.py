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
    Side,
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
        take_profit = self._round_price(
            Decimal(str(intent.take_profit)),
            context.tick_size,
        )

        order_type, reference_prices = (
            self._entry_prices(
                intent,
                policy,
                context,
            )
        )

        self._validate_geometry(
            intent.side,
            reference_prices,
            stop_loss,
            take_profit,
        )

        self._validate_not_passed_through(
            intent,
            reference_prices,
            context.market_price,
        )

        total_stop_distance = sum(
            (
                abs(price - stop_loss)
                for price in reference_prices
            ),
            Decimal("0"),
        )

        if total_stop_distance <= 0:
            raise ExecutionPlanningError(
                "Entry-to-stop distance "
                "must be positive"
            )

        raw_qty = (
            policy.risk_budget_usdt
            / total_stop_distance
        )

        quantity = self._round_down(
            raw_qty,
            context.qty_step,
        )

        if (
            quantity <= 0
            or quantity < context.min_qty
        ):
            raise ExecutionPlanningError(
                "Configured risk budget is too small "
                "for Bybit minimum quantity"
            )

        for price in reference_prices:
            if (
                context.min_notional
                and quantity * price
                < context.min_notional
            ):
                raise ExecutionPlanningError(
                    "Configured risk budget is too small "
                    "for Bybit minimum notional"
                )

        planned_max_loss = (
            quantity
            * total_stop_distance
        )

        orders = tuple(
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
            )
            for price in reference_prices
        )

        return ExecutionPlan(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            orders=orders,
            stop_loss=stop_loss,
            take_profit=take_profit,
            policy=policy.model_copy(
                deep=True
            ),
            planned_max_loss_usdt=(
                planned_max_loss
            ),
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
                Decimal(str(entry.price)),
                context.tick_size,
            )

            return (
                ExecutionOrderType.LIMIT,
                (price,),
            )

        assert entry.range_low is not None
        assert entry.range_high is not None

        low = Decimal(
            str(entry.range_low)
        )
        high = Decimal(
            str(entry.range_high)
        )

        count = policy.range_order_count

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
                for index in range(count)
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
    def _validate_geometry(
        side: Side,
        reference_prices: tuple[
            Decimal,
            ...,
        ],
        stop_loss: Decimal,
        take_profit: Decimal,
    ) -> None:
        low = min(reference_prices)
        high = max(reference_prices)

        if (
            side is Side.LONG
            and not (
                stop_loss
                < low
                <= high
                < take_profit
            )
        ):
            raise ExecutionPlanningError(
                "Rounded LONG geometry is invalid"
            )

        if (
            side is Side.SHORT
            and not (
                take_profit
                < low
                <= high
                < stop_loss
            )
        ):
            raise ExecutionPlanningError(
                "Rounded SHORT geometry is invalid"
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
