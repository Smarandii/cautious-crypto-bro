import asyncio
import sqlite3

from cautious_crypto_bro.storage import (
    MIGRATION_4_TO_5_SQL,
    IntentStore,
)


def assert_v6(
    database,
) -> None:
    with sqlite3.connect(database) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]

        columns = {
            row[1]
            for row in db.execute(
                """
                PRAGMA table_info(
                    position_strategies
                )
                """
            )
        }

    assert version == 6
    assert "protected_stop_loss" in columns
    assert "trailing_distance" in columns


def test_schema_migrates_v4_to_v6(
    tmp_path,
) -> None:
    database = tmp_path / "legacy-v4.sqlite3"

    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version = 4")

    asyncio.run(IntentStore(database).initialize())

    assert_v6(database)


def test_schema_migrates_v5_to_v6(
    tmp_path,
) -> None:
    database = tmp_path / "legacy-v5.sqlite3"

    with sqlite3.connect(database) as db:
        db.executescript(MIGRATION_4_TO_5_SQL)

        db.execute("PRAGMA user_version = 5")

    asyncio.run(IntentStore(database).initialize())

    assert_v6(database)
