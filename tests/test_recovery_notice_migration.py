"""Schema-9 recovery notices upgrade without rewriting existing trading state."""

import asyncio
import sqlite3

import pytest
from test_recovery_notice_queue import executing_action, executing_records
from test_remaining_concerns import Exchange, executor_for

from cautious_crypto_bro.storage import LATEST_SCHEMA_VERSION, IntentStore

pytestmark = pytest.mark.integration


def snapshot(db):
    tables = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        if row[0] != "interrupted_execution_notices"
    ]
    return {
        name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
        for name in tables
    }


@pytest.mark.parametrize("fail_first", [False, True])
def test_schema9_upgrade_preserves_records_and_backfills_only_interrupted_notices(
    tmp_path, fail_first
):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, action = await executing_records(
                database, exchange, executor
            )
            ids = await store.quarantine_interrupted_executions()
            unrelated = await executing_action(store, 3)
            with sqlite3.connect(database) as db:
                db.execute(
                    "UPDATE position_actions SET status='UNCERTAIN', error='Synthetic unrelated outcome' "
                    "WHERE action_id=?",
                    (str(unrelated.action_id),),
                )
                db.execute("DROP TABLE interrupted_execution_notices")
                db.execute("PRAGMA user_version = 9")
                before = snapshot(db)
                if fail_first:
                    # Force failure after CREATE TABLE and the OPEN backfill.
                    db.execute(
                        "ALTER TABLE position_actions RENAME TO unavailable_position_actions"
                    )
            if fail_first:
                with pytest.raises(sqlite3.OperationalError):
                    await IntentStore(database).initialize()
                with sqlite3.connect(database) as db:
                    assert db.execute("PRAGMA user_version").fetchone() == (9,)
                    assert not db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='interrupted_execution_notices'"
                    ).fetchone()
                    db.execute(
                        "ALTER TABLE unavailable_position_actions RENAME TO position_actions"
                    )
                    assert snapshot(db) == before
            reopened = IntentStore(database)
            await reopened.initialize()
            with sqlite3.connect(database) as db:
                assert db.execute("PRAGMA user_version").fetchone() == (
                    LATEST_SCHEMA_VERSION,
                )
                assert snapshot(db) == before
                assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert await reopened.pending_recovery_notices() == ids
            await reopened.acknowledge_recovery_notices(*ids)
            await IntentStore(database).initialize()
            assert await reopened.pending_recovery_notices() == ((), ())
            assert not exchange.requests

    asyncio.run(run())
