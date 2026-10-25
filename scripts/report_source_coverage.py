"""Aggregate original production decisions; no Telegram, exchange or model calls."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cautious_crypto_bro.source_coverage import coverage_summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    if args.hours <= 0:
        parser.error("--hours must be positive")
    result = asyncio.run(
        coverage_summary(
            args.database, since=datetime.now(UTC) - timedelta(hours=args.hours)
        )
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
