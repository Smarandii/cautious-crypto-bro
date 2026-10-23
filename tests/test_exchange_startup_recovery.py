"""Real SQLite, exchange adapter, bot polling/router and startup orchestration."""

import asyncio
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteWebhook,
    EditMessageText,
    GetMe,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from test_operator_public_ip import install_ip_transport
from test_remaining_concerns import executor_for, prepare
from test_workflow_integration import ExchangeTransport, ModelTransport, workflow

from cautious_crypto_bro.approval_bot import IntentAction, PositionActionCallback
from cautious_crypto_bro.domain import ApprovalMode, IntentStatus, TradeExecutionError

app = import_module("cautious_crypto_bro.main")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failed_gate", [1, 2])
@pytest.mark.parametrize("cancel_outage", [False, True])
def test_exchange_rejection_keeps_status_but_blocks_trading(
    monkeypatch, tmp_path, failed_gate, cancel_outage
):
    ip_requests = install_ip_transport(monkeypatch)

    async def run():
        class UnavailableExchange(ExchangeTransport):
            armed = False
            recovered = False
            account_reads = 0

            def handle(self, request):
                if self.armed and request.url.path == "/v5/position/list":
                    self.account_reads += 1
                    if self.account_reads >= failed_gate and not self.recovered:
                        return httpx.Response(
                            200,
                            json={
                                "retCode": 10010,
                                "retMsg": "Unmatched IP synthetic-secret-must-not-leak",
                            },
                        )
                return super().handle(request)

        exchange = UnavailableExchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
            await supervisor.reconcile_once()
            service, bot, supervisor = workflow(store, executor, ModelTransport())
            recovery_calls = 0
            recover = service.recover_auto_execution

            async def recover_once():
                nonlocal recovery_calls
                recovery_calls += 1
                await recover()

            service.recover_auto_execution = recover_once
            notices = asyncio.Queue()
            updates = asyncio.Queue()
            replies = asyncio.Queue()
            callbacks = asyncio.Queue()
            source_started = asyncio.Event()
            notice_attempts = 0

            async def send(chat_id, text, **kwargs):
                nonlocal notice_attempts
                if (
                    "Exchange startup reconciliation unavailable" in text
                    and "Operator" in text
                ):
                    notice_attempts += 1
                    if notice_attempts == 1:
                        raise OSError("Synthetic Telegram send failure")
                    notices.put_nowait(text)
                elif "recovered" in text:
                    notices.put_nowait(text)
                else:
                    replies.put_nowait(text)
                return SimpleNamespace(message_id=1)

            class TelegramTransport(BaseSession):
                async def close(self):
                    pass

                async def make_request(self, bot, method, timeout=None):
                    if isinstance(method, DeleteWebhook):
                        return True
                    if isinstance(method, GetMe):
                        return User(id=123456, is_bot=True, first_name="Offline")
                    if isinstance(method, AnswerCallbackQuery):
                        callbacks.put_nowait({"text": method.text})
                        return True
                    if isinstance(method, SendMessage):
                        await send(method.chat_id, method.text)
                    elif not isinstance(method, EditMessageText):
                        raise AssertionError(type(method).__name__)
                    return Message(
                        message_id=1,
                        date=datetime.now(UTC),
                        chat=Chat(id=method.chat_id, type="private"),
                        text=method.text,
                    )

                async def stream_content(self, *args, **kwargs):
                    raise AssertionError("Unexpected download")
                    yield b""

            async def listen(*args, **kwargs):
                while True:
                    yield await updates.get()

            async def source_run():
                source_started.set()
                await asyncio.Event().wait()

            await bot._bot.session.close()
            bot._bot.session = TelegramTransport()
            monkeypatch.setattr(bot._dispatcher, "_listen_updates", listen)
            original = app._reconcile_before_ingestion

            async def reconcile(runtime, **kwargs):
                return await original(
                    runtime, retry_seconds=0.02, alert_seconds=900, **kwargs
                )

            monkeypatch.setattr(app, "_reconcile_before_ingestion", reconcile)
            runtime = SimpleNamespace(
                bot=bot,
                service=service,
                supervisor=supervisor,
                source_recovery=SimpleNamespace(run=source_run),
            )
            exchange.armed = True
            task = asyncio.create_task(
                app._run_application(
                    runtime, interrupted_intents=(), interrupted_actions=()
                )
            )
            try:
                async with asyncio.timeout(5):
                    notice = await notices.get()
                    assert "synthetic-secret" not in notice
                    assert "10010 (Unmatched IP)" in notice
                    assert "https://www.bybit.com/en/app/user/api-management" in notice
                    assert notice_attempts == 2
                    assert len(ip_requests) == notice_attempts
                    assert not task.done() and not source_started.is_set()
                    assert recovery_calls == failed_gate - 1
                    baseline = list(exchange.requests)
                    message = Message(
                        message_id=1,
                        date=datetime.now(UTC),
                        chat=Chat(id=1, type="private"),
                        from_user=User(id=1, is_bot=False, first_name="Operator"),
                        text="/status",
                    )
                    updates.put_nowait(Update(update_id=1, message=message))
                    status = await replies.get()
                    assert "protection is not verified" in status
                    assert "10010 (Unmatched IP)" in status
                    assert "runtime's current public IP" in status
                    assert "https://www.bybit.com/en/app/user/api-management" in status
                    for index, data in enumerate(
                        (
                            IntentAction(
                                action="execute", intent_id=str(intent.intent_id)
                            ).pack(),
                            PositionActionCallback(
                                action="execute", action_id=str(intent.intent_id)
                            ).pack(),
                            IntentAction(
                                action="skip", intent_id=str(intent.intent_id)
                            ).pack(),
                            PositionActionCallback(
                                action="skip", action_id=str(intent.intent_id)
                            ).pack(),
                        ),
                        start=2,
                    ):
                        updates.put_nowait(
                            Update(
                                update_id=index,
                                callback_query=CallbackQuery(
                                    id=str(index),
                                    from_user=message.from_user,
                                    chat_instance="test",
                                    message=message,
                                    data=data,
                                ),
                            )
                        )
                        assert "incomplete" in (await callbacks.get())["text"]
                    assert exchange.requests == baseline
                    assert notices.empty() and not source_started.is_set()
                    # Unauthorized users receive no local operational status.
                    await bot._dispatcher.feed_update(
                        bot._bot,
                        Update(
                            update_id=8,
                            message=message.model_copy(
                                update={
                                    "from_user": User(
                                        id=2, is_bot=False, first_name="Other"
                                    )
                                }
                            ),
                        ),
                    )
                    assert replies.empty()
                    if not cancel_outage:
                        exchange.recovered = True
                        await source_started.wait()
                        assert "recovered" in await notices.get()
                        assert bot._startup_blocked is None
                        assert recovery_calls == 1
                        updates.put_nowait(Update(update_id=9, message=message))
                        assert "complete" in await replies.get()
                        updates.put_nowait(
                            Update(
                                update_id=10,
                                callback_query=CallbackQuery(
                                    id="10",
                                    from_user=message.from_user,
                                    chat_instance="test",
                                    message=message,
                                    data=IntentAction(
                                        action="execute",
                                        intent_id=str(intent.intent_id),
                                    ).pack(),
                                ),
                            )
                        )
                        assert "incomplete" not in (await callbacks.get()).get(
                            "text", ""
                        )
                        assert len(
                            [r for r in exchange.requests if r[0] == "/v5/order/create"]
                        ) == len([r for r in baseline if r[0] == "/v5/order/create"])
            finally:
                task.cancel()
                result = await asyncio.gather(task, return_exceptions=True)
                await bot.close()
                assert isinstance(result[0], asyncio.CancelledError), result
            assert notice_attempts == 2

    asyncio.run(run())


def test_startup_retry_backoff_is_bounded_and_alerts_are_rate_limited(monkeypatch):
    async def run():
        delays = []
        clock = 0.0
        notices = []

        async def sleep(delay):
            nonlocal clock
            delays.append(delay)
            clock += delay
            if len(delays) == 9:
                raise asyncio.CancelledError

        async def notice(text):
            notices.append(text)

        monkeypatch.setattr(app.asyncio, "sleep", sleep)
        monkeypatch.setattr(app, "monotonic", lambda: clock)
        runtime = SimpleNamespace(
            supervisor=SimpleNamespace(
                reconcile_once=AsyncMock(
                    side_effect=TradeExecutionError("private payload")
                )
            ),
            bot=SimpleNamespace(
                set_startup_blocked=lambda _: None, send_source_notice=notice
            ),
        )
        with pytest.raises(asyncio.CancelledError):
            await app._reconcile_before_ingestion(runtime)
        assert delays == [30, 60, 120, 240, 300, 300, 300, 300, 300]
        assert len(notices) == 2 and all(
            "private payload" not in text for text in notices
        )

        # Both startup reconciliation gates share the same notification window.
        clock = 0.0
        delays.clear()
        notices.clear()
        runtime.supervisor.reconcile_once = AsyncMock(
            side_effect=[
                TradeExecutionError("offline"),
                None,
                TradeExecutionError("offline"),
                None,
            ]
        )
        _, next_alert = await app._reconcile_before_ingestion(runtime)
        await app._reconcile_before_ingestion(runtime, next_alert=next_alert)
        assert delays == [30, 30] and len(notices) == 1

    asyncio.run(run())
