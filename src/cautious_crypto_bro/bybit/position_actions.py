"""Execute lifecycle position actions (REDUCE/CLOSE) and symbol-wide CCB cancellations."""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import ROUND_DOWN, Decimal

from ..domain import (
    PositionActionExecutionResult,
    PositionActionIntent,
    PositionActionPreflightError,
    PositionActionType,
    PositionExposure,
    Side,
    SymbolExposure,
    TradeExecutionError,
)
from . import account_reads, market_reads
from .client import BybitClient
from .normalize import format_decimal, parse_decimal


def position_for_action(
    action: PositionActionIntent,
    exposure: SymbolExposure,
) -> PositionExposure:
    if not exposure.positions:
        raise TradeExecutionError(f"No active position for {action.symbol}")

    if len(exposure.positions) != 1:
        raise TradeExecutionError(
            f"Expected exactly one active position for {action.symbol}"
        )

    position = exposure.positions[0]

    if action.expected_side is not None and position.side is not action.expected_side:
        raise TradeExecutionError(
            "Current position side does not "
            "match signal expectation: "
            f"current={position.side.value}, "
            f"expected="
            f"{action.expected_side.value}"
        )

    return position


def partial_reduce_quantity(
    client: BybitClient,
    symbol: str,
    position_size: Decimal,
    close_pct: Decimal,
) -> Decimal:
    lot = market_reads.instrument_info(client, symbol)["lotSizeFilter"]

    qty_step = Decimal(str(lot["qtyStep"]))
    min_qty = Decimal(str(lot["minOrderQty"]))

    raw = position_size * close_pct / Decimal("100")

    units = (raw / qty_step).to_integral_value(rounding=ROUND_DOWN)

    quantity = units * qty_step

    if quantity <= 0 or quantity < min_qty:
        raise TradeExecutionError(
            "Requested partial reduction rounds below Bybit minimum quantity"
        )

    if quantity >= position_size:
        raise TradeExecutionError(
            "REDUCE would close the full position; use CLOSE instead"
        )

    return quantity


def action_order_body(
    client: BybitClient,
    action: PositionActionIntent,
    position: PositionExposure,
) -> tuple[dict[str, object], Decimal | None]:
    if action.action is PositionActionType.CLOSE:
        submitted_quantity: Decimal | None = None
        qty = "0"

    else:
        assert action.close_pct is not None

        submitted_quantity = partial_reduce_quantity(
            client,
            action.symbol,
            position.size,
            Decimal(str(action.close_pct)),
        )

        qty = format_decimal(submitted_quantity)

    side = "Sell" if position.side is Side.LONG else "Buy"

    body: dict[
        str,
        object,
    ] = {
        "category": "linear",
        "symbol": action.symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty,
        "timeInForce": "IOC",
        "positionIdx": 0,
        "reduceOnly": True,
        "orderLinkId": (f"ccb-action-{action.action_id.hex[:24]}"),
    }

    if action.action is PositionActionType.CLOSE:
        body["closeOnTrigger"] = True

    return body, submitted_quantity


def cancel_ccb_exit_orders(
    client: BybitClient,
    symbol: str,
    cancel_order: Callable[[str, str], None],
) -> int:
    cancelled: set[str] = set()
    params = {
        "category": "linear",
        "symbol": symbol,
        "openOnly": 0,
        "limit": 50,
    }
    items = client.paginate_private_list(
        "/v5/order/realtime",
        params,
    )

    for item in items:
        link_id = str(item.get("orderLinkId") or "")

        if not link_id.startswith("ccb-v2-") or "-t" not in link_id:
            continue

        reduce_only = item.get("reduceOnly")

        if not (reduce_only is True or str(reduce_only).casefold() == "true"):
            continue

        if parse_decimal(item.get("leavesQty")) <= 0:
            continue

        order_id = str(item.get("orderId") or "")

        if not order_id:
            continue

        cancel_order(
            symbol,
            order_id,
        )

        cancelled.add(order_id)

    if cancelled:
        # Cancellation acknowledgements do not prove the exits are inactive.
        for attempt in range(5):
            remaining = client.paginate_private_list("/v5/order/realtime", params)
            if not any(
                str(item.get("orderId") or "") in cancelled for item in remaining
            ):
                return len(cancelled)
            if attempt < 4:
                time.sleep(0.25)
        raise TradeExecutionError(
            "CCB exit cancellation is not confirmed; refusing position action"
        )
    return len(cancelled)


def cancel_ccb_entry_orders(
    client: BybitClient,
    symbol: str,
) -> int:
    cancelled: set[str] = set()

    for _ in range(5):
        exposure = account_reads.symbol_exposure(client, symbol)

        pending = exposure.pending_entry_orders

        if not pending:
            return len(cancelled)

        for order in pending:
            link_id = order.order_link_id

            if link_id in cancelled:
                continue

            response = client.private_post(
                "/v5/order/cancel",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "orderLinkId": link_id,
                },
            )

            result = response.get(
                "result",
                {},
            )

            if not result.get("orderId"):
                raise TradeExecutionError(
                    f"Bybit accepted no order ID while cancelling CCB entry {link_id}"
                )

            cancelled.add(link_id)

        time.sleep(0.25)

    remaining = account_reads.symbol_exposure(client, symbol).pending_entry_orders

    if remaining:
        raise TradeExecutionError(
            "CCB entry orders are still active after cancellation; refusing full close"
        )

    return len(cancelled)


def execute_position_action(
    client: BybitClient,
    action: PositionActionIntent,
    cancel_order: Callable[[str, str], None],
) -> PositionActionExecutionResult:
    if action.action is PositionActionType.CANCEL_ENTRIES:
        raise PositionActionPreflightError(
            "Entry cancellation requires source-scoped coordinator"
        )
    # Validate before causing any side effects.
    try:
        client.sync_clock(force=False)
        initial_exposure = account_reads.symbol_exposure(client, action.symbol)
        initial_position = position_for_action(
            action,
            initial_exposure,
        )
    except Exception as exc:
        # This block contains reads only. Preserve uncertain outcomes once
        # cancellation or submission starts below, including later read failures.
        raise PositionActionPreflightError(str(exc)) from exc

    cancelled_entries = cancel_ccb_entry_orders(client, action.symbol)
    cancelled_exits = cancel_ccb_exit_orders(client, action.symbol, cancel_order)

    # Resolve position size again after cancellation.
    # An entry could have filled concurrently while
    # cancellations were being processed.
    position = position_for_action(
        action,
        account_reads.symbol_exposure(client, action.symbol),
    )
    if position.side is not initial_position.side:
        raise TradeExecutionError(
            "Position side changed during cancellation; refusing position action"
        )

    body, submitted_quantity = action_order_body(client, action, position)

    response = client.private_post(
        "/v5/order/create",
        body,
    )

    order_id = response.get("result", {}).get("orderId")

    if not order_id:
        raise TradeExecutionError("Bybit returned success without orderId")

    return PositionActionExecutionResult(
        order_id=str(order_id),
        position_side=position.side,
        position_size_before=position.size,
        submitted_quantity=(submitted_quantity),
        cancelled_entry_orders=(cancelled_entries),
        cancelled_exit_orders=(cancelled_exits),
    )
