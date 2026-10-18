from __future__ import annotations

import hashlib
import json
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal, InvalidOperation

from ..domain import (
    AccountOrder,
    AccountPosition,
    ClosedPnlRecord,
    OpenOrderExposure,
    PositionExposure,
    Side,
    TradeExecutionError,
)


def parse_decimal(value: object) -> Decimal:
    raw = str(value if value is not None else "0").strip()

    if not raw:
        return Decimal("0")

    parsed = Decimal(raw)
    if not parsed.is_finite():
        raise TradeExecutionError("Bybit numeric value must be finite")
    return parsed


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


def _required_quantity(value: object, name: str) -> Decimal:
    if value is None or not str(value).strip():
        raise TradeExecutionError(f"Bybit {name} is missing")
    try:
        quantity = Decimal(str(value))
    except InvalidOperation as exc:
        raise TradeExecutionError(f"Bybit {name} is invalid") from exc
    if not quantity.is_finite() or quantity < 0:
        raise TradeExecutionError(f"Bybit {name} must be finite and nonnegative")
    return quantity


def account_position_from_item(item: dict) -> AccountPosition | None:
    size = _required_quantity(item.get("size"), "position size")

    if size <= 0:
        return None

    symbol = item.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise TradeExecutionError("Active Bybit position contains no symbol")
    symbol = symbol.strip().upper()

    avg_price = parse_decimal(item.get("avgPrice"))

    mark_price = parse_decimal(item.get("markPrice"))

    if avg_price <= 0 or mark_price <= 0:
        raise TradeExecutionError("Active Bybit position contains invalid pricing")
    stop_loss = parse_optional_decimal(item.get("stopLoss"))
    if stop_loss is not None and stop_loss < 0:
        raise TradeExecutionError("Active Bybit position contains invalid stop price")

    return AccountPosition(
        symbol=symbol,
        side=parse_side(item.get("side")),
        size=size,
        avg_price=avg_price,
        mark_price=mark_price,
        unrealised_pnl=(parse_decimal(item.get("unrealisedPnl"))),
        status=str(item.get("positionStatus") or "Unknown"),
        take_profit=(parse_optional_decimal(item.get("takeProfit"))),
        stop_loss=stop_loss,
        break_even_price=(parse_optional_decimal(item.get("breakEvenPrice"))),
        trailing_stop=(parse_optional_decimal(item.get("trailingStop"))),
    )


def is_live_open_order(item: dict) -> bool:
    return _required_quantity(item.get("leavesQty"), "order leavesQty") > 0 or (
        item.get("stopOrderType") == "PartialStopLoss"
        and str(item.get("orderStatus")) == "Untriggered"
        and _required_quantity(item.get("qty"), "stop order qty") > 0
    )


def position_exposure_from_item(item: object) -> PositionExposure | None:
    if not isinstance(
        item,
        dict,
    ):
        raise TradeExecutionError("Bybit position is not an object")

    size = _required_quantity(item.get("size"), "position size")

    if size <= 0:
        return None

    avg_price_raw = item.get("avgPrice")

    if avg_price_raw is None or str(avg_price_raw).strip() == "":
        raise TradeExecutionError("Active Bybit position contains no average price")
    avg_price = parse_decimal(avg_price_raw)
    if avg_price <= 0:
        raise TradeExecutionError("Active Bybit position contains invalid pricing")

    return PositionExposure(
        side=(parse_side(item.get("side"))),
        size=size,
        avg_price=avg_price,
    )


def pending_entry_order_from_item(item: object) -> OpenOrderExposure | None:
    if not isinstance(
        item,
        dict,
    ):
        raise TradeExecutionError("Bybit entry order is not an object")

    order_link_id = str(item.get("orderLinkId") or "")

    # Only warn about pending entry
    # orders created by this app.
    if not order_link_id.startswith("ccb-"):
        return None

    reduce_only = item.get("reduceOnly")

    if reduce_only is True or str(reduce_only).casefold() == "true":
        return None

    remaining = _required_quantity(item.get("leavesQty"), "entry order leavesQty")

    if remaining <= 0:
        return None

    price_raw = str(item.get("price") or "").strip()

    price = (
        None
        if price_raw
        in {
            "",
            "0",
            "0.0",
            "0.00",
        }
        else Decimal(price_raw)
    )

    return OpenOrderExposure(
        side=(parse_side(item.get("side"))),
        remaining_quantity=(remaining),
        order_id=str(item.get("orderId") or ""),
        order_link_id=(order_link_id),
        price=price,
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
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise TradeExecutionError("Bybit order contains no symbol")
    quantity = _required_quantity(item.get("qty"), "order qty")
    remaining = _required_quantity(item.get("leavesQty"), "order leavesQty")
    if remaining > quantity:
        raise TradeExecutionError(
            "Bybit order remaining quantity exceeds total quantity"
        )
    if (
        not remaining
        and item.get("stopOrderType") == "PartialStopLoss"
        and item.get("orderStatus") == "Untriggered"
    ):
        remaining = quantity

    return AccountOrder(
        symbol=symbol.strip().upper(),
        side=parse_side(item.get("side")),
        order_type=str(item.get("orderType") or "Unknown"),
        status=str(item.get("orderStatus") or "Unknown"),
        quantity=quantity,
        remaining_quantity=remaining,
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
