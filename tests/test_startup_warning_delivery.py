"""Critical startup notices must not block polling or disappear after a send failure."""

import asyncio
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, Update, User
from test_remaining_concerns import executor_for, prepare, source, state_for
from test_workflow_integration import ExchangeTransport, ModelTransport, workflow

from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
    StrategyStatus,
)

app = import_module("cautious_crypto_bro.main")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("delivery", ["retry", "slow", "cancel"])
def test_interrupted_execution_warning_is_retried_without_blocking_startup(
    monkeypatch, tmp_path, delivery
):
    async def run():
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            database = tmp_path / "state.db"
            store, intent, _, coordinator, _ = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            action = PositionActionIntent(
                source=source(message_id=2),
                symbol="BTCUSDT",
                action=PositionActionType.CLOSE,
                expected_side=Side.LONG,
                summary="Lifecycle action",
                confidence=1,
            )
            claim = await store.claim_source(action.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source((), (action,), claim)
            await store.claim_position_action_for_execution(
                action.action_id, 1, expected_approval_mode=ApprovalMode.MANUAL
            )
            # An interrupted lifecycle action must quarantine its owned strategy.
            interrupted, actions = await store.quarantine_interrupted_executions()
            assert not interrupted and actions == (action.action_id,)
            service, bot, supervisor = workflow(store, executor, ModelTransport())
            updates, replies = asyncio.Queue(), asyncio.Queue()
            first_send, release, delivered = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )
            polling, source_started = asyncio.Event(), asyncio.Event()
            attempts = successful = reconciliations = 0
            sender = None

            async def send(chat_id, text, **kwargs):
                nonlocal attempts, successful, sender
                if "INTERRUPTED EXECUTION" in text:
                    sender = asyncio.current_task()
                    attempts += 1
                    first_send.set()
                    if delivery == "retry" and attempts == 1:
                        raise OSError("Synthetic Telegram failure")
                    if delivery != "retry":
                        await release.wait()
                    successful += 1
                    delivered.set()
                else:
                    replies.put_nowait(text)
                return SimpleNamespace(message_id=1)

            async def listen(*args, **kwargs):
                polling.set()
                while True:
                    yield await updates.get()

            async def source_run():
                source_started.set()
                await asyncio.Event().wait()

            reconcile = supervisor.reconcile_once

            async def checked_reconcile():
                nonlocal reconciliations
                reconciliations += 1
                await reconcile()

            supervisor.reconcile_once = checked_reconcile
            monkeypatch.setattr(bot._bot, "delete_webhook", AsyncMock())
            monkeypatch.setattr(
                bot._bot,
                "get_me",
                AsyncMock(
                    return_value=User(
                        id=123456,
                        is_bot=True,
                        first_name="Offline",
                    )
                ),
            )
            monkeypatch.setattr(bot._bot, "send_message", send)
            monkeypatch.setattr(bot._dispatcher, "_listen_updates", listen)
            warning = getattr(app, "_deliver_recovery_warning", None)
            if warning is not None:

                async def quick_warning(runtime, **kwargs):
                    await warning(runtime, retry_seconds=0.01, **kwargs)

                monkeypatch.setattr(app, "_deliver_recovery_warning", quick_warning)
            runtime = SimpleNamespace(
                store=store,
                bot=bot,
                service=service,
                supervisor=supervisor,
                source_recovery=SimpleNamespace(run=source_run),
            )
            before = len(exchange.requests)
            task = asyncio.create_task(app._run_application(runtime))
            try:
                async with asyncio.timeout(3):
                    await first_send.wait()
                    await polling.wait()
                    updates.put_nowait(
                        Update(
                            update_id=1,
                            message=Message(
                                message_id=1,
                                date=datetime.now(UTC),
                                chat=Chat(id=1, type="private"),
                                from_user=User(
                                    id=1, is_bot=False, first_name="Operator"
                                ),
                                text="/status",
                            ),
                        )
                    )
                    assert "HUMAN REVIEW REQUIRED" in await replies.get()
                    await source_started.wait()
                    assert reconciliations >= 2 and bot._startup_blocked is None
                    assert (await state_for(store)).status is StrategyStatus.UNCERTAIN
                    assert (
                        await store.get_position_action(action.action_id)
                    ).status is IntentStatus.UNCERTAIN
                    if delivery != "cancel":
                        release.set()
                        await delivered.wait()
                        await asyncio.sleep(0.04)
                        assert successful == 1
                        assert attempts == (2 if delivery == "retry" else 1)
                    assert not any(
                        path
                        in {
                            "/v5/order/create",
                            "/v5/order/create-batch",
                            "/v5/order/cancel",
                            "/v5/position/trading-stop",
                        }
                        for path, _ in exchange.requests[before:]
                    )
            finally:
                task.cancel()
                result = await asyncio.gather(task, return_exceptions=True)
                await bot.close()
                assert isinstance(result[0], asyncio.CancelledError), result
                assert sender is not None and sender.done()

    asyncio.run(run())
