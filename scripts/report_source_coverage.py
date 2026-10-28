"""Audit stored decisions offline, or opt into metadata-only Telegram history."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cautious_crypto_bro.source_coverage import coverage_summary


async def _telegram_history(args):
    # The default report never constructs network clients or loads credentials.
    from telethon import TelegramClient

    from cautious_crypto_bro.config import get_settings
    from cautious_crypto_bro.telegram_coverage import telegram_history_coverage
    from cautious_crypto_bro.telegram_replay import cloned_telegram_session

    settings = get_settings()
    until = datetime.now(UTC) - timedelta(seconds=args.settle_seconds)
    with cloned_telegram_session(settings.telegram_session_name) as session:
        client = TelegramClient(
            session,
            settings.telegram_api_id,
            settings.telegram_api_hash,
            receive_updates=False,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Cloned Telegram session is not authorized")
            return await telegram_history_coverage(
                args.database,
                client,
                settings.telegram_source_channels,
                since=until - timedelta(hours=args.hours),
                until=until,
                max_messages=args.max_messages,
            )
        finally:
            await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument(
        "--telegram-history",
        action="store_true",
        help="Opt in to metadata-only history reads using a temporary session clone",
    )
    parser.add_argument("--settle-seconds", type=int, default=120)
    parser.add_argument(
        "--max-messages",
        type=int,
        default=2000,
        help="Per-channel metadata cap; one extra item checks the boundary",
    )
    args = parser.parse_args()
    if args.hours <= 0:
        parser.error("--hours must be positive")
    if args.settle_seconds < 0 or args.max_messages <= 0:
        parser.error("Settling margin must be nonnegative and message cap positive")
    try:
        result = asyncio.run(
            _telegram_history(args)
            if args.telegram_history
            else coverage_summary(
                args.database, since=datetime.now(UTC) - timedelta(hours=args.hours)
            )
        )
    except Exception as exc:
        parser.exit(
            1,
            f"Coverage audit failed ({type(exc).__name__}); private details withheld\n",
        )
    print(json.dumps(result, sort_keys=True))
    if args.telegram_history and not result["intake_coverage_complete"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
