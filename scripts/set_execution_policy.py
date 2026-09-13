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
        description=(
            "Show or update "
            "the execution policy."
        )
    )

    parser.add_argument(
        "--capital-usdt",
        type=Decimal,
        help=(
            "Simulated trading capital "
            "in USDT."
        ),
    )

    parser.add_argument(
        "--risk-pct",
        type=Decimal,
        help=(
            "Maximum price-risk percentage "
            "per trade, e.g. 1 or 2."
        ),
    )

    parser.add_argument(
        "--range-orders",
        type=int,
        help=(
            "Number of evenly spaced "
            "limit orders for RANGE entries."
        ),
    )

    args = parser.parse_args()

    database_path = Path(
        os.environ.get(
            "DATABASE_PATH",
            "data/cautious_crypto_bro.sqlite3",
        )
    )

    store = IntentStore(
        database_path
    )

    await store.initialize()

    current = (
        await store.get_execution_policy()
    )

    if (
        args.capital_usdt is None
        and args.risk_pct is None
        and args.range_orders is None
    ):
        print_policy(
            current
        )
        return

    updated = (
        ExecutionPolicy.model_validate(
            {
                "trading_capital_usdt": (
                    args.capital_usdt
                    if (
                        args.capital_usdt
                        is not None
                    )
                    else (
                        current
                        .trading_capital_usdt
                    )
                ),
                "risk_per_trade_pct": (
                    args.risk_pct
                    if (
                        args.risk_pct
                        is not None
                    )
                    else (
                        current
                        .risk_per_trade_pct
                    )
                ),
                "range_order_count": (
                    args.range_orders
                    if (
                        args.range_orders
                        is not None
                    )
                    else (
                        current
                        .range_order_count
                    )
                ),
            }
        )
    )

    await store.set_execution_policy(
        updated
    )

    print(
        "Updated execution policy:"
    )

    print_policy(
        updated
    )


def print_policy(
    policy: ExecutionPolicy,
) -> None:
    print(
        "capital_usdt="
        f"{policy.trading_capital_usdt}"
    )
    print(
        "risk_pct="
        f"{policy.risk_per_trade_pct}"
    )
    print(
        "range_orders="
        f"{policy.range_order_count}"
    )
    print(
        "risk_budget_usdt="
        f"{policy.risk_budget_usdt}"
    )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
