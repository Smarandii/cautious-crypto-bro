from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from ..domain import Side

if TYPE_CHECKING:
    from ..domain import (
        AccountOrder,
        AccountPosition,
        AccountStateSummary,
        PositionStrategy,
    )


def _matches_exit(
    order: AccountOrder, position_side: Side, quantity: Decimal, price: Decimal
) -> bool:
    expected_side = Side.SHORT if position_side is Side.LONG else Side.LONG
    return (
        order.side is expected_side
        and order.reduce_only
        and order.order_type == "Limit"
        and order.quantity == quantity
        and order.status in {"New", "PartiallyFilled", "Filled"}
        and order.price == price
    )


def _find_position(
    state: PositionStrategy,
    account: AccountStateSummary,
) -> AccountPosition | None:
    positions = tuple(
        position for position in account.positions if position.symbol == state.symbol
    )

    if not positions:
        return None

    if len(positions) != 1:
        raise RuntimeError(f"Expected one live position for {state.symbol}")

    return positions[0]


def _round_price(
    value: Decimal,
    tick_size: Decimal,
) -> Decimal:
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def _round_down(
    value: Decimal,
    step: Decimal,
) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
