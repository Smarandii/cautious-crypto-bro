from __future__ import annotations

import hashlib
import json
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

from ..domain import (
    AccountOrder,
    ClosedPnlRecord,
    Side,
    TradeExecutionError,
)


def parse_decimal(value: object) -> Decimal:
    raw = str(value if value is not None else "0").strip()

    if not raw:
        return Decimal("0")

    return Decimal(raw)


def parse_optional_decimal(value: object) -> Decimal | None:
    parsed = parse_decimal(value)

    return parsed if parsed != 0 else None


def parse_side(side: object) -> Side:
    if side == "Buy":
        return Side.LONG

    if side == "Sell":
        return Side.SHORT

    raise TradeExecutionError(f"Unexpected Bybit position/order side: {side!r}")


def format_decimal(value: Decimal) -> str:
    return format(
        value.normalize(),
        "f",
    )


def closed_pnl_record_from_item(item: dict) -> ClosedPnlRecord:
    symbol = str(item.get("symbol") or "").upper()

    order_id = str(item.get("orderId") or "")

    updated_ms = int(item.get("updatedTime") or item.get("createdTime") or 0)

    if not symbol:
        raise TradeExecutionError("Closed-PnL record has no symbol")

    if updated_ms <= 0:
        raise TradeExecutionError("Closed-PnL record has no timestamp")

    if order_id:
        record_id = f"{symbol}:{order_id}"

    else:
        # Some non-standard settlement records
        # may not carry an orderId. Build a
        # deterministic identifier rather than
        # silently dropping realized P&L.
        identity = {
            "symbol": symbol,
            "side": item.get("side"),
            "execType": item.get("execType"),
            "closedSize": (item.get("closedSize")),
            "closedPnl": (item.get("closedPnl")),
            "avgEntryPrice": (item.get("avgEntryPrice")),
            "avgExitPrice": (item.get("avgExitPrice")),
            "updatedTime": updated_ms,
        }

        digest = hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        record_id = f"{symbol}:synthetic:{digest}"

    close_side = str(item.get("side") or "")

    if close_side == "Sell":
        position_side = Side.LONG
    elif close_side == "Buy":
        position_side = Side.SHORT
    else:
        raise TradeExecutionError("Closed-PnL record has invalid side")

    return ClosedPnlRecord(
        record_id=record_id,
        order_id=order_id,
        symbol=symbol,
        position_side=position_side,
        closed_pnl=parse_decimal(item.get("closedPnl")),
        closed_size=parse_decimal(item.get("closedSize")),
        avg_entry_price=(parse_optional_decimal(item.get("avgEntryPrice"))),
        avg_exit_price=(parse_optional_decimal(item.get("avgExitPrice"))),
        updated_at=datetime.fromtimestamp(
            updated_ms / 1000,
            tz=UTC,
        ),
    )


def account_order_from_item(item: dict) -> AccountOrder:
    updated_ms = int(item.get("updatedTime") or item.get("createdTime") or 0)

    return AccountOrder(
        symbol=str(item.get("symbol") or "").upper(),
        side=parse_side(item.get("side")),
        order_type=str(item.get("orderType") or "Unknown"),
        status=str(item.get("orderStatus") or "Unknown"),
        quantity=parse_decimal(item.get("qty")),
        remaining_quantity=(
            parse_decimal(item.get("leavesQty"))
            or (
                parse_decimal(item.get("qty"))
                if (
                    item.get("stopOrderType") == "PartialStopLoss"
                    and item.get("orderStatus") == "Untriggered"
                )
                else Decimal("0")
            )
        ),
        price=parse_optional_decimal(item.get("price")),
        avg_price=(parse_optional_decimal(item.get("avgPrice"))),
        order_id=str(item.get("orderId") or ""),
        order_link_id=str(item.get("orderLinkId") or ""),
        reduce_only=(
            item.get("reduceOnly") is True
            or str(item.get("reduceOnly")).casefold() == "true"
        ),
        updated_at=datetime.fromtimestamp(
            updated_ms / 1000,
            tz=UTC,
        ),
        stop_order_type=str(item.get("stopOrderType") or ""),
        create_type=str(item.get("createType") or ""),
        trigger_price=(parse_optional_decimal(item.get("triggerPrice"))),
        close_on_trigger=(
            item.get("closeOnTrigger") is True
            or str(item.get("closeOnTrigger")).casefold() == "true"
        ),
        parent_order_link_id=str(item.get("parentOrderLinkId") or ""),
        executed_quantity=parse_decimal(item.get("cumExecQty")),
    )
