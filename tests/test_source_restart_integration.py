"""Chronological recovery survives process loss without auto-trading old posts."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from test_remaining_concerns import executor_for
from test_telegram_source import FakeMessage, FakeTelegramClient
from test_workflow_integration import ExchangeTransport, ModelTransport, workflow

from cautious_crypto_bro import telegram_source
from cautious_crypto_bro.domain import ApprovalMode, AutoApprovalMode
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration
CHANNEL = -1001234567890


class SideModel(ModelTransport):
    def __init__(self, side="LONG"):
        super().__init__()
        self.side = side

    async def complete(self, request):
        response = await super().complete(request)
        data = json.loads(response.content)
        for intent in data.get("intents", []):
            intent.update(side=self.side, stop_loss=90 if self.side == "LONG" else 110)
        return LLMResponse(json.dumps(data))


def reader(monkeypatch, client, store, handler, *, channels=(CHANNEL,)):
    monkeypatch.setattr(telegram_source, "TelegramClient", lambda *a, **k: client)
    return telegram_source.TelegramSource(
        api_id=1,
        api_hash="test",
        session_name="test",
        channels=list(channels),
        on_message=handler,
        startup_lookback_hours=5,
        load_history_cursor=store.get_source_history_cursor,
        save_history_cursor=store.save_source_history_cursor,
        source_completed=store.source_completed,
        retry_message_ids=lambda channel: store.get_retryable_source_message_ids(
            channel, lease_seconds=300
        ),
    )


def message(number, *, hours=0, text="Commentary", image=None, group=None):
    return FakeMessage(
        message_id=number,
        published_at=datetime.now(UTC) - timedelta(hours=hours),
        text=text,
        image=image,
        grouped_id=group,
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_old_entry_recovers_after_model_failure_without_automatic_orders(
    tmp_path, monkeypatch, side
):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        client = FakeTelegramClient(
            [
                message(1, hours=26),
                message(2, hours=6, image=b"chart"),
                message(3, text="Fresh commentary"),
            ]
        )
        provider = SideModel(side)
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, provider)
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    first = reader(monkeypatch, client, store, service.on_message)
                    await first.start()
                    assert await store.get_source_history_cursor(CHANNEL) == 1
                    assert first.read_warnings()
                    restarted = reader(
                        monkeypatch, client, IntentStore(path), service.on_message
                    )
                    await restarted.start()
                    assert await store.get_source_history_cursor(CHANNEL) == 3
                    assert not restarted.read_warnings()
                    assert not exchange.orders
                    with closing(sqlite3.connect(path)) as db:
                        record = db.execute(
                            "SELECT intent_id,approval_mode,payload_json FROM intents"
                        ).fetchone()
                        assert record[1] == "MANUAL"
                        payload = json.loads(record[2])
                        assert payload["source"]["recovery_review_reason"]
                        assert (
                            db.execute(
                                "SELECT status FROM source_messages WHERE message_id=2"
                            ).fetchone()[0]
                            == "COMPLETED"
                        )
                    text = "\n".join(
                        call.kwargs["text"] for call in sender.call_args_list
                    )
                    assert "RECOVERED POST" in text and "human review" in text.lower()
                    await reader(monkeypatch, client, store, service.on_message).start()
                    assert provider.image_requests == 2
                    assert (
                        sum(
                            "RECOVERED POST" in call.kwargs["text"]
                            for call in sender.call_args_list
                        )
                        == 1
                    )
                    intent = await store.get_intent(UUID(record[0]))
                    assert intent.approval_mode is ApprovalMode.MANUAL
            finally:
                await bot.close()

    asyncio.run(run())


def test_inflight_post_and_newer_live_delivery_do_not_advance_cursor(
    tmp_path, monkeypatch
):
    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        client = FakeTelegramClient([message(1, hours=26)])
        claims = {}
        seen = []

        async def receive(post):
            seen.append(post.source.message_id)
            token = await store.claim_source(post.source, lease_seconds=300)
            claims[post.source.message_id] = (post.source, token)
            if post.source.message_id != 2:
                await store.mark_source_completed(
                    post.source, token, reason="Fixture commentary"
                )

        source = reader(monkeypatch, client, store, receive)
        await source.start()
        client.messages.extend([message(2), message(3)])
        client.messages[-1].chat = client.entity
        await client.handlers[1](type("Event", (), {"message": client.messages[-1]})())
        assert await store.get_source_history_cursor(CHANNEL) == 1
        await source.poll_once()
        assert await store.get_source_history_cursor(CHANNEL) == 1
        assert await store.mark_source_completed(
            *claims[2], reason="Fixture commentary"
        )
        await reader(monkeypatch, client, store, receive).start()
        assert await store.get_source_history_cursor(CHANNEL) == 3
        assert seen == [3, 2]

    asyncio.run(run())


def test_cursor_commit_failure_retries_completed_post_without_redelivery(
    tmp_path, monkeypatch
):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        client = FakeTelegramClient([message(1, hours=26)])
        seen = []

        async def receive(post):
            seen.append(post.source.message_id)
            token = await store.claim_source(post.source, lease_seconds=300)
            await store.mark_source_completed(
                post.source, token, reason="Fixture commentary"
            )

        source = reader(monkeypatch, client, store, receive)
        await source.start()
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TRIGGER fail_cursor BEFORE UPDATE ON source_history_cursors BEGIN SELECT RAISE(ABORT,'synthetic cursor failure'); END"
            )
            db.commit()
        client.messages.append(message(2))
        await source.poll_once()
        assert await store.get_source_history_cursor(CHANNEL) == 1
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP TRIGGER fail_cursor")
            db.commit()
        await reader(monkeypatch, client, store, receive).start()
        assert await store.get_source_history_cursor(CHANNEL) == 2 and seen == [2]

    asyncio.run(run())


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_fresh_entry_keeps_automatic_policy(tmp_path, monkeypatch, side):
    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        client = FakeTelegramClient([message(1, hours=26), message(2, image=b"chart")])
        provider = SideModel(side)
        provider.fail_image_once = False
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, provider)
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await reader(monkeypatch, client, store, service.on_message).start()
                assert exchange.orders
                with closing(sqlite3.connect(store._database_path)) as db:
                    mode, payload = db.execute(
                        "SELECT approval_mode,payload_json FROM intents"
                    ).fetchone()
                    assert (
                        mode == "AUTO"
                        and json.loads(payload)["source"]["recovery_review_reason"]
                        is None
                    )
                assert await store.get_source_history_cursor(CHANNEL) == 2
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["CLOSE", "REDUCE", "CANCEL_ENTRIES"])
def test_old_lifecycle_instruction_is_recorded_without_approval(
    tmp_path, monkeypatch, action
):
    captions = {
        "CLOSE": "Close BTCUSDT now",
        "REDUCE": "Reduce BTCUSDT by 50%",
        "CANCEL_ENTRIES": "Cancel pending BTCUSDT entry orders",
    }

    class LifecycleModel(SideModel):
        async def complete(self, request):
            if captions[action] in request.user_text:
                item = {
                    "symbol": "BTCUSDT",
                    "action": action,
                    "expected_side": "LONG",
                    "evidence_text": captions[action],
                    "summary": "Explicit instruction",
                    "confidence": 1,
                }
                if action == "REDUCE":
                    item["close_pct"] = 50
                return LLMResponse(
                    json.dumps(
                        {
                            "actionable": True,
                            "reason": "Explicit current caption instruction",
                            "position_actions": [item],
                        }
                    )
                )
            return await super().complete(request)

    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        client = FakeTelegramClient(
            [
                message(1, hours=26),
                message(2, hours=7, image=b"chart"),
                message(3, hours=6, text=captions[action]),
            ]
        )
        provider = LifecycleModel()
        provider.fail_image_once = False
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store,
                executor,
                provider,
                mode=AutoApprovalMode.ALL,
            )
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    entry = await telegram_source.telegram_messages_to_post(
                        (client.messages[1],), chat=client.entity
                    )
                    await service.on_message(entry)
                    before = {k: dict(v) for k, v in exchange.orders.items()}
                    sent_before_recovery = sender.await_count
                    await reader(monkeypatch, client, store, service.on_message).start()
                    assert exchange.orders == before
                with closing(sqlite3.connect(store._database_path)) as db:
                    mode, payload, status, error = db.execute(
                        "SELECT approval_mode,payload_json,status,error FROM position_actions"
                    ).fetchone()
                    assert (
                        mode == "MANUAL"
                        and json.loads(payload)["source"]["recovery_review_reason"]
                    )
                    assert status == "SKIPPED" and "expired" in error.lower()
                recovered = sender.call_args_list[sent_before_recovery:]
                assert len(recovered) == 1
                assert "EXPIRED LIFECYCLE" in recovered[0].kwargs["text"]
                assert not recovered[0].kwargs.get("reply_markup")
                assert "BYBIT DEMO — ACCOUNT" not in recovered[0].kwargs["text"]
                assert await store.get_source_history_cursor(CHANNEL) == 3
            finally:
                await bot.close()

    asyncio.run(run())


def test_durable_cursor_recovers_newly_visible_post_older_than_bootstrap(
    tmp_path, monkeypatch
):
    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        client = FakeTelegramClient([message(1, hours=40)])
        seen = []

        async def receive(post):
            seen.append(post.source)
            token = await store.claim_source(post.source, lease_seconds=300)
            await store.mark_source_completed(
                post.source, token, reason="Fixture commentary"
            )

        await reader(monkeypatch, client, store, receive).start()
        client.messages.extend([message(2, hours=30), message(3)])
        await reader(monkeypatch, client, store, receive).start()
        assert [s.message_id for s in seen] == [2, 3]
        assert seen[0].recovery_review_reason and seen[1].recovery_review_reason is None
        assert await store.get_source_history_cursor(CHANNEL) == 3

    asyncio.run(run())


def test_schema15_upgrade_preserves_source_and_trade_state(tmp_path):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        from test_zone_ledger_integration import zones

        await zones(path)
        with closing(sqlite3.connect(path)) as db:
            tables = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='source_history_cursors'"
                )
            ]
            before = {t: db.execute("SELECT * FROM " + t).fetchall() for t in tables}
            db.execute("DROP TABLE source_history_cursors")
            db.execute("PRAGMA user_version=15")
            db.commit()
        await IntentStore(path).initialize()
        await IntentStore(path).initialize()
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (16,)
            assert {
                t: db.execute("SELECT * FROM " + t).fetchall() for t in tables
            } == before
            assert db.execute(
                "SELECT count(*) FROM source_history_cursors"
            ).fetchone() == (0,)
            assert not db.execute("PRAGMA foreign_key_check").fetchall()

    asyncio.run(run())


def test_seed_write_failure_does_not_silently_create_memory_only_progress(
    tmp_path, monkeypatch
):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        client = FakeTelegramClient([message(1, hours=26), message(2)])
        seen = []

        async def receive(post):
            seen.append(post.source.message_id)
            token = await store.claim_source(post.source, lease_seconds=300)
            await store.mark_source_completed(
                post.source, token, reason="Fixture commentary"
            )

        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TRIGGER fail_seed BEFORE INSERT ON source_history_cursors BEGIN SELECT RAISE(ABORT,'synthetic seed failure'); END"
            )
            db.commit()
        source = reader(monkeypatch, client, store, receive)
        await source.start()
        assert (
            await store.get_source_history_cursor(CHANNEL) is None
            and not seen
            and source.read_warnings()
        )
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP TRIGGER fail_seed")
            db.commit()
        await source.poll_once()
        assert await store.get_source_history_cursor(CHANNEL) == 2 and seen == [2]

    asyncio.run(run())


def test_failed_channel_does_not_block_other_durable_source(tmp_path, monkeypatch):
    from types import SimpleNamespace

    other = CHANNEL - 1

    class TwoChannels(FakeTelegramClient):
        async def get_entity(self, channel):
            return SimpleNamespace(
                id=abs(channel) - 1000000000000, title="Fixture source", username=None
            )

        def iter_messages(self, entity, *, min_id=0, reverse=False, limit=None):
            async def items():
                channel = -1000000000000 - entity.id
                values = sorted(
                    [
                        m
                        for m in self.messages
                        if m.chat_id == channel and m.id > min_id
                    ],
                    key=lambda m: m.id,
                    reverse=not reverse,
                )
                for m in values[:limit]:
                    yield m

            return items()

    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        rows = [message(1, hours=26), message(2), message(1, hours=26), message(2)]
        for m in rows[2:]:
            m.chat_id = other
        client = TwoChannels(rows)
        fail = True
        seen = []

        async def receive(post):
            nonlocal fail
            if post.source.channel_id == CHANNEL and fail:
                fail = False
                raise RuntimeError("synthetic first-channel failure")
            token = await store.claim_source(post.source, lease_seconds=300)
            await store.mark_source_completed(
                post.source, token, reason="Fixture commentary"
            )
            seen.append(post.source.channel_id)

        await reader(
            monkeypatch, client, store, receive, channels=(CHANNEL, other)
        ).start()
        assert await store.get_source_history_cursor(CHANNEL) == 1
        assert await store.get_source_history_cursor(other) == 2
        await reader(
            monkeypatch, client, store, receive, channels=(CHANNEL, other)
        ).start()
        assert await store.get_source_history_cursor(CHANNEL) == 2
        assert seen == [other, CHANNEL]

    asyncio.run(run())


def test_page_boundary_album_is_durable_without_duplicate_delivery(
    tmp_path, monkeypatch
):
    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        rows = [message(1, hours=26)] + [message(i) for i in range(2, 201)]
        rows.extend(
            [
                message(201, image=b"first", group=99),
                message(202, image=b"second", group=99),
                message(203),
            ]
        )
        client = FakeTelegramClient(rows)
        seen = []

        async def receive(post):
            seen.append((post.source.message_id, len(post.images)))
            token = await store.claim_source(post.source, lease_seconds=300)
            await store.mark_source_completed(
                post.source, token, reason="Fixture commentary"
            )

        await reader(monkeypatch, client, store, receive).start()
        assert await store.get_source_history_cursor(CHANNEL) == 202
        assert len(seen) == 200 and seen[-1] == (201, 2)
        await reader(monkeypatch, client, store, receive).start()
        assert await store.get_source_history_cursor(CHANNEL) == 203
        assert len(seen) == 201 and seen[-1] == (203, 0)
        await reader(monkeypatch, client, store, receive).start()
        assert len(seen) == 201

    asyncio.run(run())
