from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal
from pathlib import Path

from cautious_crypto_bro.domain import (
    ExecutionPolicy,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Show or update Strategy V2 execution policy.")
    )

    parser.add_argument(
        "--risk-pct",
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

    if args.risk_pct is None:
        print_policy(current)
        return

    updated = ExecutionPolicy.model_validate(
        {
            "risk_per_trade_pct": (args.risk_pct),
        }
    )

    await store.set_execution_policy(updated)

    print("Updated execution policy:")
    print_policy(updated)


def print_policy(
    policy: ExecutionPolicy,
) -> None:
    strategy = policy.strategy_v2

    print("capital_usdt=live Bybit totalWalletBalance")
    print(f"risk_pct={policy.risk_per_trade_pct}")
    print(
        "entries="
        f"{strategy.primary_entry_risk_pct}/"
        f"{strategy.secondary_entry_risk_pct}/"
        f"{strategy.tertiary_entry_risk_pct}%"
    )
    print(
        "entry_depths="
        f"0/"
        f"{strategy.secondary_entry_depth_r}/"
        f"{strategy.tertiary_entry_depth_r}R"
    )
    print(
        "fixed_exits="
        f"{strategy.first_take_profit_pct}%@"
        f"{strategy.first_take_profit_r}R,"
        f"{strategy.second_take_profit_pct}%@"
        f"{strategy.second_take_profit_r}R,"
        f"{strategy.third_take_profit_pct}%@"
        f"{strategy.third_take_profit_r}R"
    )
    print(f"runner={strategy.runner_pct}%")
    print(f"trailing={strategy.trailing_activation_r}R/{strategy.trailing_distance_r}R")
    print(f"minimum_locked_profit={strategy.minimum_locked_profit_r}R")


if __name__ == "__main__":
    asyncio.run(main())
