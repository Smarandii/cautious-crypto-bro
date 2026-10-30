"""Verify terminal entry requests against captured exchange orders and fills."""

from decimal import Decimal, InvalidOperation


def entry_orders_settled(
    expected: list[dict],
    symbol: str,
    side: str,
    history: list[dict] | None,
    current: list[dict] | None,
    fills: dict[str, list[dict]],
) -> bool:
    if not expected or history is None or current is None:
        return False
    current_ids = {str(order.get("orderId") or "") for order in current}
    expected_ids = {item["order_id"] for item in expected}
    records = {}
    for order in history:
        order_id = str(order.get("orderId") or "")
        if order_id not in expected_ids:
            continue
        if order_id in records and records[order_id] != order:
            return False
        records[order_id] = order
    return all(
        item["order_id"] not in current_ids
        and _settled_entry_order(
            item,
            records.get(item["order_id"], {}),
            fills.get(item["order_id"], []),
            symbol,
            side,
        )
        for item in expected
    )


def _settled_entry_order(
    expected: dict, order: dict, fills: list[dict], symbol: str, side: str
) -> bool:
    if order.get("orderStatus") not in {
        "Filled",
        "Cancelled",
        "Rejected",
        "Deactivated",
        "PartiallyFilledCanceled",
    }:
        return False
    if (
        order.get("orderLinkId") != expected["order_link_id"]
        or order.get("symbol") != symbol
        or order.get("side") != side
        or order.get("reduceOnly") is not False
    ):
        return False
    try:
        quantity = Decimal(str(order["qty"]))
        executed = Decimal(str(order["cumExecQty"]))
        if (
            not quantity.is_finite()
            or not executed.is_finite()
            or quantity != Decimal(expected["quantity"])
            or not 0 <= executed <= quantity
        ):
            return False
        total = Decimal("0")
        for fill in fills:
            size = Decimal(str(fill["execQty"]))
            fee = Decimal(str(fill["execFee"]))
            if (
                not size.is_finite()
                or size <= 0
                or not fee.is_finite()
                or Decimal(str(fill.get("closedSize", "0"))) != 0
                or fill.get("symbol") != symbol
                or fill.get("side") != side
                or fill.get("orderId") != expected["order_id"]
                or fill.get("orderLinkId") != expected["order_link_id"]
            ):
                return False
            total += size
        return total == executed
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
