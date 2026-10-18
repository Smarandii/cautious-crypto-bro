"""Offline durable-state backups with an independent, temporary restore drill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

from cautious_crypto_bro.state_ownership import state_ownership

CONTENTS = {
    "database.sqlite3": ("intents", "execution_plans"),
    "telegram.session": ("sessions",),
}


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inspect_database(path: Path, required_tables: tuple[str, ...]) -> dict[str, int]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError(f"SQLite integrity check failed: {path.name}")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not set(required_tables) <= tables:
            raise ValueError(f"Required state tables missing: {path.name}")
        return {
            "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
            "table_count": len(tables),
        }


def verify_archive(archive_path: Path, expected_sha256: str) -> dict:
    digest = sha256(archive_path)
    if digest != expected_sha256.lower():
        raise ValueError("Archive SHA-256 does not match the expected receipt")
    with tempfile.TemporaryDirectory(prefix="ccb-restore-") as directory:
        restored = Path(directory)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if (
                len(members) != len(CONTENTS)
                or {member.name for member in members} != set(CONTENTS)
                or any(not member.isfile() for member in members)
            ):
                raise ValueError(
                    "Archive must contain exactly the two regular state files"
                )
            for member in members:
                # Names are checked before writing; never follow archive links/paths.
                source = archive.extractfile(member)
                assert source is not None
                with source, (restored / member.name).open("xb") as target:
                    shutil.copyfileobj(source, target)
        return {
            "archive_sha256": digest,
            "isolated_restore_verified": True,
            "files": {
                name: inspect_database(restored / name, tables)
                for name, tables in CONTENTS.items()
            },
        }


def create_backup(database: Path, session: Path, output: Path) -> dict:
    database = database.resolve(strict=True)
    session = session.resolve(strict=True)
    if database == session:
        raise ValueError("Application database and Telegram session must be distinct")
    with state_ownership(database):
        output.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with tempfile.TemporaryDirectory(prefix="ccb-backup-") as directory:
                snapshots = Path(directory)
                for name, source in {
                    "database.sqlite3": database,
                    "telegram.session": session,
                }.items():
                    with (
                        closing(
                            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                        ) as original,
                        closing(sqlite3.connect(snapshots / name)) as snapshot,
                    ):
                        original.backup(snapshot)
                # Exclusive creation preserves existing archives; mode protects sessions.
                descriptor = os.open(
                    output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                created = True
                with os.fdopen(descriptor, "wb") as handle:
                    with tarfile.open(fileobj=handle, mode="w:gz") as archive:
                        for name in CONTENTS:
                            archive.add(snapshots / name, arcname=name)
            return verify_archive(output, sha256(output))
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Stop all writers before backing up")
    create.add_argument("--database", required=True, type=Path)
    create.add_argument("--session", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser(
        "verify", help="Restore only into disposable scratch space"
    )
    verify.add_argument("archive", type=Path)
    verify.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_backup(args.database, args.session, args.output)
        else:
            result = verify_archive(args.archive, args.sha256)
    except Exception as exc:
        parser.exit(1, f"Backup operation failed ({type(exc).__name__}): {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
