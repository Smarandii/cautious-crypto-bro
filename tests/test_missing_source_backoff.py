"""Missing Telegram posts back off durably without blocking available work."""

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from test_telegram_source import FakeMessage, FakeTelegramClient

from cautious_crypto_bro import telegram_source
from cautious_crypto_bro.domain import SourceMessage
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration
CHANNEL = -1001234567890


def post(message_id):
    now = datetime.now(UTC) - timedelta(hours=6)
    return SourceMessage(
        channel_id=CHANNEL,
        channel_title="Test",
        message_id=message_id,
        published_at=now,
        received_at=now,
        text="Post",
    )


class RecordingClient(FakeTelegramClient):
    def __init__(self, messages):
        super().__init__(messages)
        self.lookups = []
        self.failed = False

    async def get_messages(self, entity, *, ids):
        self.lookups.append(tuple(ids))
        if self.failed:
            raise OSError("Synthetic transport outage")
        return await super().get_messages(entity, ids=ids)


def reader(monkeypatch, client, store, seen):
    monkeypatch.setattr(telegram_source, "TelegramClient", lambda *a, **kw: client)

    async def retry_ids(channel):
        return await store.get_retryable_source_message_ids(channel, lease_seconds=300)

    async def unavailable(channel, ids):
        await store.defer_unavailable_source_messages(channel, ids, lease_seconds=300)

    async def receive(incoming):
        token = await store.claim_source(incoming.source, lease_seconds=300)
        assert token is not None
        await store.mark_source_completed(incoming.source, token)
        seen.append(incoming.source.message_id)

    return telegram_source.TelegramSource(
        api_id=1,
        api_hash="fake",
        session_name="fake",
        channels=[CHANNEL],
        on_message=receive,
        retry_message_ids=retry_ids,
        defer_unavailable_messages=unavailable,
    )


def test_missing_retry_backoff_survives_restart_and_recovers_after_one_day(
    monkeypatch, tmp_path, caplog
):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        for message_id in (1, 2):
            token = await store.claim_source(post(message_id), lease_seconds=300)
            await store.mark_source_failed(post(message_id), token, "Temporary failure")
        old = post(1).published_at
        client = RecordingClient(
            [
                FakeMessage(message_id=2, published_at=old, text="available"),
                FakeMessage(message_id=3, published_at=old, text="history boundary"),
            ]
        )
        seen = []
        source = reader(monkeypatch, client, store, seen)
        await source.start()
        await source.poll_once()
        assert client.lookups == [(1, 2)]
        assert seen == [2]
        assert caplog.text.count("Retryable Telegram posts unavailable") == 1

        reopened = IntentStore(database)
        source = reader(monkeypatch, client, reopened, seen)
        await source.start()
        await source.poll_once()
        assert client.lookups == [(1, 2)]
        async with aiosqlite.connect(database) as db:
            row = await (
                await db.execute(
                    "SELECT status, attempt_count, last_error FROM source_messages WHERE message_id=1"
                )
            ).fetchone()
            assert row[0:2] == ("UNAVAILABLE", 1)
            assert "24 hours" in row[2]
            await db.execute(
                "UPDATE source_messages SET updated_at=datetime('now','-25 hours') WHERE message_id=1"
            )
            await db.commit()
        await source.poll_once()
        await source.poll_once()
        assert client.lookups == [(1, 2), (1,)]
        async with aiosqlite.connect(database) as db:
            await db.execute(
                "UPDATE source_messages SET updated_at=datetime('now','-25 hours') WHERE message_id=1"
            )
            await db.commit()
        client.messages.append(
            FakeMessage(message_id=1, published_at=old, text="restored")
        )
        await source.poll_once()
        client.messages.append(
            FakeMessage(message_id=4, published_at=datetime.now(UTC), text="new")
        )
        await source.poll_once()
        assert client.lookups == [(1, 2), (1,), (1,)]
        assert seen == [2, 1, 4]
        assert source._watermarks[CHANNEL] == 4
        assert (
            await reopened.get_retryable_source_message_ids(CHANNEL, lease_seconds=300)
            == ()
        )

    asyncio.run(run())


@pytest.mark.parametrize("state", ["FAILED", "STALE", "PROCESSING", "COMPLETED"])
def test_missing_deferral_preserves_active_claims_and_source_payload(tmp_path, state):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        item = post(1)
        token = await store.claim_source(item, lease_seconds=300)
        if state == "FAILED":
            await store.mark_source_failed(item, token, "failure")
        elif state == "COMPLETED":
            await store.mark_source_completed(item, token)
        elif state == "STALE":
            async with aiosqlite.connect(database) as db:
                await db.execute(
                    "UPDATE source_messages SET updated_at=datetime('now','-1 hour')"
                )
                await db.commit()
        await store.defer_unavailable_source_messages(CHANNEL, (1,), lease_seconds=300)
        async with aiosqlite.connect(database) as db:
            row = await (
                await db.execute(
                    "SELECT status, claim_token, payload_json, attempt_count FROM source_messages"
                )
            ).fetchone()
        assert row[0] == ("UNAVAILABLE" if state in {"FAILED", "STALE"} else state)
        assert row[1] == (token if state == "PROCESSING" else None)
        assert row[2:] == (item.model_dump_json(), 1)

    asyncio.run(run())


def test_transport_failure_does_not_classify_posts_as_missing(monkeypatch, tmp_path):
    async def run():
        store = IntentStore(tmp_path / "state.db")
        await store.initialize()
        token = await store.claim_source(post(1), lease_seconds=300)
        await store.mark_source_failed(post(1), token, "failure")
        client = RecordingClient([])
        client.failed = True
        await reader(monkeypatch, client, store, []).start()
        assert await store.get_retryable_source_message_ids(
            CHANNEL, lease_seconds=300
        ) == (1,)

    asyncio.run(run())
