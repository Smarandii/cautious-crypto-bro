"""Recovery warning reservations must survive restarts and commit atomically."""

import asyncio
import sqlite3
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, source, state_for
from test_workflow_integration import ModelTransport, workflow

from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
    StrategyStatus,
)
from cautious_crypto_bro.storage import IntentStore

app = import_module("cautious_crypto_bro.main")
pytestmark = pytest.mark.integration


async def executing_records(database, exchange, executor):
    store, intent, plan, _, _ = await prepare(database, exchange, executor)
    await store.claim_for_execution(
        intent.intent_id, 1, expected_approval_mode=ApprovalMode.MANUAL
    )
    await store.ensure_position_strategy(plan)
    action = await executing_action(store, 2)
    return store, intent, action


async def executing_action(store, message_id):
    action = PositionActionIntent(
        source=source(message_id=message_id),
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
    return action


def test_failed_warning_survives_restart_and_success_is_acknowledged(
    tmp_path, monkeypatch
):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, action = await executing_records(
                database, exchange, executor
            )
            ids = ((intent.intent_id,), (action.action_id,))
            assert await store.quarantine_interrupted_executions() == ids
            _, bot, _ = workflow(store, executor, ModelTransport())
            attempted = asyncio.Event()
            notices = []

            async def failed_send(*args, **kwargs):
                attempted.set()
                raise OSError("Synthetic Telegram outage")

            monkeypatch.setattr(bot._bot, "send_message", failed_send)
            task = asyncio.create_task(
                app._deliver_recovery_warning(
                    SimpleNamespace(store=store, bot=bot),
                    retry_seconds=3600,
                )
            )
            try:
                await asyncio.wait_for(attempted.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            reopened = IntentStore(database)
            assert await reopened.quarantine_interrupted_executions() == ((), ())
            assert await reopened.pending_recovery_notices() == ids

            async def sent(*args, **kwargs):
                notices.append(kwargs["text"])

            monkeypatch.setattr(bot._bot, "send_message", sent)
            try:
                await app._deliver_recovery_warning(
                    SimpleNamespace(store=reopened, bot=bot)
                )
                assert len(notices) == 1
                assert "OPEN intents: <b>1</b>" in notices[0]
                assert "Position actions: <b>1</b>" in notices[0]
                assert await IntentStore(database).pending_recovery_notices() == (
                    (),
                    (),
                )
                await app._deliver_recovery_warning(
                    SimpleNamespace(store=IntentStore(database), bot=bot)
                )
                assert len(notices) == 1
                assert (
                    await reopened.get_intent(intent.intent_id)
                ).status is IntentStatus.UNCERTAIN
                assert (await state_for(reopened)).status is StrategyStatus.UNCERTAIN
            finally:
                await bot.close()

    asyncio.run(run())


def test_notice_reservation_failure_rolls_back_quarantine(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, action = await executing_records(
                database, exchange, executor
            )
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TRIGGER reject_notice BEFORE INSERT ON interrupted_execution_notices "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic notice write failure'); END"
                )
            with pytest.raises(sqlite3.IntegrityError, match="synthetic notice"):
                await store.quarantine_interrupted_executions()
            reopened = IntentStore(database)
            assert (
                await reopened.get_intent(intent.intent_id)
            ).status is IntentStatus.EXECUTING
            assert (
                await reopened.get_position_action(action.action_id)
            ).status is IntentStatus.EXECUTING
            assert (await state_for(reopened)).status is StrategyStatus.ENTERING
            assert await reopened.pending_recovery_notices() == ((), ())

    asyncio.run(run())


def test_failed_acknowledgement_retains_the_entire_batch(tmp_path, monkeypatch):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, action = await executing_records(
                database, exchange, executor
            )
            ids = ((intent.intent_id,), (action.action_id,))
            await store.quarantine_interrupted_executions()
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TRIGGER reject_ack BEFORE DELETE ON interrupted_execution_notices "
                    "WHEN OLD.kind='ACTION' BEGIN "
                    "SELECT RAISE(ABORT, 'synthetic acknowledgement failure'); END"
                )
            with pytest.raises(
                sqlite3.IntegrityError, match="synthetic acknowledgement"
            ):
                await store.acknowledge_recovery_notices(*ids)
            assert await IntentStore(database).pending_recovery_notices() == ids
            _, bot, _ = workflow(store, executor, ModelTransport())
            notices = []

            async def sent(*args, **kwargs):
                notices.append(kwargs["text"])
                if len(notices) == 2:
                    with sqlite3.connect(database) as db:
                        db.execute("DROP TRIGGER reject_ack")

            monkeypatch.setattr(bot._bot, "send_message", sent)
            try:
                async with asyncio.timeout(3):
                    await app._deliver_recovery_warning(
                        SimpleNamespace(store=store, bot=bot), retry_seconds=0.01
                    )
                assert (
                    len(notices) == 2
                )  # At least once after an acknowledgement failure.
                assert await store.pending_recovery_notices() == ((), ())
            finally:
                await bot.close()

    asyncio.run(run())


def test_acknowledgement_does_not_remove_a_later_reservation(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, action = await executing_records(
                database, exchange, executor
            )
            captured = await store.quarantine_interrupted_executions()
            later = await executing_action(store, 3)
            assert await store.quarantine_interrupted_executions() == (
                (),
                (later.action_id,),
            )
            await store.acknowledge_recovery_notices(*captured)
            assert await IntentStore(database).pending_recovery_notices() == (
                (),
                (later.action_id,),
            )
            await store.acknowledge_recovery_notices((uuid4(),), (uuid4(),))
            assert await store.pending_recovery_notices() == ((), (later.action_id,))

    asyncio.run(run())
