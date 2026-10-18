"""Durable notification workflows with real SQLite, coordinator and HTTP adapter."""

import asyncio
import sqlite3
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from test_remaining_concerns import executor_for, prepare, source
from test_workflow_integration import ExchangeTransport, ModelTransport, workflow

from cautious_crypto_bro.domain import (
    ApprovalMode,
    AutoApprovalMode,
    IntentStatus,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    Side,
)
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


async def automatic_entry(database, exchange, executor):
    store, intent, plan, _, _ = await prepare(
        database, exchange, executor, mode=ApprovalMode.AUTO
    )
    intent = intent.model_copy(update={"relation": OpenRelation.NEW})
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE intents SET payload_json = ? WHERE intent_id = ?",
            (intent.model_dump_json(), str(intent.intent_id)),
        )
    return store, intent, plan


async def persist_copy(store, intent, plan, *, message_id, mode=ApprovalMode.AUTO):
    copied = intent.model_copy(
        update={
            "intent_id": uuid4(),
            "source": intent.source.model_copy(update={"message_id": message_id}),
            "approval_mode": mode,
        }
    )
    copied_plan = plan.model_copy(update={"intent_id": copied.intent_id})
    claim = await store.claim_source(copied.source, lease_seconds=300)
    assert claim is not None
    assert await store.create_signal_batch_and_complete_source(
        ((copied, copied_plan),), (), claim
    )
    return copied


def make_due(database):
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE manual_deliveries SET retry_after = NULL, claim_token = NULL WHERE delivered_at IS NULL"
        )
        for table in ("intents", "position_actions"):
            db.execute(f"UPDATE {table} SET updated_at = '2000-01-01 00:00:00'")


async def one_delivery_cycle(service):
    async def stop(_seconds):
        raise asyncio.CancelledError

    with patch("cautious_crypto_bro.service.asyncio.sleep", new=stop):
        with pytest.raises(asyncio.CancelledError):
            await service.run_delivery_recovery()


@pytest.mark.parametrize("legacy_pending", [False, True])
def test_successful_trade_notification_survives_outage_restart_and_disabled_auto(
    tmp_path, legacy_pending
):
    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, _ = await automatic_entry(database, exchange, executor)
            if legacy_pending:
                with sqlite3.connect(database) as db:
                    db.execute("DELETE FROM manual_deliveries")
            service, bot, _ = workflow(
                store, executor, ModelTransport(), mode=AutoApprovalMode.ALL
            )

            async def unavailable(**kwargs):
                if "AUTO-EXECUTED" in kwargs["text"]:
                    raise RuntimeError("Telegram unavailable")

            try:
                with patch.object(
                    bot._bot, "send_message", new=AsyncMock(side_effect=unavailable)
                ):
                    await service.recover_auto_execution()
                assert (
                    await store.get_intent(intent.intent_id)
                ).status is IntentStatus.EXECUTED
                assert not await store.get_pending_auto_intent_ids()
                make_due(database)
                restarted, retry_bot, _ = workflow(
                    IntentStore(database),
                    executor,
                    ModelTransport(),
                    mode=AutoApprovalMode.DISABLED,
                )
                requests_before = list(exchange.requests)
                sender = AsyncMock()
                try:
                    with patch.object(retry_bot._bot, "send_message", new=sender):
                        await restarted.recover_auto_execution()
                        await one_delivery_cycle(restarted)
                        await one_delivery_cycle(restarted)
                    notices = [
                        call.kwargs["text"]
                        for call in sender.call_args_list
                        if "AUTO-EXECUTED" in call.kwargs["text"]
                    ]
                    assert len(notices) == 1, (
                        "Executed AUTO record lost its notification after Telegram outage"
                    )
                    assert "RECOVERED EXECUTION RECORD" in notices[0]
                    assert "not a new trade" in notices[0]
                    assert exchange.requests == requests_before, (
                        "Retry queried or mutated the exchange"
                    )
                finally:
                    await retry_bot.close()
            finally:
                await bot.close()

    asyncio.run(run())


def test_action_outcome_retries_from_durable_facts_without_guessing_fills(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        action = PositionActionIntent(
            source=source("Reduce BTC", 2),
            symbol="BTCUSDT",
            action=PositionActionType.REDUCE,
            close_pct=50,
            expected_side=Side.LONG,
            summary="Reduce half",
            confidence=1,
            approval_mode=ApprovalMode.AUTO,
        )
        claim = await store.claim_source(action.source, lease_seconds=300)
        assert await store.create_signal_batch_and_complete_source((), (action,), claim)
        exchange = ExchangeTransport()
        exchange.size, exchange.stop = Decimal("1"), Decimal("90")
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, ModelTransport(), mode=AutoApprovalMode.ALL
            )
            try:
                with patch.object(
                    bot._bot,
                    "send_message",
                    new=AsyncMock(side_effect=RuntimeError("Telegram offline")),
                ):
                    await service.recover_auto_execution()
                assert (
                    await store.get_position_action(action.action_id)
                ).status is IntentStatus.EXECUTED
                assert exchange.size == Decimal("0.5") and exchange.stop == Decimal(
                    "90"
                )
                make_due(database)
                requests_before = list(exchange.requests)
                sender = AsyncMock()
                with patch.object(bot._bot, "send_message", new=sender):
                    await service.recover_auto_outcome_deliveries()
                text = sender.call_args.kwargs["text"]
                assert (
                    "AUTO-EXECUTED" in text
                    and "Exact fill details are unavailable" in text
                )
                assert "RECOVERED EXECUTION RECORD" in text
                assert str(action.action_id) in text
                assert exchange.requests == requests_before
            finally:
                await bot.close()

    asyncio.run(run())


def test_send_acceptance_then_ack_failure_can_duplicate_notice_but_not_trade(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, _ = await automatic_entry(database, exchange, executor)
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TRIGGER reject_ack BEFORE UPDATE ON manual_deliveries WHEN NEW.delivered_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'ack unavailable'); END"
                )
            service, bot, _ = workflow(
                store, executor, ModelTransport(), mode=AutoApprovalMode.ALL
            )
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    await service.recover_auto_execution()
                    assert (
                        await store.get_intent(intent.intent_id)
                    ).status is IntentStatus.EXECUTED
                    with sqlite3.connect(database) as db:
                        db.execute(
                            "UPDATE intents SET updated_at = '2000-01-01 00:00:00'"
                        )
                    assert not await store.pending_auto_outcome_deliveries(), (
                        "Active lease must survive failed acknowledgement"
                    )
                    requests_before = list(exchange.requests)
                    with sqlite3.connect(database) as db:
                        db.execute("DROP TRIGGER reject_ack")
                    make_due(database)
                    await service.recover_auto_outcome_deliveries()
                    await service.recover_auto_outcome_deliveries()
                notices = [
                    call.kwargs["text"]
                    for call in sender.call_args_list
                    if "AUTO-EXECUTED" in call.kwargs["text"]
                ]
                assert len(notices) == 2
                assert exchange.requests == requests_before
            finally:
                await bot.close()

    asyncio.run(run())


def test_legacy_interrupted_work_is_notified_without_replaying_old_history(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, plan = await automatic_entry(database, exchange, executor)
            history = await persist_copy(store, intent, plan, message_id=3)
            await store.mark_executed(history.intent_id, ("old-history-order",))
            assert await store.claim_for_execution(
                intent.intent_id, None, expected_approval_mode=ApprovalMode.AUTO
            )
            with sqlite3.connect(database) as db:
                db.execute("DELETE FROM manual_deliveries")
            assert (await store.quarantine_interrupted_executions())[0] == (
                intent.intent_id,
            )
            make_due(database)
            service, bot, _ = workflow(
                IntentStore(database),
                executor,
                ModelTransport(),
                mode=AutoApprovalMode.DISABLED,
            )
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    await service.recover_auto_execution()
                    await one_delivery_cycle(service)
                assert sender.await_count == 1
                text = sender.call_args.kwargs["text"]
                assert (
                    "UNCERTAIN" in text and "Process restarted during execution" in text
                )
                assert "old-history-order" not in text
                assert not exchange.requests
            finally:
                await bot.close()

    asyncio.run(run())


def test_notice_reservation_failure_rolls_back_source_work_before_execution(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, plan = await automatic_entry(database, exchange, executor)
            copied = intent.model_copy(
                update={"intent_id": uuid4(), "source": source(message_id=2)}
            )
            copied_plan = plan.model_copy(update={"intent_id": copied.intent_id})
            claim = await store.claim_source(copied.source, lease_seconds=300)
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TRIGGER reject_reservation BEFORE INSERT ON manual_deliveries BEGIN SELECT RAISE(ABORT, 'notice storage unavailable'); END"
                )
            with pytest.raises(
                sqlite3.IntegrityError, match="notice storage unavailable"
            ):
                await store.create_signal_batch_and_complete_source(
                    ((copied, copied_plan),), (), claim
                )
            assert await store.get_intent(copied.intent_id) is None
            assert await store.get_execution_plan(copied.intent_id) is None
            with sqlite3.connect(database) as db:
                assert (
                    db.execute(
                        "SELECT status FROM source_messages WHERE message_id = 2"
                    ).fetchone()[0]
                    == "PROCESSING"
                )
            assert not exchange.requests

    asyncio.run(run())


@pytest.mark.parametrize("status", [IntentStatus.FAILED, IntentStatus.SKIPPED])
def test_failed_and_withdrawn_auto_work_has_durable_notices(tmp_path, status):
    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, _ = await automatic_entry(database, exchange, executor)
            if status is IntentStatus.FAILED:
                await store.mark_failed(intent.intent_id, "Preflight rejected")
            else:
                await store.mark_skipped(intent.intent_id, 42)
            make_due(database)
            service, bot, _ = workflow(
                store, executor, ModelTransport(), mode=AutoApprovalMode.DISABLED
            )
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    await service.recover_auto_outcome_deliveries()
                assert (
                    sender.await_count == 1
                    and status.value in sender.call_args.kwargs["text"]
                )
                assert not exchange.requests
            finally:
                await bot.close()

    asyncio.run(run())


def test_broken_manual_card_does_not_block_auto_outcomes(tmp_path, caplog):
    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, plan = await automatic_entry(database, exchange, executor)
            broken = await persist_copy(
                store, intent, plan, message_id=3, mode=ApprovalMode.MANUAL
            )
            with sqlite3.connect(database) as db:
                db.execute(
                    "UPDATE execution_plans SET payload_json = '{}' WHERE intent_id = ?",
                    (str(broken.intent_id),),
                )
            await store.mark_executed(intent.intent_id, ("recorded-order",))
            make_due(database)
            service, bot, _ = workflow(
                store, executor, ModelTransport(), mode=AutoApprovalMode.DISABLED
            )
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    await one_delivery_cycle(service)
                assert (
                    sender.await_count == 1
                    and "recorded-order" in sender.call_args.kwargs["text"]
                )
                assert "Manual approval recovery failed" in caplog.text
                assert not exchange.requests
            finally:
                await bot.close()

    asyncio.run(run())


def test_corrupt_auto_notice_backs_off_without_blocking_other_notices(tmp_path, caplog):
    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, broken, plan = await automatic_entry(database, exchange, executor)
            healthy = await persist_copy(store, broken, plan, message_id=3)
            for item in (broken, healthy):
                await store.mark_executed(item.intent_id, (str(item.intent_id),))
            with sqlite3.connect(database) as db:
                db.execute(
                    "UPDATE execution_plans SET payload_json = '{}' WHERE intent_id = ?",
                    (str(broken.intent_id),),
                )
            make_due(database)
            service, bot, _ = workflow(
                store, executor, ModelTransport(), mode=AutoApprovalMode.DISABLED
            )
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    await service.recover_auto_outcome_deliveries()
                    assert sender.await_count == 1
                    assert str(healthy.intent_id) in sender.call_args.kwargs["text"]
                    assert not await store.pending_auto_outcome_deliveries()
                    with sqlite3.connect(database) as db:
                        pending, token, retry = db.execute(
                            "SELECT delivered_at, claim_token, retry_after FROM manual_deliveries WHERE record_id = ?",
                            (str(broken.intent_id),),
                        ).fetchone()
                        assert pending is None and token is None and retry is not None
                        db.execute(
                            "UPDATE execution_plans SET payload_json = ? WHERE intent_id = ?",
                            (plan.model_dump_json(), str(broken.intent_id)),
                        )
                    make_due(database)
                    await service.recover_auto_outcome_deliveries()
                    await service.recover_auto_outcome_deliveries()
                assert sender.await_count == 2
                assert str(broken.intent_id) in sender.call_args.kwargs["text"]
                assert "AUTO outcome retry failed" in caplog.text
                assert not exchange.requests
            finally:
                await bot.close()

    asyncio.run(run())


def test_delivery_batches_eventually_drain_more_than_one_hundred_records(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, plan = await automatic_entry(database, exchange, executor)
            batch_source = source(message_id=2)
            copies = [
                intent.model_copy(update={"intent_id": uuid4(), "source": batch_source})
                for _ in range(100)
            ]
            claim = await store.claim_source(batch_source, lease_seconds=300)
            assert await store.create_signal_batch_and_complete_source(
                tuple(
                    (item, plan.model_copy(update={"intent_id": item.intent_id}))
                    for item in copies
                ),
                (),
                claim,
            )
            for number, item in enumerate([intent, *copies]):
                await store.mark_executed(item.intent_id, (f"recorded-{number}",))
            make_due(database)
            service, bot, _ = workflow(
                store, executor, ModelTransport(), mode=AutoApprovalMode.DISABLED
            )
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    for _ in range(3):
                        await service.recover_auto_outcome_deliveries()
                assert sender.await_count == 101
                assert not await store.pending_auto_outcome_deliveries()
                assert not await store.get_pending_auto_intent_ids()
                assert not exchange.requests
            finally:
                await bot.close()

    asyncio.run(run())
