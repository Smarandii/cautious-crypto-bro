"""Whole-post media rejection with real intake, service and SQLite."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from test_remaining_concerns import executor_for
from test_telegram_source import FakeMessage, FakeTelegramClient
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro import telegram_replay, telegram_source
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.openrouter import IntentExtractor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("sizes", [(9,), (5, 4), (8,), (4, 4)])
def test_image_budget_applies_to_whole_post_and_accepts_exact_boundary(sizes):
    async def run():
        client = FakeTelegramClient([])
        messages = [
            FakeMessage(
                message_id=i,
                published_at=datetime.now(UTC),
                text=f"caption {i}",
                image=b"x" * size,
            )
            for i, size in enumerate(sizes, start=1)
        ]
        with patch.object(telegram_source, "MAX_POST_IMAGE_BYTES", 8, create=True):
            post = await telegram_source.telegram_messages_to_post(
                messages, chat=client.entity
            )
        if sum(sizes) > 8:
            assert post.rejection_reason, "Oversized post was accepted"
            assert not post.images
            with pytest.raises(ValueError, match="image budget exceeded"):
                await IntentExtractor(provider=object(), cache_identity="test").extract(
                    post
                )
        else:
            assert post.rejection_reason is None
            assert sum(len(image.data) for image in post.images) == 8
        assert post.source.text == "\n".join(m.message for m in messages)

    asyncio.run(run())


def test_explicit_replay_rejects_oversized_post_before_returning_partial_input(
    tmp_path,
):
    async def run():
        session = tmp_path / "synthetic.session"
        with sqlite3.connect(session) as db:
            db.execute("CREATE TABLE synthetic(value TEXT)")
        message = FakeMessage(
            message_id=1, published_at=datetime.now(UTC), image=b"123456789"
        )

        class ReplayClient(FakeTelegramClient):
            async def connect(self):
                pass

            async def is_user_authorized(self):
                return True

            async def get_messages(self, entity, *, ids):
                return (
                    message if ids == 1 else await super().get_messages(entity, ids=ids)
                )

        client = ReplayClient([message])
        with (
            patch.object(telegram_replay, "TelegramClient", return_value=client),
            patch.object(telegram_source, "MAX_POST_IMAGE_BYTES", 8),
        ):
            with pytest.raises(
                telegram_replay.TelegramReplayError, match="image budget exceeded"
            ):
                await telegram_replay.fetch_telegram_post(
                    url="https://t.me/example/1",
                    api_id=1,
                    api_hash="synthetic",
                    session_name=str(session),
                )

    asyncio.run(run())


def test_rejection_recording_failure_preserves_retryable_source_and_cursor(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        with sqlite3.connect(database) as db:
            db.execute(
                "CREATE TRIGGER reject_completion BEFORE UPDATE ON source_messages "
                "WHEN NEW.status = 'COMPLETED' BEGIN SELECT RAISE(ABORT, 'recording unavailable'); END"
            )
        message = FakeMessage(
            message_id=1, published_at=datetime.now(UTC), image=b"123456789"
        )
        client = FakeTelegramClient([message])
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, object())

            async def retry_ids(channel):
                return await store.get_retryable_source_message_ids(
                    channel, lease_seconds=300
                )

            with patch.object(telegram_source, "TelegramClient", return_value=client):
                intake = telegram_source.TelegramSource(
                    api_id=1,
                    api_hash="synthetic",
                    session_name="synthetic",
                    channels=[message.chat_id],
                    on_message=service.on_message,
                    startup_lookback_hours=1,
                    retry_message_ids=retry_ids,
                )
            try:
                with patch.object(telegram_source, "MAX_POST_IMAGE_BYTES", 8):
                    await intake.start()
                    assert intake._watermarks[message.chat_id] == 0
                    with sqlite3.connect(database) as db:
                        assert db.execute(
                            "SELECT status FROM source_messages"
                        ).fetchall() == [("PROCESSING",)]
                        db.execute("DROP TRIGGER reject_completion")
                        db.execute(
                            "UPDATE source_messages SET updated_at = datetime('now', '-1 hour')"
                        )
                    assert await retry_ids(message.chat_id) == (1,)
                    await intake.poll_once()
                with sqlite3.connect(database) as db:
                    row = db.execute(
                        "SELECT status, last_error FROM source_messages"
                    ).fetchone()
                    assert row[0] == "COMPLETED" and "image budget exceeded" in row[1]
                assert intake._watermarks[message.chat_id] == 1
                assert await retry_ids(message.chat_id) == ()
                assert not exchange.requests
            finally:
                await intake.disconnect()
                await bot.close()

    asyncio.run(run())


def test_stream_stops_before_retaining_excess_bytes_or_reading_remaining_chunks():
    async def run():
        attempted = []
        retained = []

        class ChunkedImage(FakeMessage):
            async def download_media(self, *, file):
                assert file is not bytes
                for chunk in (
                    memoryview(b"1234"),
                    bytearray(b"5678"),
                    b"9",
                    b"never read",
                ):
                    attempted.append(chunk)
                    try:
                        file.write(chunk)
                    except ValueError:
                        retained.append(len(file.getvalue()))
                        raise
                return file

        message = ChunkedImage(message_id=1, published_at=datetime.now(UTC), image=b"x")
        with patch.object(telegram_source, "MAX_POST_IMAGE_BYTES", 8):
            post = await telegram_source.telegram_messages_to_post(
                [message], chat=FakeTelegramClient([]).entity
            )
        assert post.rejection_reason and not post.images
        assert len(attempted) == 3 and retained == [8]

    asyncio.run(run())


@pytest.mark.parametrize("album", [False, True])
def test_rejection_is_durable_does_not_reach_model_and_does_not_block_later_posts(
    tmp_path, album, caplog
):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        downloads = []
        requests = []

        class ImageMessage(FakeMessage):
            async def download_media(self, *, file):
                downloads.append(self.id)
                return await super().download_media(file=file)

        class Model:
            async def complete(self, request):
                requests.append(request)
                return LLMResponse(
                    json.dumps({"actionable": False, "reason": "Commentary"})
                )

            async def close(self):
                pass

        sizes = [4, 5, 1] if album else [9]
        oversized = [
            ImageMessage(
                message_id=i,
                published_at=datetime.now(UTC) - timedelta(seconds=3),
                grouped_id=77 if album else None,
                text=f"caption {i}" if album else "",
                image=b"x" * size,
            )
            for i, size in enumerate(sizes, start=1)
        ]
        later = FakeMessage(
            message_id=len(sizes) + 1,
            published_at=datetime.now(UTC),
            text="Later commentary",
        )
        client = FakeTelegramClient([*oversized, later])
        with executor_for(ExchangeTransport()) as executor:
            service, bot, _ = workflow(store, executor, Model())

            async def retry_ids(channel):
                return await store.get_retryable_source_message_ids(
                    channel, lease_seconds=300
                )

            with patch.object(telegram_source, "TelegramClient", return_value=client):
                intake = telegram_source.TelegramSource(
                    api_id=1,
                    api_hash="synthetic",
                    session_name="synthetic",
                    channels=[later.chat_id],
                    on_message=service.on_message,
                    startup_lookback_hours=1,
                    retry_message_ids=retry_ids,
                )
            try:
                with (
                    patch.object(telegram_source, "MAX_POST_IMAGE_BYTES", 8),
                    patch.object(bot._bot, "send_message", new=AsyncMock()),
                ):
                    await intake.start()
                    await intake.poll_once()
                assert len(requests) == 1
                assert downloads == ([1, 2] if album else [1])
                assert intake._watermarks[later.chat_id] == later.id
                assert await retry_ids(later.chat_id) == ()
                with sqlite3.connect(database) as db:
                    rows = db.execute(
                        "SELECT message_id, status, last_error, payload_json FROM source_messages ORDER BY message_id"
                    ).fetchall()
                    assert len(rows) == 2
                    assert rows[0][:2] == (1, "COMPLETED")
                    assert "image budget exceeded" in rows[0][2]
                    assert json.loads(rows[0][3])["text"] == "\n".join(
                        m.message for m in oversized if m.message
                    )
                    assert rows[1][:3] == (later.id, "COMPLETED", None)
                    assert db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
                assert "Rejected Telegram post" in caplog.text
            finally:
                await intake.disconnect()
                await bot.close()

    asyncio.run(run())
