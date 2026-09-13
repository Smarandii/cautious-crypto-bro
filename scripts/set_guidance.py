from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from cautious_crypto_bro.storage import IntentStore


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Create or replace signal interpretation guidance.")
    )

    scope = parser.add_mutually_exclusive_group(required=True)

    scope.add_argument(
        "--global",
        dest="is_global",
        action="store_true",
        help="Set guidance applied to every channel.",
    )

    scope.add_argument(
        "--channel",
        type=int,
        help="Set guidance for one Telegram channel.",
    )

    args = parser.parse_args()

    content = sys.stdin.read().strip()

    if not content:
        raise SystemExit("Pass guidance text through stdin.")

    database_path = Path(
        os.environ.get(
            "DATABASE_PATH",
            "data/cautious_crypto_bro.sqlite3",
        )
    )

    store = IntentStore(database_path)
    await store.initialize()

    channel_id = None if args.is_global else args.channel

    await store.set_guidance(
        content,
        channel_id=channel_id,
    )

    if channel_id is None:
        print("Updated global guidance.")
    else:
        print(f"Updated guidance for channel {channel_id}.")


if __name__ == "__main__":
    asyncio.run(main())
