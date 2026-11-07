from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .domain import (
    EntryType,
    InstrumentContext,
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
class TakeProfitRequest:
    """Inputs for deriving the three Strategy V2 fixed exits."""

    side: Side
    strategy: StrategyV2Policy
    weighted_entry: Decimal
    risk_distance: Decimal
    tick_size: Decimal
    trader_take_profit: Decimal | None
    trader_target_error: str
    collapsed_targets_error: str


def round_price(value: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= 0:
        raise ExecutionPlanningError("Bybit tick size must be positive")
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ExecutionPlanningError("Bybit quantity step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def trader_capped_rules(
    request: TakeProfitRequest,
) -> tuple[list[tuple[str, Decimal, Decimal]], TakeProfitSource]:
    strategy = request.strategy
    trader_tp = request.trader_take_profit
    assert trader_tp is not None

    if request.side is Side.LONG:
        trader_reward = trader_tp - request.weighted_entry
    else:
        trader_reward = request.weighted_entry - trader_tp

    trader_r = trader_reward / request.risk_distance
    if trader_r <= strategy.first_take_profit_r:
        raise ExecutionPlanningError(request.trader_target_error)
    if trader_r >= strategy.third_take_profit_r:
        return list(strategy.exit_rules), TakeProfitSource.POLICY

    middle_r = (strategy.first_take_profit_r + trader_r) / Decimal("2")
    return (
        [
            ("TP1", strategy.first_take_profit_r, strategy.first_take_profit_pct),
            ("TP2", middle_r, strategy.second_take_profit_pct),
            ("TP3", trader_r, strategy.third_take_profit_pct),
        ],
        TakeProfitSource.TRADER,
    )


def derived_take_profits(
    request: TakeProfitRequest,
    rules: list[tuple[str, Decimal, Decimal]],
) -> list[PlannedTakeProfit]:
    targets: list[PlannedTakeProfit] = []
    previous_price: Decimal | None = None

    for name, configured_r, close_pct in rules:
        reward_distance = request.risk_distance * configured_r
        if request.side is Side.LONG:
            raw_price = request.weighted_entry + reward_distance
        else:
            raw_price = request.weighted_entry - reward_distance

        price = round_price(raw_price, request.tick_size)
        if price <= 0:
            raise ExecutionPlanningError("Derived take profit is not positive")

        if previous_price is not None:
            if request.side is Side.LONG and price <= previous_price:
                raise ExecutionPlanningError(request.collapsed_targets_error)
            if request.side is Side.SHORT and price >= previous_price:
                raise ExecutionPlanningError(request.collapsed_targets_error)

        targets.append(
            PlannedTakeProfit(
                name=name,
                price=price,
                close_pct=close_pct,
                r_multiple=configured_r,
            )
        )
        previous_price = price

    return targets


def build_take_profit_targets(
    request: TakeProfitRequest,
) -> tuple[
    tuple[PlannedTakeProfit, PlannedTakeProfit, PlannedTakeProfit],
    TakeProfitSource,
]:
    rules = list(request.strategy.exit_rules)
    source = TakeProfitSource.POLICY
    if request.trader_take_profit is not None:
        rules, source = trader_capped_rules(request)

    targets = derived_take_profits(request, rules)
    if len(targets) != 3:
        raise ExecutionPlanningError("Strategy V2 requires three fixed exits")

    return (targets[0], targets[1], targets[2]), source


def validate_initial_exit_capacity(
    side: Side,
    primary_order: PlannedOrder,
    stop_loss: Decimal,
    targets: tuple[PlannedTakeProfit, PlannedTakeProfit, PlannedTakeProfit],
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
        quantity = round_down(
            position_quantity * target.close_pct / Decimal("100"),
            context.qty_step,
        )
        if quantity <= 0 or quantity < context.min_qty:
            continue

        reward_distance = risk_distance * target.r_multiple
        raw_price = (
            entry_price + reward_distance
            if side is Side.LONG
            else entry_price - reward_distance
        )
        price = round_price(raw_price, context.tick_size)

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


def validate_entry_geometry(
    side: Side,
    reference_prices: tuple[Decimal, ...],
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


def validate_target_geometry(
    side: Side,
    weighted_entry: Decimal,
    take_profits: tuple[Decimal, ...],
) -> None:
    if side is Side.LONG:
        if any(target <= weighted_entry for target in take_profits):
            raise ExecutionPlanningError("LONG V2 take-profit geometry is invalid")
    else:
        if any(target >= weighted_entry for target in take_profits):
            raise ExecutionPlanningError("SHORT V2 take-profit geometry is invalid")


def validate_not_passed_through(
    intent: TradingIntent,
    reference_prices: tuple[Decimal, ...],
    market_price: Decimal,
) -> None:
    if intent.entry.type is EntryType.MARKET:
        return
    primary = reference_prices[0]
    if intent.side is Side.LONG and market_price < primary:
        raise ExecutionPlanningError("Market is already below the primary LONG entry")
    if intent.side is Side.SHORT and market_price > primary:
        raise ExecutionPlanningError("Market is already above the primary SHORT entry")
