"""Real application workflows; fake only model, Telegram, and exchange transports."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, make_intent
from test_source_recovery import RecoveringClient, harness, notice
from test_telegram_source import FakeMessage, FakeTelegramClient

from cautious_crypto_bro import telegram_source
from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.domain import (
    AutoApprovalMode,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
    SourceMessage,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.service import SignalService
from cautious_crypto_bro.signal_context import SignalContextProvider
from cautious_crypto_bro.signals.extraction import IntentExtractor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class ModelTransport:
    def __init__(self):
        self.fail_image_once = True
        self.image_requests = 0

    async def complete(self, request):
        if "Close BTCUSDT now" in request.user_text:
            return LLMResponse(
                json.dumps(
                    {
                        "actionable": True,
                        "reason": "Explicit close",
                        "position_actions": [
                            {
                                "symbol": "BTCUSDT",
                                "action": "CLOSE",
                                "expected_side": "LONG",
                                "evidence_text": "Close BTCUSDT now",
                                "summary": "Close now",
                                "confidence": 1,
                            }
                        ],
                    }
                )
            )
        if request.images:
            self.image_requests += 1
            if self.fail_image_once:
                self.fail_image_once = False
                raise RuntimeError("Temporary model outage")
            return LLMResponse(
                json.dumps(
                    {
                        "actionable": True,
                        "reason": "Explicit entry",
                        "intents": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "LONG",
                                "entry": "MARKET",
                                "stop_loss": 90,
                                "relation": "NEW",
                                "summary": "Enter now",
                                "confidence": 1,
                            }
                        ],
                    }
                )
            )
        return LLMResponse(json.dumps({"actionable": False, "reason": "Commentary"}))

    async def close(self):
        pass


class ExchangeTransport(Exchange):
    def handle(self, request):
        if request.url.path == "/v5/position/closed-pnl":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"list": [], "nextPageCursor": ""},
                },
            )
        return super().handle(request)


def workflow(
    store, executor, provider, *, mode=AutoApprovalMode.OPEN_ONLY, max_age=900
):
    planner = ExecutionPlanner()
    lock = asyncio.Lock()
    coordinator = ExecutionCoordinator(
        store=store,
        executor=executor,
        planner=planner,
        max_age_seconds=max_age,
        execution_lock=lock,
    )
    bot = ApprovalBot(
        token="123456:synthetic-integration-token",
        approval_chat_id=1,
        approver_user_id=1,
        store=store,
        executor=coordinator,
    )
    service = SignalService(
        store=store,
        extractor=IntentExtractor(provider=provider, cache_identity="test"),
        planner=planner,
        executor=executor,
        approval_bot=bot,
        coordinator=coordinator,
        context_provider=SignalContextProvider(store=store, executor=executor),
        auto_approval_mode=mode,
        intent_max_age_seconds=max_age,
    )
    supervisor = PositionSupervisor(store=store, executor=executor, mutation_lock=lock)
    return service, bot, supervisor


@pytest.mark.parametrize("path", ["catchup", "live", "startup"])
def test_durable_model_failure_reaches_operator_without_blocking_later_posts(
    tmp_path, monkeypatch, path
):
    class OutageModel(ModelTransport):
        unavailable = True

        def __init__(self):
            super().__init__()
            self.startup_blocked = asyncio.Event()
            self.release_startup = asyncio.Event()

        async def complete(self, request):
            if path == "startup" and not request.images and self.unavailable:
                self.startup_blocked.set()
                await self.release_startup.wait()
            if self.unavailable and request.images:
                self.image_requests += 1
                raise RuntimeError("Temporary model outage")
            self.fail_image_once = False
            return await super().complete(request)

    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        provider = OutageModel()
        client = RecoveringClient(authorized=True)

        async def retry_ids(channel):
            return await store.get_retryable_source_message_ids(
                channel, lease_seconds=300
            )

        h = harness(monkeypatch, tmp_path, client, retry_ids=retry_ids)
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, provider)
            h.source._on_message = service.on_message
            messages = [
                FakeMessage(
                    message_id=1,
                    published_at=datetime.now(UTC),
                    text="Enter BTCUSDT LONG at market, stop 90",
                    image=b"chart",
                ),
                FakeMessage(
                    message_id=2,
                    published_at=datetime.now(UTC),
                    text="Commentary",
                ),
            ]
            if path == "startup":
                client.messages = messages
                h.source._watermarks[-1001234567890] = 0
                h.recovery._outage = True
            task = asyncio.create_task(h.recovery.run())
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    if path == "startup":
                        await asyncio.wait_for(provider.startup_blocked.wait(), 3)
                        await notice(h, "HUMAN REVIEW REQUIRED")
                        assert not client.listening.is_set()
                        assert not any(
                            "reader restored" in text for _, text in h.messages
                        )
                        provider.release_startup.set()
                    await asyncio.wait_for(client.listening.wait(), 3)
                    client.messages = messages
                    if path == "live":
                        await h.source._handle_messages(
                            client.messages[:1], chat=client.entity
                        )
                        await notice(h, "HUMAN REVIEW REQUIRED")
                    if path != "startup":
                        await h.source.poll_once()
                    async with aiosqlite.connect(database) as db:
                        rows = await (
                            await db.execute(
                                "SELECT message_id,status FROM source_messages ORDER BY message_id"
                            )
                        ).fetchall()
                    assert rows == [(1, "FAILED"), (2, "COMPLETED")]
                    assert h.source._watermarks[-1001234567890] == 2
                    assert exchange.size == 0
                    if path == "catchup":
                        await notice(h, "HUMAN REVIEW REQUIRED")
                    assert h.recovery.state == "DEGRADED"
                    attempts = provider.image_requests
                    provider.unavailable = False
                    await h.source.poll_once()
                    await notice(h, "reader restored")
                    assert h.recovery.state == "ONLINE" and not h.source.read_warnings()
                    assert provider.image_requests == attempts + 1
                    assert not await retry_ids(-1001234567890)
                    assert exchange.size > 0 and exchange.stop == Decimal("90")
                    assert client.starts == 1 and client.qrs == 0
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await bot.close()
                await h.bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("album", [False, True])
@pytest.mark.parametrize("retry_outcome", ["model_failure", "media_failure", "success"])
def test_live_retry_overlapping_history_is_attempted_once_per_poll(
    tmp_path, monkeypatch, album, retry_outcome
):
    class OutageModel(ModelTransport):
        unavailable = True

        async def complete(self, request):
            if self.unavailable and request.images:
                self.image_requests += 1
                raise RuntimeError("Synthetic model outage")
            self.fail_image_once = False
            return await super().complete(request)

    class CountedMessage(FakeMessage):
        downloads = 0
        fail_download = False

        async def download_media(self, *, file):
            self.downloads += 1
            if self.fail_download:
                raise OSError("Synthetic media outage")
            return await super().download_media(file=file)

    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        provider = OutageModel()
        client = FakeTelegramClient([])
        monkeypatch.setattr(telegram_source, "TelegramClient", lambda *a, **kw: client)

        async def retry_ids(channel):
            return await store.get_retryable_source_message_ids(
                channel, lease_seconds=300
            )

        with executor_for(ExchangeTransport()) as executor:
            service, bot, _ = workflow(store, executor, provider)
            source = telegram_source.TelegramSource(
                api_id=1,
                api_hash="synthetic",
                session_name="synthetic",
                channels=[-1001234567890],
                on_message=service.on_message,
                retry_message_ids=retry_ids,
            )
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await source.start()
                    images = [
                        CountedMessage(
                            message_id=1,
                            published_at=datetime.now(UTC),
                            text="Enter BTCUSDT LONG at market, stop 90",
                            grouped_id=99 if album else None,
                            image=b"chart",
                        )
                    ]
                    if album:
                        images.append(
                            CountedMessage(
                                message_id=2,
                                published_at=datetime.now(UTC),
                                grouped_id=99,
                                image=b"position",
                            )
                        )
                    client.messages = [
                        *images,
                        FakeMessage(
                            message_id=3,
                            published_at=datetime.now(UTC),
                            text="Commentary",
                        ),
                    ]
                    await source._handle_messages(images, chat=client.entity)
                    assert provider.image_requests == 1
                    provider.unavailable = retry_outcome != "success"
                    images[0].fail_download = retry_outcome == "media_failure"
                    before = sum(message.downloads for message in images)
                    await source.poll_once()
                    expected_downloads = 1 if images[0].fail_download else len(images)
                    assert (
                        sum(message.downloads for message in images) - before
                        == expected_downloads
                    )
                    assert provider.image_requests == (
                        1 if images[0].fail_download else 2
                    )
                    assert source._watermarks[-1001234567890] == (
                        0 if images[0].fail_download else 3
                    )
                    assert bool(await retry_ids(-1001234567890)) == provider.unavailable
                    if provider.unavailable:
                        assert source.read_warnings()
                    provider.unavailable = False
                    images[0].fail_download = False
                    await source.poll_once()
                    assert not await retry_ids(-1001234567890)
                    assert not source.read_warnings()
                    assert source._watermarks[-1001234567890] == 3
                    async with aiosqlite.connect(database) as db:
                        rows = await (
                            await db.execute(
                                "SELECT message_id,status FROM source_messages ORDER BY message_id"
                            )
                        ).fetchall()
                    assert rows == [(1, "COMPLETED"), (3, "COMPLETED")]
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("retry_state", ["failed", "stale_claim", "active_claim"])
def test_failed_album_retries_after_restart_without_blocking_later_posts(
    tmp_path, retry_state
):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        provider = ModelTransport()
        client = FakeTelegramClient([])
        channel_id = -1001234567890

        async def source_for(service, current_store):
            async def retry_ids(channel):
                return await current_store.get_retryable_source_message_ids(
                    channel,
                    lease_seconds=300,
                )

            source = telegram_source.TelegramSource(
                api_id=1,
                api_hash="fake",
                session_name="fake",
                channels=[channel_id],
                on_message=service.on_message,
                retry_message_ids=retry_ids,
            )
            await source.start()
            return source

        exchange = ExchangeTransport()
        with (
            executor_for(exchange) as executor,
            patch.object(
                telegram_source,
                "TelegramClient",
                return_value=client,
            ),
        ):
            service, bot, _ = workflow(store, executor, provider)
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    source = await source_for(service, store)
                    old = datetime.now(UTC) - timedelta(hours=6)
                    client.messages = [
                        FakeMessage(
                            message_id=1,
                            published_at=old,
                            text="Enter BTCUSDT LONG at market, stop 90",
                            grouped_id=99,
                            image=b"chart",
                        ),
                        FakeMessage(
                            message_id=2,
                            published_at=old,
                            grouped_id=99,
                            image=b"position",
                        ),
                        FakeMessage(message_id=3, published_at=old, text="Commentary"),
                    ]
                    await source.poll_once()
                    async with aiosqlite.connect(database) as db:
                        rows = await (
                            await db.execute(
                                "SELECT message_id, status FROM source_messages ORDER BY message_id"
                            )
                        ).fetchall()
                    assert rows == [(1, "FAILED"), (3, "COMPLETED")]
                    assert exchange.size == 0
                    if retry_state != "failed":
                        async with aiosqlite.connect(database) as db:
                            await db.execute(
                                "UPDATE source_messages SET status='PROCESSING', "
                                "claim_token='other-worker', updated_at=datetime('now', ?) "
                                "WHERE message_id=1",
                                (
                                    "-1 hour"
                                    if retry_state == "stale_claim"
                                    else "0 seconds",
                                ),
                            )
                            await db.commit()
            finally:
                await bot.close()

            restarted_store = IntentStore(database)
            service, bot, supervisor = workflow(restarted_store, executor, provider)
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    restarted = await source_for(service, restarted_store)
                    if retry_state == "active_claim":
                        assert provider.image_requests == 1
                        assert exchange.size == 0
                        async with aiosqlite.connect(database) as db:
                            await db.execute(
                                "UPDATE source_messages SET updated_at=datetime('now', '-1 hour') "
                                "WHERE message_id=1"
                            )
                            await db.commit()
                        await restarted.poll_once()
                    await supervisor.reconcile_once()
                    await restarted.poll_once()
                    # Duplicate live delivery must not submit another entry.
                    await restarted._handle_messages(
                        client.messages[:2], chat=client.entity
                    )
                intents = await restarted_store.get_recent_source_intents(channel_id)
                assert len(intents) == 1
                assert intents[0].status is IntentStatus.EXECUTED
                assert provider.image_requests == 2
                assert exchange.size > 0 and exchange.stop == Decimal("90")
                assert len(exchange.exits()) == 3
                assert (
                    await restarted_store.get_retryable_source_message_ids(
                        channel_id,
                        lease_seconds=300,
                    )
                    == ()
                )
            finally:
                await bot.close()

    asyncio.run(run())


def test_retry_does_not_turn_old_close_caption_into_fresh_execution(tmp_path):
    async def run():
        store = IntentStore(tmp_path / "state.db")
        await store.initialize()
        old = datetime.now(UTC) - timedelta(hours=6)
        client = FakeTelegramClient(
            [
                FakeMessage(
                    message_id=1,
                    published_at=old,
                    text="Close BTCUSDT now",
                )
            ]
        )
        original = SourceMessage(
            channel_id=-1001234567890,
            channel_title="Test",
            message_id=1,
            published_at=old,
            received_at=old,
            text="Close BTCUSDT now",
        )
        token = await store.claim_source(original, lease_seconds=300)
        await store.mark_source_failed(original, token, "Temporary model outage")
        exchange = ExchangeTransport()
        exchange.size = Decimal("1")
        exchange.stop = Decimal("90")
        with (
            executor_for(exchange) as executor,
            patch.object(
                telegram_source,
                "TelegramClient",
                return_value=client,
            ),
        ):
            service, bot, _ = workflow(
                store, executor, ModelTransport(), mode=AutoApprovalMode.ALL
            )

            async def retry_ids(channel):
                return await store.get_retryable_source_message_ids(
                    channel, lease_seconds=300
                )

            source = telegram_source.TelegramSource(
                api_id=1,
                api_hash="fake",
                session_name="fake",
                channels=[original.channel_id],
                on_message=service.on_message,
                retry_message_ids=retry_ids,
            )
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await source.start()
                async with aiosqlite.connect(tmp_path / "state.db") as db:
                    row = await (
                        await db.execute("SELECT status, error FROM position_actions")
                    ).fetchone()
                assert row is not None and row[0] == "FAILED"
                assert "stale" in row[1]
                assert exchange.size == Decimal("1")
                assert exchange.orders == {}
            finally:
                await bot.close()

    asyncio.run(run())


def test_disabled_auto_recovery_clears_backlog_beyond_default_page(tmp_path):
    async def run():
        store = IntentStore(tmp_path / "state.db")
        await store.initialize()
        intents = tuple(make_intent() for _ in range(205))
        actions = tuple(
            PositionActionIntent(
                source=intents[0].source,
                symbol="BTCUSDT",
                action=PositionActionType.CLOSE,
                expected_side=Side.LONG,
                summary="Close now",
                confidence=1,
            )
            for _ in range(205)
        )
        # Seed the actual durable queue atomically; no fake repository behavior.
        async with aiosqlite.connect(tmp_path / "state.db") as db:
            for table, column, records in (
                ("intents", "intent_id", intents),
                ("position_actions", "action_id", actions),
            ):
                await db.executemany(
                    f"INSERT INTO {table}({column}, channel_id, message_id, payload_json, "
                    "status, approval_mode, created_at, updated_at) "
                    "VALUES (?, 0, 1, ?, 'PENDING', 'AUTO', ?, ?)",
                    [
                        (
                            str(getattr(item, column)),
                            item.model_dump_json(),
                            item.created_at.isoformat(),
                            item.created_at.isoformat(),
                        )
                        for item in records
                    ],
                )
            await db.commit()

        service = SignalService(
            store=store,
            extractor=None,
            planner=None,
            executor=None,
            approval_bot=None,
            coordinator=None,
            context_provider=None,
            auto_approval_mode=AutoApprovalMode.DISABLED,
        )
        await service.recover_auto_execution()
        async with aiosqlite.connect(tmp_path / "state.db") as db:
            for table in ("intents", "position_actions"):
                remaining = await (
                    await db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE status='PENDING'"
                    )
                ).fetchone()
                assert remaining == (0,)
                failed = await (
                    await db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE status='FAILED'"
                    )
                ).fetchone()
                assert failed == (205,)

    asyncio.run(run())
