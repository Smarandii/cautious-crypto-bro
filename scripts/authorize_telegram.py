"""Authorize a replacement reader session locally while the app is stopped."""

from __future__ import annotations

import asyncio
import getpass
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from telethon import TelegramClient

from cautious_crypto_bro.config import Settings, get_settings
from cautious_crypto_bro.source_recovery import (
    pin_source_identity,
    read_source_identity,
)
from cautious_crypto_bro.state_ownership import state_ownership
from cautious_crypto_bro.telegram_source import SourceAuthorizationRequired


def _backup_session(session: Path) -> Path | None:
    if not session.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = session.with_name(f"{session.name}.before-authorization-{stamp}")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(session)) as original:
        if original.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Existing session failed its integrity check")
        original.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with closing(sqlite3.connect(backup)) as snapshot:
            original.backup(snapshot)
    if any(
        Path(str(session) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise RuntimeError(
            "Session journal remains; stop all session writers before retrying"
        )
    return backup


async def authorize(settings: Settings) -> None:
    name = settings.telegram_session_name
    session = Path(name if name.endswith(".session") else name + ".session").resolve()
    identity = settings.database_path.with_suffix(".telegram-identity.json")
    with state_ownership(settings.database_path):
        expected = read_source_identity(identity) or settings.telegram_approver_user_id
        session.parent.mkdir(parents=True, exist_ok=True)
        descriptor, candidate_name = tempfile.mkstemp(
            prefix=".telegram-authorization-", suffix=".session", dir=session.parent
        )
        os.close(descriptor)
        candidate = Path(candidate_name)
        client = None
        try:
            client = TelegramClient(
                str(candidate), settings.telegram_api_id, settings.telegram_api_hash
            )
            await client.start(
                phone=lambda: getpass.getpass("Telegram phone (hidden): "),
                code_callback=lambda: getpass.getpass("Telegram login code (hidden): "),
                password=lambda: getpass.getpass("Telegram 2FA password (hidden): "),
            )
            user = await client.get_me()
            if user is None or getattr(user, "id", None) != expected:
                await client.log_out()
                raise SourceAuthorizationRequired("Source account identity mismatch")
            await client.disconnect()
            _backup_session(session)
            candidate.replace(session)
            pin_source_identity(identity, expected)
        finally:
            try:
                if client is not None:
                    await client.disconnect()
            finally:
                candidate.unlink(missing_ok=True)


def main() -> None:
    try:
        asyncio.run(authorize(get_settings()))
    except Exception as exc:
        # Do not print RPC bodies, login codes, phone numbers or passwords.
        print(
            f"Authorization failed ({type(exc).__name__}). Review the local session and retry."
        )
        raise SystemExit(1) from None
    print("Authorized reader session saved. Restart the application.")


if __name__ == "__main__":
    main()
