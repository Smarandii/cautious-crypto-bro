"""Real dispatcher + SQLite; fake only Telegram transport and forbid execution."""

import asyncio
import html
import json
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from aiogram.types import Chat, Message, Update, User
from test_manual_lifecycle_smoke import TelegramTransport

from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class ForbiddenExecutor:
    def __getattr__(self, name):
        raise AssertionError(f"Read-only command reached executor: {name}")


def bot_for(store):
    return ApprovalBot(
        token="123456:synthetic-integration-token",
        approval_chat_id=-100123,
        approver_user_id=42,
        store=store,
        executor=ForbiddenExecutor(),
    )


async def dispatch(bot, text, *, user=42, chat=-100123):
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat, type="supergroup"),
        from_user=User(id=user, is_bot=False, first_name="Operator")
        if user is not None
        else None,
        text=text,
    )
    sender = AsyncMock()
    identity = User(id=123456, is_bot=True, first_name="Review", username="ReviewBot")
    with (
        patch.object(bot._bot, "send_message", new=sender),
        patch.object(bot._bot, "me", new=AsyncMock(return_value=identity)),
    ):
        await bot._dispatcher.feed_update(
            bot._bot, Update(update_id=1, message=message)
        )
    if sender.await_count:
        assert sender.await_count == 1
        assert sender.call_args.kwargs["chat_id"] == -100123
        assert sender.call_args.kwargs["parse_mode"] == "HTML"
        assert sender.call_args.kwargs["reply_markup"] is None
        return sender.call_args.kwargs["text"]
    return None


def seed(database, count=1):
    """Seed historical rows, including legacy strategies with no reason or plan."""
    with sqlite3.connect(database) as db:
        for number in range(1, count + 1):
            record_id = str(UUID(int=number))
            timestamp = "2026-10-18T00:00:00+00:00"
            kind = number % 4
            if kind in (0, 1):
                db.execute(
                    "INSERT INTO position_strategies "
                    "(strategy_id, symbol, side, status, pause_reason, pause_notified, created_at, updated_at) "
                    "VALUES (?, ?, 'LONG', ?, ?, 1, ?, ?)",
                    (
                        record_id,
                        "BTCUSDT",
                        "MANUAL_OVERRIDE" if kind == 1 else "UNCERTAIN",
                        "Changed <exit> & price" if kind == 1 else None,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                table, column = (
                    ("intents", "intent_id")
                    if kind == 2
                    else ("position_actions", "action_id")
                )
                db.execute(
                    f"INSERT INTO {table} "
                    f"({column}, channel_id, message_id, payload_json, status, error, created_at, updated_at) "
                    "VALUES (?, 1, ?, ?, 'UNCERTAIN', ?, ?, ?)",
                    (
                        record_id,
                        number,
                        json.dumps({"symbol": "ETHUSDT"}),
                        "Accepted request; response lost",
                        timestamp,
                        timestamp,
                    ),
                )
        # Healthy/terminal records must not be presented as quarantined.
        db.execute(
            "INSERT INTO position_strategies "
            "(strategy_id, symbol, side, status, created_at, updated_at) "
            "VALUES (?, 'HEALTHYUSDT', 'LONG', 'CLOSED', ?, ?)",
            (str(UUID(int=10000)), timestamp, timestamp),
        )


def snapshot(database):
    with sqlite3.connect(database) as db:
        return tuple(db.iterdump())


@pytest.mark.parametrize("state", ["empty", "quarantined", "unreadable"])
def test_status_reports_durable_review_state_without_mutation(tmp_path, state):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        if state == "quarantined":
            seed(database, 4)
        before = snapshot(database)
        if state == "unreadable":
            store = IntentStore(tmp_path / "missing" / "state.db")
        bot = bot_for(store)
        transport = TelegramTransport()
        bot._bot.session = transport
        bot.set_startup_blocked("Exchange startup reconciliation unavailable.")
        try:
            for user, chat in [
                (43, -100123),
                (42, -100999),
                (None, -100123),
                (42, 42),
                (42, -100123),
            ]:
                authorized = user == 42 and chat in {42, -100123}
                count = len(transport.messages)
                await bot._dispatcher.feed_update(
                    bot._bot,
                    Update(
                        update_id=count + 1,
                        message=Message(
                            message_id=1,
                            date=datetime.now(UTC),
                            chat=Chat(
                                id=chat, type="private" if chat > 0 else "supergroup"
                            ),
                            from_user=User(id=user, is_bot=False, first_name="Operator")
                            if user is not None
                            else None,
                            text="/status",
                        ),
                    ),
                )
                if not authorized:
                    assert len(transport.messages) == count
                    continue
                response = list(transport.messages.values())[-1]
                assert response.chat.id == chat
                assert "Exchange startup reconciliation unavailable" in response.text
                if state == "quarantined":
                    assert "HUMAN REVIEW REQUIRED" in response.text
                    assert "/attention in the approval chat" in response.text
                    assert "Changed <exit>" not in response.text
                elif state == "empty":
                    assert (
                        "No paused or uncertain records in local storage"
                        in response.text
                    )
                else:
                    assert "local storage could not be read" in response.text
                    assert "No paused or uncertain records" not in response.text
                if state != "unreadable":
                    assert "not a live exchange protection check" in response.text
            assert snapshot(database) == before
        finally:
            await bot.close()

    asyncio.run(run())


def test_attention_paginates_all_quarantine_types_without_mutation(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        seed(database, 106)
        before = snapshot(database)
        # Reopen durable state as after restart; acknowledged pauses stay visible.
        bot = bot_for(IntentStore(database))
        try:
            seen = set()
            pages = []
            for page in range(1, 23):
                text = await dispatch(bot, f"/attention {page}")
                pages.append(text)
                assert "HEALTHYUSDT" not in text
                assert "not a live exchange check" in text
                assert "does not resume automation or acknowledge alerts" in text
                identifiers = {
                    line.removeprefix("<code>").removesuffix("</code>")
                    for line in text.splitlines()
                    if line.startswith("<code>")
                }
                assert 1 <= len(identifiers) <= 5
                assert not seen.intersection(identifiers)
                seen.update(identifiers)
                assert (f"Next: /attention {page + 1}" in text) == (page < 22)
                assert len(text) < 4096
            combined = "\n".join(pages)
            assert "Changed &lt;exit&gt; &amp; price" in combined
            assert "— strategy" in combined and "MANUAL_OVERRIDE" in combined
            assert "— entry" in combined and "— action" in combined
            assert "Exchange outcome unknown" in combined
            assert seen == {str(UUID(int=n)) for n in range(1, 107)}
            assert "No records on this page" in await dispatch(bot, "/attention 23")
            assert snapshot(database) == before
        finally:
            await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("user,chat", [(43, -100123), (42, -100999), (None, -100123)])
def test_attention_does_not_disclose_to_other_users_or_chats(tmp_path, user, chat):
    async def run():
        # No database exists: unauthorized requests must not even access storage.
        bot = bot_for(IntentStore(tmp_path / "missing" / "state.db"))
        try:
            assert await dispatch(bot, "/attention", user=user, chat=chat) is None
        finally:
            await bot.close()

    asyncio.run(run())


def test_attention_distinguishes_empty_state_from_storage_failure(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        bot = bot_for(store)
        try:
            assert "No paused or uncertain records" in await dispatch(bot, "/attention")
            assert "No paused or uncertain records" in await dispatch(
                bot, "/attention@ReviewBot"
            )
            assert await dispatch(bot, "/attention@OtherBot") is None
            with sqlite3.connect(database) as db:
                db.execute("DROP TABLE position_strategies")
            text = await dispatch(bot, "/attention")
            assert "unavailable" in text
            assert "No paused or uncertain records" not in text
        finally:
            await bot.close()

    asyncio.run(run())


def test_attention_rejects_invalid_pages_before_reading_storage(tmp_path):
    async def run():
        bot = bot_for(IntentStore(tmp_path / "missing" / "state.db"))
        try:
            for argument in ("0", "-1", "1.5", "1 2", "١", "1" * 1000):
                assert "Usage:" in await dispatch(bot, f"/attention {argument}")
        finally:
            await bot.close()

    asyncio.run(run())


def test_attention_bounds_long_reasons_and_keeps_legacy_pauses_visible(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        seed(database, 5)
        with sqlite3.connect(database) as db:
            db.execute("UPDATE position_strategies SET pause_reason = ?", ("<" * 5000,))
            db.execute("UPDATE intents SET error = ?", ("&" * 5000,))
            db.execute("UPDATE position_actions SET error = ?", ("😀" * 5000,))
            db.execute(
                "UPDATE position_strategies SET pause_reason = NULL WHERE strategy_id = ?",
                (str(UUID(int=1)),),
            )
        before = snapshot(database)
        bot = bot_for(store)
        try:
            text = await dispatch(bot, "/attention")
            assert "No pause reason recorded (legacy state)" in text
            assert "(truncated)" in text
            assert "&lt;" in text
            # Telegram limits text after entity parsing, measured as UTF-16 units.
            assert len(html.unescape(text).encode("utf-16-le")) // 2 < 4096
            assert snapshot(database) == before
        finally:
            await bot.close()

    asyncio.run(run())


def test_attention_shows_entry_failure_reason_for_uncertain_strategy(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        seed(database, 4)
        with sqlite3.connect(database) as db:
            db.execute(
                "INSERT INTO intents (intent_id, channel_id, message_id, payload_json, "
                "status, error, created_at, updated_at) VALUES (?, 1, 50, ?, 'FAILED', ?, ?, ?)",
                (
                    str(UUID(int=4)),
                    json.dumps({"symbol": "BTCUSDT"}),
                    "Exchange failed after submitting entry",
                    "2026-10-18",
                    "2026-10-18",
                ),
            )
        bot = bot_for(store)
        try:
            assert "Exchange failed after submitting entry" in await dispatch(
                bot, "/attention"
            )
        finally:
            await bot.close()

    asyncio.run(run())
