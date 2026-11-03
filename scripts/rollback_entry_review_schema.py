"""Offline schema17 -> 16 compatibility step; retain all current trading state."""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from cautious_crypto_bro.state_ownership import state_ownership


def rollback(database: Path, backup: Path) -> None:
    if not database.is_file() or database.resolve() == backup.resolve():
        raise ValueError("Existing database and distinct new backup path required")
    if backup.exists():
        raise ValueError("Backup already exists; refusing to overwrite")
    with state_ownership(database), closing(sqlite3.connect(database)) as db:
        if db.execute("PRAGMA user_version").fetchone() != (17,):
            raise ValueError("Only schema17 is supported by this rollback")
        columns = [row[1] for row in db.execute("PRAGMA table_info(entry_reviews)")]
        if columns != [
            "channel_id",
            "message_id",
            "symbol",
            "payload_json",
            "evaluation_fingerprint",
            "claim_token",
            "retry_after",
            "delivered_at",
            "created_at",
        ]:
            raise ValueError("Unexpected review table; refuse compatibility rollback")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Database foreign key check failed")
        # O_EXCL prevents replacing a backup created between the earlier check
        # and this point. Neither the backup nor the review evidence is deleted.
        os.close(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with closing(sqlite3.connect(backup)) as destination:
            db.backup(destination)
        with db:
            db.execute("PRAGMA user_version=16")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("backup", type=Path)
    arguments = parser.parse_args()
    rollback(arguments.database, arguments.backup)
    print(
        "Schema16 compatibility restored; current state and review evidence retained."
    )
