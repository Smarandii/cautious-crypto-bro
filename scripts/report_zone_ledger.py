"""Inspect allocation evidence only; never submit trades or infer protection readiness."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import aiosqlite

from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_portfolio_admission import measure_multi_zone_coverage


async def report(path: Path) -> dict:
    uri = path.resolve().as_uri() + "?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as db:
        rows = await (
            await db.execute("SELECT group_id FROM zone_groups ORDER BY group_id")
        ).fetchall()
    with closing(sqlite3.connect(uri, uri=True)) as sync_db:
        coverage = measure_multi_zone_coverage(sync_db)
    store = IntentStore(path)
    groups = [await store.zone_group_snapshot(UUID(row[0])) for row in rows]
    return {
        "groups": len(groups),
        "zones": sum(len(group["zones"]) for group in groups),
        "total_frozen_budget_usdt": str(
            sum((group["total_risk_budget_usdt"] for group in groups), Decimal("0"))
        ),
        "groups_with_ledger_issues": sum(bool(group["issues"]) for group in groups),
        "multi_zone_coverage": coverage,
        "live_account_snapshot_supplied": False,
        "protection_verified": False,
        "trading_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("--database must name an existing ledger database")
    print(json.dumps(asyncio.run(report(args.database)), sort_keys=True))


if __name__ == "__main__":
    main()
