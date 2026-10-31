"""Expired lifecycle evidence is durable information, not an executable approval."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from test_remaining_concerns import executor_for, make_intent
from test_workflow_integration import ExchangeTransport, ModelTransport, workflow

from cautious_crypto_bro.domain import (
    AccountSnapshotDelivery,
    ApprovalMode,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
)
from cautious_crypto_bro.signals.delivery import SignalDelivery
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


async def seed(path, *, age_seconds=1800):
    store = IntentStore(path)
    await store.initialize()
    source = make_intent().source.model_copy(
        update={
            "published_at": datetime.now(UTC) - timedelta(seconds=age_seconds),
            "recovery_review_reason": "Recovered outside automatic window",
        }
    )
    action = PositionActionIntent(
        source=source,
        symbol="BTCUSDT",
        action=PositionActionType.CLOSE,
        expected_side=Side.LONG,
        summary="Explicit close",
        confidence=1,
    )
    token = await store.claim_source(source, lease_seconds=300)
    assert await store.create_signal_batch_and_complete_source([], (action,), token)
    return store, action


@pytest.mark.parametrize("max_age,expired", [(900, True), (3600, False)])
def test_manual_delivery_uses_configured_original_publication_deadline(
    tmp_path, max_age, expired
):
    async def run():
        store, action = await seed(tmp_path / "state.sqlite3")
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, ModelTransport(), max_age=max_age
            )
            sender = AsyncMock()
            snapshot = AccountSnapshotDelivery(
                state=None, state_error="Fixture unavailable", pnl=None, pnl_error=None
            )
            try:
                before = list(exchange.requests)
                with patch.object(bot._bot, "send_message", new=sender):
                    card = await service.delivery.manual(action, snapshot=snapshot)
                assert card is not expired
                saved = await store.get_position_action(action.action_id)
                assert saved.status is (
                    IntentStatus.SKIPPED if expired else IntentStatus.PENDING
                )
                if expired:
                    assert sender.await_count == 1
                    assert "EXPIRED LIFECYCLE" in sender.call_args.kwargs["text"]
                    assert not sender.call_args.kwargs.get("reply_markup")
                else:
                    assert sender.await_count == 2
                    assert sender.call_args.kwargs.get("reply_markup")
                assert exchange.requests == before and not exchange.orders
            finally:
                await bot.close()

    asyncio.run(run())


def test_expired_notice_retries_after_network_failure_and_process_restart(tmp_path):
    async def run():
        path = tmp_path / "state.sqlite3"
        store, action = await seed(path)
        with closing(sqlite3.connect(path)) as db:
            payload = db.execute(
                "SELECT payload_json FROM position_actions"
            ).fetchone()[0]
        with executor_for(ExchangeTransport()) as executor:
            service, bot, _ = workflow(store, executor, ModelTransport())
            try:
                with patch.object(
                    bot._bot,
                    "send_message",
                    new=AsyncMock(side_effect=OSError("offline")),
                ):
                    assert not await service.delivery.manual(action)
                assert (
                    await store.get_position_action(action.action_id)
                ).status is IntentStatus.SKIPPED
                with closing(sqlite3.connect(path)) as db:
                    assert (
                        db.execute(
                            "SELECT delivered_at FROM manual_deliveries"
                        ).fetchone()[0]
                        is None
                    )
                    db.execute(
                        "UPDATE manual_deliveries SET retry_after=datetime('now','-1 second')"
                    )
                    db.commit()
                restored = SignalDelivery(
                    store=IntentStore(path), executor=executor, approval_bot=bot
                )
                sender = AsyncMock()
                with patch.object(bot._bot, "send_message", new=sender):
                    await restored.recover_manual()
                    await restored.recover_manual()
                assert sender.await_count == 1 and not sender.call_args.kwargs.get(
                    "reply_markup"
                )
                with closing(sqlite3.connect(path)) as db:
                    assert (
                        db.execute(
                            "SELECT payload_json FROM position_actions"
                        ).fetchone()[0]
                        == payload
                    )
                    assert db.execute(
                        "SELECT delivered_at FROM manual_deliveries"
                    ).fetchone()[0]
                assert not await store.pending_manual_deliveries()
            finally:
                await bot.close()

    asyncio.run(run())


def test_competing_delivery_owners_send_one_expired_notice(tmp_path):
    async def run():
        path = tmp_path / "state.sqlite3"
        store, action = await seed(path)
        with executor_for(ExchangeTransport()) as executor:
            service, bot, _ = workflow(store, executor, ModelTransport())
            entered = asyncio.Event()
            release = asyncio.Event()

            async def send(**kwargs):
                entered.set()
                await release.wait()

            try:
                with patch.object(
                    bot._bot, "send_message", new=AsyncMock(side_effect=send)
                ) as sender:
                    first = asyncio.create_task(service.delivery.manual(action))
                    await asyncio.wait_for(entered.wait(), 3)
                    other = SignalDelivery(
                        store=IntentStore(path), executor=executor, approval_bot=bot
                    )
                    assert not await other.manual(action)
                    release.set()
                    assert not await first
                    assert sender.await_count == 1
                assert not await store.pending_manual_deliveries()
            finally:
                release.set()
                await bot.close()

    asyncio.run(run())


def test_expiring_after_valid_delivery_still_fails_at_execution_boundary(
    tmp_path, monkeypatch
):
    async def run():
        store, action = await seed(tmp_path / "state.sqlite3", age_seconds=60)
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, ModelTransport())
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()) as sender:
                    assert await service.delivery.manual(action)
                    assert sender.call_args.kwargs.get("reply_markup")
                import cautious_crypto_bro.execution_coordinator as module

                future = datetime.now(UTC) + timedelta(seconds=1000)

                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return future if tz else future.replace(tzinfo=None)

                monkeypatch.setattr(module, "datetime", Clock)
                before = list(exchange.requests)
                outcome = await service._coordinator.execute_position_action(
                    action.action_id, approval_mode=ApprovalMode.MANUAL, user_id=1
                )
                assert (
                    outcome.status is IntentStatus.FAILED and "stale" in outcome.message
                )
                assert exchange.requests == before and not exchange.orders
            finally:
                await bot.close()

    asyncio.run(run())


def test_expiry_preparation_does_not_overwrite_an_execution_claim(tmp_path):
    async def run():
        store, action = await seed(tmp_path / "state.sqlite3")
        assert await store.claim_position_action_for_execution(
            action.action_id, 1, expected_approval_mode=ApprovalMode.MANUAL
        )
        with executor_for(ExchangeTransport()) as executor:
            service, bot, _ = workflow(store, executor, ModelTransport())
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()) as sender:
                    assert not await service.delivery.manual(action)
                assert sender.await_count == 0
                assert (
                    await store.get_position_action(action.action_id)
                ).status is IntentStatus.EXECUTING
            finally:
                await bot.close()

    asyncio.run(run())
