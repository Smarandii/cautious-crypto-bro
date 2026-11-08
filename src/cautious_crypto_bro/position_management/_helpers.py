from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from ..domain import Side
from ..execution_geometry import (
    round_down as _round_down,
)
from ..execution_geometry import (
    round_price as _round_price,
)

__all__ = ["_find_position", "_matches_exit", "_round_down", "_round_price"]

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
