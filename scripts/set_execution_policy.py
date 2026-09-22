from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal
from pathlib import Path

from cautious_crypto_bro.domain import (
    ExecutionPolicy,
    ExitPolicy,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Show or update the execution policy.")
    )

    parser.add_argument(
        "--risk-pct",
        type=Decimal,
    )
    parser.add_argument(
        "--range-orders",
        type=int,
    )

    parser.add_argument(
        "--minimum-reward-bps",
        type=Decimal,
    )

    parser.add_argument(
        "--basic-r",
        type=Decimal,
    )
    parser.add_argument(
        "--basic-close-pct",
        type=Decimal,
    )

    parser.add_argument(
        "--medium-r",
        type=Decimal,
    )
    parser.add_argument(
        "--medium-close-pct",
        type=Decimal,
    )

    parser.add_argument(
        "--high-r",
        type=Decimal,
    )
    parser.add_argument(
        "--high-close-pct",
        type=Decimal,
    )

    args = parser.parse_args()

    database_path = Path(
        os.environ.get(
            "DATABASE_PATH",
            "data/cautious_crypto_bro.sqlite3",
        )
    )

    store = IntentStore(database_path)
    await store.initialize()

    current = await store.get_execution_policy()

    supplied = any(
        value is not None
        for value in (
            args.risk_pct,
            args.range_orders,
            args.minimum_reward_bps,
            args.basic_r,
            args.basic_close_pct,
            args.medium_r,
            args.medium_close_pct,
            args.high_r,
            args.high_close_pct,
        )
    )

    if not supplied:
        print_policy(current)
        return

    current_exit = current.exit_policy

    exit_policy = ExitPolicy(
        minimum_reward_bps=(
            args.minimum_reward_bps
            if args.minimum_reward_bps is not None
            else current_exit.minimum_reward_bps
        ),
        basic_r_multiple=(
            args.basic_r if args.basic_r is not None else current_exit.basic_r_multiple
        ),
        basic_close_pct=(
            args.basic_close_pct
            if args.basic_close_pct is not None
            else current_exit.basic_close_pct
        ),
        medium_r_multiple=(
            args.medium_r
            if args.medium_r is not None
            else current_exit.medium_r_multiple
        ),
        medium_close_pct=(
            args.medium_close_pct
            if args.medium_close_pct is not None
            else current_exit.medium_close_pct
        ),
        high_r_multiple=(
            args.high_r if args.high_r is not None else current_exit.high_r_multiple
        ),
        high_close_pct=(
            args.high_close_pct
            if args.high_close_pct is not None
            else current_exit.high_close_pct
        ),
    )

    updated = ExecutionPolicy(
        # Temporary V1 compatibility field.
        # Runtime planning replaces this value
        # with live Bybit totalWalletBalance.
        trading_capital_usdt=(current.trading_capital_usdt),
        risk_per_trade_pct=(
            args.risk_pct if args.risk_pct is not None else current.risk_per_trade_pct
        ),
        range_order_count=(
            args.range_orders
            if args.range_orders is not None
            else current.range_order_count
        ),
        exit_policy=exit_policy,
    )

    await store.set_execution_policy(updated)

    print("Updated execution policy:")
    print_policy(updated)


def print_policy(
    policy: ExecutionPolicy,
) -> None:
    exit_policy = policy.exit_policy

    print("capital_usdt=live Bybit totalWalletBalance at planning time")
    print(f"risk_pct={policy.risk_per_trade_pct}")
    print(f"range_orders={policy.range_order_count}")
    print("risk_budget_usdt=live capital * risk_pct / 100")

    print(f"minimum_reward_bps={exit_policy.minimum_reward_bps}")

    print(f"basic={exit_policy.basic_r_multiple}R/{exit_policy.basic_close_pct}%")
    print(f"medium={exit_policy.medium_r_multiple}R/{exit_policy.medium_close_pct}%")
    print(f"high={exit_policy.high_r_multiple}R/{exit_policy.high_close_pct}%")


if __name__ == "__main__":
    asyncio.run(main())
