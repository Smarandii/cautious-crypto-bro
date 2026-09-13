from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from cautious_crypto_bro.bybit import BybitDemoExecutor
from cautious_crypto_bro.config import get_settings
from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    Side,
    SourceMessage,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.storage import IntentStore


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a synthetic risk-sized execution plan "
            "and optionally submit it to Bybit Demo."
        )
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
        "--entry-type",
        choices=["MARKET", "LIMIT", "RANGE"],
        default="RANGE",
    )

    parser.add_argument(
        "--omit-tp",
        action="store_true",
        help=(
            "Leave trader TP empty so the "
            "ExecutionPolicy fallback ladder is used."
        ),
    )

    parser.add_argument(
        "--cancel-existing",
        action="store_true",
        help=(
            "Cancel all existing Demo orders for "
            "the selected symbol before the smoke test."
        ),
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the demo order(s).",
    )

    args = parser.parse_args()
    settings = get_settings()

    store = IntentStore(settings.database_path)
    await store.initialize()

    policy = await store.get_execution_policy()

    executor = BybitDemoExecutor(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
    )

    try:
        symbol = args.symbol.upper()
        side = Side(args.side)
        entry_type = EntryType(args.entry_type)

        if args.cancel_existing:
            cancelled = await executor.cancel_all_orders(
                symbol
            )

            print(
                f"Cancelled existing orders: {cancelled}"
            )

            await asyncio.sleep(1)

        context = await executor.market_context(symbol)
        market = context.market_price

        if side is Side.LONG:
            stop_loss = market * Decimal("0.95")
            take_profit = market * Decimal("1.05")

            limit_price = market * Decimal("0.99")

            range_low = market * Decimal("0.98")
            range_high = market * Decimal("0.99")
        else:
            stop_loss = market * Decimal("1.05")
            take_profit = market * Decimal("0.95")

            limit_price = market * Decimal("1.01")

            range_low = market * Decimal("1.01")
            range_high = market * Decimal("1.02")

        if entry_type is EntryType.MARKET:
            entry = Entry(
                type=EntryType.MARKET,
            )

        elif entry_type is EntryType.LIMIT:
            entry = Entry(
                type=EntryType.LIMIT,
                price=float(limit_price),
            )

        else:
            entry = Entry(
                type=EntryType.RANGE,
                range_low=float(range_low),
                range_high=float(range_high),
            )

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
            entry=entry,
            stop_loss=float(stop_loss),
            take_profit=(
                None
                if args.omit_tp
                else float(take_profit)
            ),
            summary="Synthetic Bybit Demo integration test.",
            confidence=1.0,
        )

        plan = ExecutionPlanner().plan(
            intent,
            policy,
            context,
        )

        print(f"Symbol:       {plan.symbol}")
        print(f"Side:         {plan.side}")
        print(f"Entry type:   {entry_type}")
        print(f"Market:       {context.market_price}")
        print(
            "Capital:      "
            f"{policy.trading_capital_usdt} USDT"
        )
        print(
            "Risk:         "
            f"{policy.risk_per_trade_pct}%"
        )
        print(
            "Risk budget:  "
            f"{policy.risk_budget_usdt} USDT"
        )
        print(f"Orders:       {len(plan.orders)}")

        for index, order in enumerate(
            plan.orders,
            start=1,
        ):
            price = (
                str(order.price)
                if order.price is not None
                else "MARKET"
            )

            print(
                f"  {index}: "
                f"{order.order_type} "
                f"price={price} "
                f"qty={order.quantity} "
                f"tp={order.take_profit}"
            )

        print(f"SL:           {plan.stop_loss}")
        print(f"TP:           {plan.take_profit}")
        print(
            "Planned loss: "
            f"{plan.planned_max_loss_usdt} USDT"
        )
        print()

        if not args.execute:
            print("Dry run only.")
            print(
                "Re-run with --execute to submit "
                "the Bybit Demo order(s)."
            )
            return

        order_ids = await executor.execute(plan)

        print()
        print("EXECUTED ON BYBIT DEMO")

        for order_id in order_ids:
            print(f"Order ID: {order_id}")

    finally:
        executor.close()


if __name__ == "__main__":
    asyncio.run(main())
