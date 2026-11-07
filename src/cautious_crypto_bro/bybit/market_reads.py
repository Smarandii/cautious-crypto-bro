"""Read public Bybit market metadata, tickers, candles, and risk tiers."""

from __future__ import annotations

from decimal import Decimal

from ..domain import InstrumentContext, TradeExecutionError
from .client import BybitClient
from .leverage import RiskTier, instrument_leverage_filter, risk_tiers
from .normalize import parse_decimal


def one_minute_closes(
    client: BybitClient, symbol: str, limit: int
) -> list[tuple[int, Decimal]]:
    if not 1 <= limit <= 200:
        raise TradeExecutionError("Minute-close limit must be between 1 and 200")
    response = client.public_get(
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": symbol,
            "interval": "1",
            "limit": limit + 1,
        },
    )
    rows = response.get("result", {}).get("list") or []
    closes: list[tuple[int, Decimal]] = []
    for row in rows:
        try:
            start = int(row[0])
            close = parse_decimal(row[4])
        except (IndexError, TypeError, ValueError, ArithmeticError):
            continue
        if start > 0 and close is not None and close > 0:
            closes.append((start, close))
    closes.sort()
    return closes


def instrument_info(client: BybitClient, symbol: str) -> dict:
    response = client.public_get(
        "/v5/market/instruments-info",
        {
            "category": "linear",
            "symbol": symbol,
        },
    )
    items = response.get("result", {}).get("list", [])
    if not items:
        raise TradeExecutionError(f"No Bybit instrument found for {symbol}")
    return items[0]


def build_market_context(instrument: dict, market_price: Decimal) -> InstrumentContext:
    lot = instrument["lotSizeFilter"]
    price_filter = instrument["priceFilter"]
    leverage_filter = instrument_leverage_filter(instrument)
    return InstrumentContext(
        leverage_filter=leverage_filter,
        market_price=market_price,
        tick_size=Decimal(price_filter["tickSize"]),
        qty_step=Decimal(lot["qtyStep"]),
        min_qty=Decimal(lot["minOrderQty"]),
        min_notional=Decimal(lot.get("minNotionalValue", "0")),
    )


def risk_tiers_for_symbol(client: BybitClient, symbol: str) -> tuple[RiskTier, ...]:
    items: list[dict] = []
    cursor = ""
    seen_cursors: set[str] = set()
    for _ in range(10):
        params = {"category": "linear", "symbol": symbol}
        if cursor:
            params["cursor"] = cursor
        response = client.public_get("/v5/market/risk-limit", params)
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise TradeExecutionError("Bybit risk-limit page is malformed")
        items.extend(result["list"])
        next_cursor = result.get("nextPageCursor", "")
        if not isinstance(next_cursor, str):
            raise TradeExecutionError("Bybit risk-limit cursor is malformed")
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise TradeExecutionError("Bybit risk-limit cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise TradeExecutionError("Bybit risk-limit pagination did not complete")
    return risk_tiers(items, symbol)


def mark_price(client: BybitClient, symbol: str) -> Decimal:
    response = client.public_get(
        "/v5/market/tickers", {"category": "linear", "symbol": symbol}
    )
    result = response.get("result")
    items = result.get("list") if isinstance(result, dict) else None
    if (
        not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
        or items[0].get("symbol") != symbol
    ):
        raise TradeExecutionError("Bybit ticker mark evidence is incomplete")
    try:
        mark = parse_decimal(items[0].get("markPrice"))
    except (ArithmeticError, ValueError) as exc:
        raise TradeExecutionError("Bybit ticker mark is invalid") from exc
    if mark <= 0:
        raise TradeExecutionError("Bybit ticker mark must be positive")
    return mark


def last_price(client: BybitClient, symbol: str) -> Decimal:
    response = client.public_get(
        "/v5/market/tickers",
        {
            "category": "linear",
            "symbol": symbol,
        },
    )
    items = response.get("result", {}).get("list", [])
    if not items:
        raise TradeExecutionError(f"No Bybit ticker found for {symbol}")
    return Decimal(items[0]["lastPrice"])
