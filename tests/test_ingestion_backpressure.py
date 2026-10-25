"""Exercise real intake, extraction and source claims under a synthetic burst."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from test_remaining_concerns import executor_for
from test_telegram_source import FakeMessage, FakeTelegramClient
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro import telegram_source
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class BurstModel:
    def __init__(self):
        self.active = self.peak = 0
        self.two_started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active >= 2:
            self.two_started.set()
        try:
            await self.release.wait()
            return LLMResponse(
                json.dumps({"actionable": False, "reason": "Commentary"})
            )
        finally:
            self.active -= 1

    async def close(self):
        pass


@pytest.mark.parametrize(
    "intake,cancellation",
    [
        ("live", "none"),
        ("live", "waiting"),
        ("live", "active"),
        ("catchup", "none"),
        ("retry", "none"),
        ("album", "none"),
    ],
)
def test_live_burst_bounds_model_calls_and_downloads_before_claiming_work(
    tmp_path, intake, cancellation
):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        provider = BurstModel()
        downloaded = []

        class ImageMessage(FakeMessage):
            async def download_media(self, *, file):
                downloaded.append(self.id)
                return await super().download_media(file=file)

        client = FakeTelegramClient([])
        messages = [
            ImageMessage(
                message_id=i,
                published_at=datetime.now(UTC) - timedelta(seconds=3),
                text=f"Commentary {i}",
                image=b"synthetic chart",
            )
            for i in range(1, 9)
        ]
        for message in messages:
            message.chat = client.entity

        async def retry_ids(_channel):
            return (1,)

        with executor_for(ExchangeTransport()) as executor:
            service, bot, _ = workflow(store, executor, provider)
            with patch.object(telegram_source, "TelegramClient", return_value=client):
                source = telegram_source.TelegramSource(
                    api_id=1,
                    api_hash="synthetic",
                    session_name="synthetic",
                    channels=[-1001234567890],
                    on_message=service.on_message,
                    retry_message_ids=retry_ids if intake == "retry" else None,
                )
            with patch.object(bot._bot, "send_message", new=AsyncMock()):
                await source.start()
                live = client.handlers[1]
                client.messages = [messages[0]]
                if intake == "catchup":
                    first = source.poll_once()
                elif intake == "retry":
                    first = source._retry_failed_messages(
                        client.entity, messages[0].chat_id
                    )
                elif intake == "album":
                    messages[0].grouped_id = 77
                    first = client.handlers[0](
                        SimpleNamespace(messages=[messages[0]], chat=client.entity)
                    )
                else:
                    first = live(SimpleNamespace(message=messages[0]))
                tasks = [
                    asyncio.create_task(first),
                    *(
                        asyncio.create_task(live(SimpleNamespace(message=message)))
                        for message in messages[1:]
                    ),
                ]
                try:
                    await asyncio.wait_for(provider.two_started.wait(), timeout=5)
                    assert len(downloaded) == 2, (
                        f"Burst downloaded {len(downloaded)} images before bounded model work drained"
                    )
                    cancelled_index = -1 if cancellation == "waiting" else 0
                    if cancellation != "none":
                        tasks[cancelled_index].cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await tasks[cancelled_index]
                    if cancellation == "waiting":
                        assert 8 not in downloaded
                        with sqlite3.connect(database) as db:
                            assert (
                                db.execute(
                                    "SELECT 1 FROM source_messages WHERE message_id = 8"
                                ).fetchone()
                                is None
                            )
                    provider.release.set()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    if intake == "retry":
                        assert results[0] == {1: True}
                        results = results[1:]
                    assert sum(
                        isinstance(x, asyncio.CancelledError) for x in results
                    ) == int(cancellation != "none")
                    assert all(
                        x is None or isinstance(x, asyncio.CancelledError)
                        for x in results
                    )
                    if cancellation == "active":
                        with sqlite3.connect(database) as db:
                            assert db.execute(
                                "SELECT status FROM source_messages WHERE message_id = 1"
                            ).fetchone() == ("PROCESSING",)
                            db.execute(
                                "UPDATE source_messages SET updated_at = datetime('now', '-1 hour') WHERE message_id = 1"
                            )
                    if cancellation != "none":
                        await live(SimpleNamespace(message=messages[cancelled_index]))
                    assert provider.peak <= 2
                    expected_downloads = list(range(1, 9))
                    if cancellation == "active":
                        expected_downloads.append(1)
                    assert sorted(downloaded) == sorted(expected_downloads)
                    with sqlite3.connect(database) as db:
                        assert db.execute(
                            "SELECT status, COUNT(*) FROM source_messages GROUP BY status"
                        ).fetchall() == [("COMPLETED", 8)]
                finally:
                    provider.release.set()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await source.disconnect()
                    await bot.close()

    asyncio.run(run())
