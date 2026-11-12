"""Delivery ownership survives concurrent recovery and interrupted sends."""

import asyncio
import sqlite3
from contextlib import closing

import aiosqlite
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro.domain import IntentStatus
from cautious_crypto_bro.signals.delivery import SignalDelivery
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "kind,table,id_column",
    [
        ("OPEN", "intents", "intent_id"),
        ("ACTION", "position_actions", "action_id"),
    ],
)
@pytest.mark.parametrize("stale", [False, True])
def test_manual_reconciliation_preserves_concurrent_changes(
    tmp_path, monkeypatch, kind, table, id_column, stale
):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        with closing(sqlite3.connect(database)) as db:
            for name, key in (
                ("intents", "intent_id"),
                ("position_actions", "action_id"),
            ):
                db.execute(
                    f"CREATE TABLE {name} ({key} TEXT PRIMARY KEY, status TEXT, "
                    "approval_mode TEXT, created_at TEXT, error TEXT, updated_at TEXT)"
                )
            db.execute(
                "CREATE TABLE manual_deliveries (record_id TEXT PRIMARY KEY, kind TEXT)"
            )
            db.executemany(
                f"INSERT INTO {table} ({id_column}, status, approval_mode, created_at) "
                "VALUES (?, 'PENDING', 'MANUAL', ?)",
                [
                    (
                        record_id,
                        "2020-01-01T00:00:00+00:00"
                        if stale
                        else "2099-01-01T00:00:00+00:00",
                    )
                    for record_id in ("status-changed", "delivery-created", "orphan")
                ],
            )
            db.commit()

        fetchall = aiosqlite.Cursor.fetchall

        async def fetch_then_change(cursor):
            rows = await fetchall(cursor)
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    f"UPDATE {table} SET status='EXECUTED' WHERE {id_column}='status-changed'"
                )
                db.execute(
                    "INSERT INTO manual_deliveries VALUES ('delivery-created', ?)",
                    (kind,),
                )
                db.commit()
            return rows

        monkeypatch.setattr(aiosqlite.Cursor, "fetchall", fetch_then_change)
        assert await store.reconcile_manual_deliveries() == (
            (0, 1) if stale else (1, 0)
        )
        with closing(sqlite3.connect(database)) as db:
            assert dict(db.execute(f"SELECT {id_column}, status FROM {table}")) == {
                "status-changed": "EXECUTED",
                "delivery-created": "PENDING",
                "orphan": "FAILED" if stale else "PENDING",
            }
            expected = {"delivery-created": kind}
            if not stale:
                expected["orphan"] = kind
            assert (
                dict(db.execute("SELECT record_id, kind FROM manual_deliveries"))
                == expected
            )

    asyncio.run(run())


@pytest.mark.parametrize("cancel_send", [False, True])
def test_delivery_lease_prevents_duplicate_send_until_completion_or_expiry(
    tmp_path, cancel_send
):
    class BlockingSender:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.sent_ids = []

        async def send_intent(self, intent, plan, **kwargs):
            self.sent_ids.append(intent.intent_id)
            self.started.set()
            await self.release.wait()

    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, plan, _, _ = await prepare(database, exchange, executor)
            sender = BlockingSender()
            first_owner = SignalDelivery(
                store=store, executor=executor, approval_bot=sender
            )
            second_owner = SignalDelivery(
                store=IntentStore(database), executor=executor, approval_bot=sender
            )
            first_send = asyncio.create_task(first_owner.manual(intent, plan))
            try:
                await asyncio.wait_for(sender.started.wait(), timeout=3)
                assert not await second_owner.manual(intent, plan)
                assert sender.sent_ids == [intent.intent_id]
                if cancel_send:
                    first_send.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first_send
                    assert not await second_owner.manual(intent, plan)
                    assert await store.pending_manual_deliveries() == ()
                    with closing(sqlite3.connect(database)) as db:
                        db.execute(
                            "UPDATE manual_deliveries SET retry_after = datetime('now', '-1 second')"
                        )
                        db.commit()
                    sender.release.set()
                    await second_owner.recover_manual()
                else:
                    sender.release.set()
                    assert await first_send
                await second_owner.recover_manual()
                assert not await second_owner.manual(intent, plan)
                assert sender.sent_ids == [intent.intent_id] * (2 if cancel_send else 1)
                assert (await store.get_intent(intent.intent_id)).status is (
                    IntentStatus.PENDING
                )
                assert exchange.orders == {}
            finally:
                sender.release.set()
                first_send.cancel()
                await asyncio.gather(first_send, return_exceptions=True)

    asyncio.run(run())
