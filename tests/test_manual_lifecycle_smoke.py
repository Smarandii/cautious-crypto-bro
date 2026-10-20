"""Offline signal-to-close smoke flow; only external transports are replaced."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from test_remaining_concerns import executor_for
from test_telegram_source import FakeMessage, FakeTelegramClient
from test_workflow_integration import ExchangeTransport, ModelTransport, workflow

from cautious_crypto_bro import telegram_source
from cautious_crypto_bro.domain import AutoApprovalMode, IntentStatus
from cautious_crypto_bro.storage import IntentStore

pytestmark = [pytest.mark.integration, pytest.mark.smoke]
CHANNEL = -1001234567890


class TelegramTransport(BaseSession):
    def __init__(self):
        super().__init__()
        self.messages = {}
        self.answers = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Unexpected Telegram download")
        yield b""

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Smoke", username="SmokeBot")
        if isinstance(method, SendMessage):
            message = Message(
                message_id=len(self.messages) + 1,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=User(id=bot.id, is_bot=True, first_name="Smoke"),
                text=method.text,
                reply_markup=method.reply_markup,
            )
            self.messages[message.message_id] = message
            return message
        if isinstance(method, EditMessageText):
            message = self.messages[method.message_id].model_copy(
                update={"text": method.text, "reply_markup": method.reply_markup}
            )
            self.messages[message.message_id] = message
            return message
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method.text)
            return True
        raise AssertionError(f"Unexpected Telegram method: {type(method).__name__}")

    def card(self):
        return next(
            message
            for message in reversed(self.messages.values())
            if message.reply_markup is not None
        )


async def click(bot, card, *, user=1):
    await bot._dispatcher.feed_update(
        bot._bot,
        Update(
            update_id=card.message_id,
            callback_query=CallbackQuery(
                id=f"click-{card.message_id}-{user}",
                from_user=User(id=user, is_bot=False, first_name="Operator"),
                chat_instance="offline-smoke",
                message=card,
                data=card.reply_markup.inline_keyboard[0][0].callback_data,
            ),
        ),
    )


def mutation_count(exchange):
    return sum(
        path
        in {
            "/v5/order/create",
            "/v5/order/create-batch",
            "/v5/order/cancel",
            "/v5/position/trading-stop",
            "/v5/position/set-leverage",
        }
        for path, _ in exchange.requests
    )


async def start(database, executor, provider, transport):
    store = IntentStore(database)
    await store.initialize()
    service, bot, supervisor = workflow(
        store, executor, provider, mode=AutoApprovalMode.DISABLED
    )
    await bot._bot.session.close()
    bot._bot.session = transport
    assert await store.quarantine_interrupted_executions() == ((), ())
    await supervisor.reconcile_once()
    await service.recover_auto_execution()
    source = telegram_source.TelegramSource(
        api_id=1,
        api_hash="synthetic",
        session_name="synthetic",
        channels=[CHANNEL],
        on_message=service.on_message,
    )
    await source.start()
    return store, bot, supervisor, source, transport


def test_manual_signal_to_protection_and_close_survives_restart(tmp_path, monkeypatch):
    async def run():
        database = tmp_path / "state.db"
        client = FakeTelegramClient([])
        monkeypatch.setattr(telegram_source, "TelegramClient", lambda *a, **kw: client)
        provider = ModelTransport()
        provider.fail_image_once = False
        exchange = ExchangeTransport()
        transport = TelegramTransport()
        with executor_for(exchange) as executor:
            store, bot, supervisor, source, transport = await start(
                database, executor, provider, transport
            )
            try:
                client.messages.append(
                    FakeMessage(
                        message_id=1,
                        published_at=datetime.now(UTC),
                        text="Enter BTCUSDT LONG at market, stop 90",
                        image=b"synthetic chart",
                    )
                )
                await source.poll_once()
                intent = (await store.get_recent_source_intents(CHANNEL))[0]
                assert intent.status is IntentStatus.PENDING
                assert exchange.size == 0 and not exchange.orders
                entry_card = transport.card()
                await click(bot, entry_card, user=2)
                assert transport.answers[-1] == "Not authorized"
                assert (
                    await store.get_intent(intent.intent_id)
                ).status is IntentStatus.PENDING
                assert not exchange.orders

                await click(bot, entry_card)
                assert (
                    await store.get_intent(intent.intent_id)
                ).status is IntentStatus.EXECUTED
                assert (
                    "EXECUTED ON BYBIT DEMO"
                    in transport.messages[entry_card.message_id].text
                )
                await supervisor.reconcile_once()
                assert exchange.size > 0 and exchange.stop > 0
                assert len(exchange.exits()) == 3
                orders_before_restart = set(exchange.orders)
            finally:
                await source.disconnect()
                await bot.close()

            store, bot, supervisor, source, transport = await start(
                database, executor, provider, transport
            )
            try:
                before_duplicate = mutation_count(exchange)
                await click(bot, entry_card)
                await source.poll_once()
                assert set(exchange.orders) == orders_before_restart
                assert mutation_count(exchange) == before_duplicate
                assert not await store.pending_manual_deliveries()
                client.messages.append(
                    FakeMessage(
                        message_id=2,
                        published_at=datetime.now(UTC),
                        text="Close BTCUSDT now",
                    )
                )
                await source.poll_once()
                assert exchange.size > 0, "Manual close must wait for approval"
                close_card = transport.card()
                await click(bot, close_card, user=2)
                assert transport.answers[-1] == "Not authorized" and exchange.size > 0
                await click(bot, close_card)
                assert exchange.size == 0
                await supervisor.reconcile_once()
                assert not await store.get_active_position_strategies()
                assert len(await store.get_recent_executed_closes()) == 1
                final_orders = set(exchange.orders)
                before_duplicate = mutation_count(exchange)
                await click(bot, close_card)
                await source.poll_once()
                assert set(exchange.orders) == final_orders
                assert mutation_count(exchange) == before_duplicate
                assert not exchange.open_entries() and not exchange.exits()
                assert len(await store.get_recent_source_intents(CHANNEL)) == 1
                assert provider.image_requests == 1
                reopened = IntentStore(database)
                assert (
                    await reopened.get_intent(intent.intent_id)
                ).status is IntentStatus.EXECUTED
                with closing(sqlite3.connect(database)) as db:
                    assert db.execute(
                        "SELECT status FROM position_strategies"
                    ).fetchall() == [("CLOSED",)]
                    assert db.execute(
                        "SELECT status FROM source_messages ORDER BY message_id"
                    ).fetchall() == [("COMPLETED",), ("COMPLETED",)]
            finally:
                await source.disconnect()
                await bot.close()

    asyncio.run(run())
