"""Offline subprocess application: actual TaskGroup, signal handling and bot polling."""

import asyncio
import json
import os
import signal
import sys
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import F
from aiogram.types import Chat, Message, Update, User

from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.cancellation import finish_before_cancellation
from cautious_crypto_bro.supervision_health import (
    publish_success,
    supervision_health_path,
)

app = import_module("cautious_crypto_bro.main")
events = []
phase, directory = sys.argv[1:]
database = Path(directory) / "state.db"


async def forever():
    try:
        await asyncio.Event().wait()
    finally:
        events.append("background.stopped")


def send_signals():
    loop = asyncio.get_running_loop()
    loop.call_later(0.02, os.kill, os.getpid(), signal.SIGTERM)
    loop.call_later(0.06, os.kill, os.getpid(), signal.SIGTERM)


async def owned(settings):
    publish_success(supervision_health_path(database))
    bot = ApprovalBot(
        token="123456:synthetic-offline-token",
        approval_chat_id=1,
        approver_user_id=1,
        store=object(),
        executor=object(),
    )
    bot.send_recovery_warning = AsyncMock()
    bot.run_strategy_alert_delivery = forever
    bot._bot.me = AsyncMock(
        return_value=User(id=123456, is_bot=True, first_name="Offline")
    )

    async def start():
        if phase == "startup":
            send_signals()
            await forever()

    bot.start = start

    async def hold(message):
        loop = asyncio.get_running_loop()

        def worker():
            events.append("worker.started")
            loop.call_soon_threadsafe(send_signals)
            time.sleep(0.2)
            events.append("worker.finished")

        async def execute():
            await asyncio.to_thread(worker)
            events.append("outcome.persisted")

        try:
            await finish_before_cancellation(execute())
        finally:
            events.append("handler.finished")

    bot._router.message(F.text == "hold")(hold)

    async def updates(*args, **kwargs):
        yield Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=1, type="private"),
                from_user=User(id=1, is_bot=False, first_name="Offline"),
                text="hold",
            ),
        )
        await forever()

    bot._dispatcher._listen_updates = updates

    async def close():
        if phase == "polling":
            assert "handler.finished" in events, "Telegram client closed before handler"
            assert "outcome.persisted" in events, "Outcome abandoned during shutdown"
        events.append("client.closed")

    bot._bot.session.close = close

    def cleanup():
        assert supervision_health_path(database).exists()
        try:
            with app.state_ownership(database):
                raise AssertionError("State owner released before cleanup")
        except RuntimeError as exc:
            assert "already owned" in str(exc)
        events.append("cleanup.complete")

    runtime = SimpleNamespace(
        bot=bot,
        supervisor=SimpleNamespace(reconcile_once=AsyncMock(), run=forever),
        service=SimpleNamespace(
            recover_auto_execution=AsyncMock(),
            run_delivery_recovery=forever,
            run_periodic_account_pnl_sync=forever,
        ),
        source=SimpleNamespace(
            start=AsyncMock(), run_until_disconnected=forever, run_catchup=forever
        ),
    )
    async with AsyncExitStack() as stack:
        stack.callback(cleanup)
        stack.push_async_callback(bot.close)
        await app._run_application(
            runtime, interrupted_intents=(), interrupted_actions=()
        )


app.get_settings = lambda: SimpleNamespace(log_level="INFO", database_path=database)
app._run_owned_application = owned
# Bound application startup and shutdown, independently of cold dependency imports.
signal.alarm(5)
app.main()
assert not supervision_health_path(database).exists()
with app.state_ownership(database):
    events.append("owner.released")
print(json.dumps(events), flush=True)
