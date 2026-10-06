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


def account_position_from_item(item: dict) -> AccountPosition | None:
    size = parse_decimal(item.get("size"))

    if size <= 0:
        return None

    symbol = str(item.get("symbol") or "").upper()

    if not symbol:
        return None

    avg_price = parse_decimal(item.get("avgPrice"))

    mark_price = parse_decimal(item.get("markPrice"))

    if avg_price <= 0 or mark_price <= 0:
        raise TradeExecutionError("Active Bybit position contains invalid pricing")

    return AccountPosition(
        symbol=symbol,
        side=parse_side(item.get("side")),
        size=size,
        avg_price=avg_price,
        mark_price=mark_price,
        unrealised_pnl=(parse_decimal(item.get("unrealisedPnl"))),
        status=str(item.get("positionStatus") or "Unknown"),
        take_profit=(parse_optional_decimal(item.get("takeProfit"))),
        stop_loss=(parse_optional_decimal(item.get("stopLoss"))),
        break_even_price=(parse_optional_decimal(item.get("breakEvenPrice"))),
        trailing_stop=(parse_optional_decimal(item.get("trailingStop"))),
    )


def is_live_open_order(item: dict) -> bool:
    return parse_decimal(item.get("leavesQty")) > 0 or (
        item.get("stopOrderType") == "PartialStopLoss"
        and str(item.get("orderStatus")) == "Untriggered"
        and parse_decimal(item.get("qty")) > 0
    )


def position_exposure_from_item(item: object) -> PositionExposure | None:
    if not isinstance(
        item,
        dict,
    ):
        return None

    size = Decimal(str(item.get("size") or "0"))

    if size <= 0:
        return None

    avg_price_raw = item.get("avgPrice")

    if avg_price_raw is None or str(avg_price_raw).strip() == "":
        raise TradeExecutionError("Active Bybit position contains no average price")

    return PositionExposure(
        side=(parse_side(item.get("side"))),
        size=size,
        avg_price=Decimal(str(avg_price_raw)),
    )


def pending_entry_order_from_item(item: object) -> OpenOrderExposure | None:
    if not isinstance(
        item,
        dict,
    ):
        return None

    order_link_id = str(item.get("orderLinkId") or "")

    # Only warn about pending entry
    # orders created by this app.
    if not order_link_id.startswith("ccb-"):
        return None

    reduce_only = item.get("reduceOnly")

    if reduce_only is True or str(reduce_only).casefold() == "true":
        return None

    remaining = Decimal(str(item.get("leavesQty") or "0"))

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
