import asyncio
import sqlite3

import aiosqlite

from cautious_crypto_bro.storage import (
    LATEST_SCHEMA_VERSION,
    IntentStore,
)


def test_fresh_database_is_migrated_to_latest_version(
    tmp_path,
) -> None:
    async def run() -> None:
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)

        await store.initialize()
        await store.initialize()

        async with aiosqlite.connect(database) as db:
            cursor = await db.execute("PRAGMA user_version")
            version = (await cursor.fetchone())[0]

            assert version == LATEST_SCHEMA_VERSION

            cursor = await db.execute("PRAGMA table_info(source_messages)")
            columns = {row[1] for row in await cursor.fetchall()}

            assert {
                "channel_id",
                "message_id",
                "payload_json",
                "status",
                "attempt_count",
                "last_error",
                "claim_token",
                "updated_at",
            } <= columns

            cursor = await db.execute("SELECT COUNT(*) FROM execution_policy")
            assert (await cursor.fetchone())[0] == 1

            cursor = await db.execute("SELECT COUNT(*) FROM execution_exit_policy")
            assert (await cursor.fetchone())[0] == 1

            cursor = await db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE
                    type = 'table'
                    AND name = 'account_closed_pnl'
                """
            )
            assert await cursor.fetchone() is not None

            cursor = await db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE
                    type = 'table'
                    AND name = 'account_pnl_sync'
                """
            )
            assert await cursor.fetchone() is not None

    asyncio.run(run())


def test_obsolete_database_schema_is_rejected(
    tmp_path,
) -> None:
    database = tmp_path / "state.sqlite3"

    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version = 3")

    async def run() -> None:
        store = IntentStore(database)

        try:
            await store.initialize()
        except RuntimeError as exc:
            assert "Unsupported database schema version" in str(exc)
        else:
            raise AssertionError("Expected obsolete schema to be rejected")

    asyncio.run(run())


def test_unversioned_non_empty_database_is_rejected(
    tmp_path,
) -> None:
    database = tmp_path / "state.sqlite3"

    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE legacy_data(value TEXT)")

    async def run() -> None:
        store = IntentStore(database)

        try:
            await store.initialize()
        except RuntimeError as exc:
            assert "Unversioned non-empty database" in str(exc)
        else:
            raise AssertionError("Expected unversioned legacy DB to be rejected")

    asyncio.run(run())
