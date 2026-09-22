import asyncio
import sqlite3

from cautious_crypto_bro.storage import (
    IntentStore,
)


def test_schema_migrates_v4_to_v5(
    tmp_path,
) -> None:
    database = tmp_path / "legacy.sqlite3"

    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version = 4")

    store = IntentStore(database)

    asyncio.run(store.initialize())

    with sqlite3.connect(database) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]

        table = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE
                type = 'table'
                AND name = 'position_strategies'
            """
        ).fetchone()

    assert version == 5
    assert table == ("position_strategies",)
