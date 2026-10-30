"""Allocate exchange requests without changing the three logical risk legs."""

from decimal import Decimal, InvalidOperation

from ..domain import EntryPreflightError, EntrySlice, ExecutionOrderType, ExecutionPlan


def _number(lot: dict, name: str, *, zero: bool = False) -> Decimal:
    try:
        value = Decimal(str(lot[name]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise EntryPreflightError(f"Invalid instrument {name}") from exc
    if not value.is_finite() or value < 0 or (not zero and value == 0):
        raise EntryPreflightError(f"Invalid instrument {name}")
    return value


def allocate_entry_slices(plan: ExecutionPlan, instrument: dict) -> ExecutionPlan:
    lot = instrument.get("lotSizeFilter", {})
    step = _number(lot, "qtyStep")
    minimum = _number(lot, "minOrderQty")
    notional = _number(lot, "minNotionalValue", zero=True)
    market_max = _number(lot, "maxMktOrderQty")
    limit_max = _number(lot, "maxOrderQty")
    children: list[EntrySlice] = []
    for order in plan.orders:
        confirmed = tuple(
            s
            for s in plan.entry_slices
            if s.leg_name == order.name and s.filled_quantity is not None
        )
        if confirmed:
            children.extend(confirmed)
            continue
        ceiling = (
            market_max if order.order_type is ExecutionOrderType.MARKET else limit_max
        )
        ticks = order.quantity / step
        max_ticks = int(ceiling // step)
        if ticks != ticks.to_integral_value() or max_ticks <= 0:
            raise EntryPreflightError(
                "Entry quantity cannot satisfy exchange step/ceiling"
            )
        total = int(ticks)
        count = (total + max_ticks - 1) // max_ticks
        # Every physical entry carries a native partial SL. Bybit 110061 caps
        # Partial TP/SL at 20; never admit entries we cannot keep protected.
        if count <= 0 or len(children) + count > 20:
            raise EntryPreflightError(
                "Entry allocation exceeds native partial-stop capacity (20)"
            )
        # Balance steps rather than leaving a final child below exchange minima.
        base, extra = divmod(total, count)
        for index in range(count):
            quantity = step * (base + (index < extra))
            if quantity < minimum or quantity * order.reference_price < notional:
                raise EntryPreflightError("Entry slices cannot satisfy exchange minima")
            children.append(
                EntrySlice.model_validate(
                    {
                        "leg_name": order.name,
                        "index": 0 if count == 1 else index + 1,
                        "quantity": quantity,
                    }
                )
            )
    mapped = tuple(children)
    return ExecutionPlan.model_validate(plan.model_dump() | {"entry_slices": mapped})
