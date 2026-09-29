from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from ..domain import Side
from ._helpers import _round_price

if TYPE_CHECKING:
    from ..domain import AccountPosition, ExecutionPlan, InstrumentContext
    from ..ports import AccountGateway


class TrailingStopManager:
    def __init__(
        self,
        *,
        executor: AccountGateway,
    ) -> None:
        self._executor = executor

    @staticmethod
    def _verify_protection(
        position: AccountPosition,
        stop_loss: Decimal,
        trailing_distance: Decimal | None,
    ) -> None:
        if position.stop_loss != stop_loss:
            raise RuntimeError(
                "Bybit did not confirm expected "
                "position stop: "
                f"expected={stop_loss}, "
                f"live={position.stop_loss}"
            )

        if (
            trailing_distance is not None
            and position.trailing_stop != trailing_distance
        ):
            raise RuntimeError(
                "Bybit did not confirm expected "
                "trailing distance: "
                f"expected={trailing_distance}, "
                f"live={position.trailing_stop}"
            )

    @staticmethod
    def _protected_stop(
        plan: ExecutionPlan,
        position: AccountPosition,
        context: InstrumentContext,
        *,
        previous: Decimal | None,
    ) -> Decimal:
        risk_distance = abs(position.avg_price - plan.stop_loss)

        anchor = position.break_even_price or position.avg_price

        buffer = risk_distance * plan.policy.strategy_v2.minimum_locked_profit_r

        if position.side is Side.LONG:
            raw = max(
                plan.stop_loss,
                anchor + buffer,
            )

            if previous is not None:
                raw = max(
                    raw,
                    previous,
                )

            result = (raw / context.tick_size).to_integral_value(
                rounding=ROUND_CEILING
            ) * context.tick_size

            if result >= position.mark_price:
                raise RuntimeError("Protected LONG stop would not be below live price")

        else:
            raw = min(
                plan.stop_loss,
                anchor - buffer,
            )

            if previous is not None:
                raw = min(
                    raw,
                    previous,
                )

            result = (raw / context.tick_size).to_integral_value(
                rounding=ROUND_FLOOR
            ) * context.tick_size

            if result <= 0 or result <= position.mark_price:
                raise RuntimeError("Protected SHORT stop would not be above live price")

        return result

    @staticmethod
    def _trailing_distance(
        plan: ExecutionPlan,
        position: AccountPosition,
        context: InstrumentContext,
    ) -> Decimal:
        distance = abs(position.avg_price - plan.stop_loss)

        raw = distance * plan.policy.strategy_v2.trailing_distance_r

        result = _round_price(
            raw,
            context.tick_size,
        )

        if result <= 0:
            raise RuntimeError("Trailing distance rounded to zero")

        return result
