from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain import AccountPosition, AccountStateSummary, PositionStrategy


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
