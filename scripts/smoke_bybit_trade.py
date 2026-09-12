from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from cautious_crypto_bro.bybit import BybitDemoExecutor
from cautious_crypto_bro.config import get_settings
from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    Side,
    SourceMessage,
    TradingIntent,
)


DEMO_BASE_URL = "https://api-demo.bybit.com"


def current_price(symbol: str) -> Decimal:
    response = httpx.get(
        f"{DEMO_BASE_URL}/v5/market/tickers",
        params={
            "category": "linear",
            "symbol": symbol,
        },
        timeout=10,
    )
    response.raise_for_status()

    data = response.json()

    if str(data.get("retCode", 0)) != "0":
        raise RuntimeError(
            f"Bybit ticker request failed: "
            f"{data.get('retCode')} {data.get('retMsg')}"
        )

    items = data.get("result", {}).get("list", [])

    if not items:
        raise RuntimeError(f"No ticker found for {symbol}")

    return Decimal(items[0]["lastPrice"])


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a synthetic TradingIntent and execute it on Bybit Demo."
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
    )
    parser.add_argument(
        "--side",
        choices=["LONG", "SHORT"],
        default="LONG",
    )
    parser.add_argument(
        "--notional",
        type=float,
        default=None,
        help="Override BYBIT_DEFAULT_NOTIONAL_USDT.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the demo order.",
    )

    args = parser.parse_args()
    settings = get_settings()

    symbol = args.symbol.upper()
    side = Side(args.side)
    price = current_price(symbol)

    if side is Side.LONG:
        stop_loss = price * Decimal("0.95")
        take_profit = price * Decimal("1.05")
    else:
        stop_loss = price * Decimal("1.05")
        take_profit = price * Decimal("0.95")

    now = datetime.now(timezone.utc)

    intent = TradingIntent(
        source=SourceMessage(
            channel_id=0,
            channel_title="CCB Bybit smoke test",
            channel_username=None,
            message_id=0,
            published_at=now,
            received_at=now,
            text="Synthetic integration-test intent.",
        ),
        symbol=symbol,
        side=side,
        entry=Entry(type=EntryType.MARKET),
        stop_loss=float(stop_loss),
        take_profit=float(take_profit),
        summary="Synthetic Bybit Demo integration test.",
        confidence=1.0,
    )

    notional = (
        args.notional
        if args.notional is not None
        else settings.bybit_default_notional_usdt
    )

    print(f"Symbol:   {intent.symbol}")
    print(f"Side:     {intent.side}")
    print(f"Price:    {price}")
    print(f"Notional: {notional} USDT")
    print(f"SL:       {intent.stop_loss}")
    print(f"TP:       {intent.take_profit}")
    print()

    if not args.execute:
        print("Dry run only.")
        print("Re-run with --execute to place the Bybit Demo order.")
        return

    executor = BybitDemoExecutor(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        notional_usdt=notional,
    )

    try:
        order_id = await executor.execute(intent)
    finally:
        executor.close()

    print()
    print("EXECUTED ON BYBIT DEMO")
    print(f"Order ID: {order_id}")


if __name__ == "__main__":
    asyncio.run(main())
