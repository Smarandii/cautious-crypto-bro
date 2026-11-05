"""Read account evidence through the existing broker client; never submit orders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..domain import AccountStateSummary, ClosedPnlRecord, TradeExecutionError
from ..execution_history import ExecutionHistoryPage
from .client import BybitClient
from .normalize import (
    account_order_from_item,
    account_position_from_item,
    closed_pnl_record_from_item,
    is_live_open_order,
    parse_decimal,
)


def execution_history_page(
    client: BybitClient, symbol: str, start_ms: int, end_ms: int, cursor: str
) -> ExecutionHistoryPage:
    client.sync_clock(force=False)
    result = client.private_get(
        "/v5/execution/list",
        {
            "category": "linear",
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 100,
            "cursor": cursor,
        },
    ).get("result")
    if not isinstance(result, dict):
        raise TradeExecutionError("Bybit execution history returned an invalid result")
    rows, next_cursor = (result.get("list"), result.get("nextPageCursor"))
    if (
        not isinstance(rows, list)
        or len(rows) > 100
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise TradeExecutionError("Bybit execution history returned an invalid page")
    if (
        not isinstance(next_cursor, str)
        or len(next_cursor) > 2048
        or (next_cursor and next_cursor == cursor)
    ):
        raise TradeExecutionError("Bybit execution history returned an invalid cursor")
    return ExecutionHistoryPage(tuple(rows), next_cursor)


def execution_history(
    client: BybitClient, symbol: str, since_ms: int
) -> tuple[dict, ...]:
    client.sync_clock(force=False)
    return tuple(
        client.paginate_private_list(
            "/v5/execution/list",
            {
                "category": "linear",
                "symbol": symbol,
                "startTime": int(since_ms),
                "limit": 100,
            },
        )
    )


def wallet_balance_usdt(client: BybitClient) -> Decimal:
    response = client.private_get(
        "/v5/account/wallet-balance", {"accountType": "UNIFIED"}
    )
    accounts = response.get("result", {}).get("list", [])
    if (
        not isinstance(accounts, list)
        or len(accounts) != 1
        or (not isinstance(accounts[0], dict))
    ):
        raise TradeExecutionError(
            "Bybit wallet balance response did not contain exactly one account"
        )
    balance = parse_decimal(accounts[0].get("totalWalletBalance"))
    if balance <= 0:
        raise TradeExecutionError("Bybit totalWalletBalance must be positive")
    return balance


def account_state(client: BybitClient) -> AccountStateSummary:
    client.sync_clock(force=False)
    now = datetime.now(UTC)
    position_items = client.paginate_private_list(
        "/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": 200}
    )
    positions = [
        position
        for position in map(account_position_from_item, position_items)
        if position is not None
    ]
    open_items = client.paginate_private_list(
        "/v5/order/realtime",
        {"category": "linear", "settleCoin": "USDT", "openOnly": 0, "limit": 50},
    )
    open_orders = tuple(
        account_order_from_item(item) for item in open_items if is_live_open_order(item)
    )
    return AccountStateSummary(
        as_of=now,
        positions=tuple(sorted(positions, key=lambda position: position.symbol)),
        open_orders=tuple(
            sorted(open_orders, key=lambda order: order.updated_at, reverse=True)
        ),
    )


def closed_pnl_history(
    client: BybitClient, start: datetime, end: datetime
) -> tuple[ClosedPnlRecord, ...]:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Closed-PnL range must be timezone-aware")
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    if end <= start:
        return ()
    client.sync_clock(force=False)
    records: dict[str, ClosedPnlRecord] = {}
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=7), end)
        items = client.paginate_private_list(
            "/v5/position/closed-pnl",
            {
                "category": "linear",
                "startTime": int(window_start.timestamp() * 1000),
                "endTime": int(window_end.timestamp() * 1000),
                "limit": 100,
            },
        )
        for item in items:
            record = closed_pnl_record_from_item(item)
            records[record.record_id] = record
        window_start = window_end
    return tuple(sorted(records.values(), key=lambda item: item.updated_at))
