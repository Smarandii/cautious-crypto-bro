import asyncio
import sqlite3

import aiosqlite
import pytest

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

    asyncio.run(run())


def test_pre_versioned_database_is_upgraded_without_data_loss(
    tmp_path,
) -> None:
    database = tmp_path / "state.sqlite3"

    with sqlite3.connect(database) as db:
        db.execute(
            """
            CREATE TABLE source_messages (
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (channel_id, message_id)
            )
            """
        )
        db.execute(
            """
            INSERT INTO source_messages(
                channel_id,
                message_id,
                payload_json
            )
            VALUES (?, ?, ?)
            """,
            (
                -1001234567890,
                42,
                '{"legacy":true}',
            ),
        )
        db.commit()

    async def run() -> None:
        store = IntentStore(database)
        await store.initialize()

        async with aiosqlite.connect(database) as db:
            cursor = await db.execute("PRAGMA user_version")
            assert (await cursor.fetchone())[0] == LATEST_SCHEMA_VERSION

            cursor = await db.execute(
                """
                SELECT
                    payload_json,
                    status,
                    attempt_count,
                    updated_at
                FROM source_messages
                WHERE channel_id = ?
                  AND message_id = ?
                """,
                (
                    -1001234567890,
                    42,
                ),
            )

            row = await cursor.fetchone()

            assert row is not None
            assert row[0] == '{"legacy":true}'
            assert row[1] == "COMPLETED"
            assert row[2] == 1
            assert row[3]

    asyncio.run(run())


def test_newer_database_schema_is_rejected(
    tmp_path,
) -> None:
    database = tmp_path / "state.sqlite3"

    with sqlite3.connect(database) as db:
        db.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")

    store = IntentStore(database)

    with pytest.raises(
        RuntimeError,
        match="Database schema is newer",
    ):
        asyncio.run(store.initialize())
