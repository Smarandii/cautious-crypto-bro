"""Ambiguous ownership pauses durably without touching the exchange."""

import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.domain import ApprovalMode, StrategyStatus
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "second_status", [StrategyStatus.ENTERING, StrategyStatus.UNCERTAIN]
)
def test_duplicate_owners_pause_and_alert_after_restart(tmp_path, second_status):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            # Historical overlapping ladders may predate the current OPEN guard.
            other = plan.model_copy(update={"intent_id": uuid4()})
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "INSERT INTO execution_plans(intent_id,payload_json,created_at) "
                    "VALUES(?,?,?)",
                    (
                        str(other.intent_id),
                        other.model_dump_json(),
                        other.created_at.isoformat(),
                    ),
                )
                db.commit()
            await store.ensure_position_strategy(other)
            records = await store.get_active_position_strategies()
            second = next(
                state for state, _ in records if state.strategy_id == other.intent_id
            )
            await store.save_position_strategy(
                second.model_copy(update={"status": second_status})
            )
            before = {
                state.strategy_id: state
                for state, _ in await store.get_active_position_strategies()
            }
            request_count = len(exchange.requests)
            await supervisor.reconcile_once()

            reopened = IntentStore(database)
            alerts = await reopened.get_pending_strategy_alerts()
            assert len(alerts) == 2
            assert not await reopened.get_active_position_strategies()
            for state in alerts:
                assert state.status is StrategyStatus.MANUAL_OVERRIDE
                assert "Multiple active strategies" in state.pause_reason
                assert state.symbol in state.pause_reason
                assert not state.pause_notified
                unchanged = {"status", "pause_reason", "pause_notified", "updated_at"}
                assert state.model_dump(exclude=unchanged) == before[
                    state.strategy_id
                ].model_dump(exclude=unchanged)

            restarted = PositionSupervisor(
                store=reopened, executor=executor, mutation_lock=asyncio.Lock()
            )
            await restarted.reconcile_once()
            bot = ApprovalBot(
                token="123456:synthetic-integration-token",
                approval_chat_id=1,
                approver_user_id=1,
                store=reopened,
                executor=coordinator,
            )
            try:
                with patch.object(
                    bot._bot,
                    "send_message",
                    new=AsyncMock(side_effect=OSError("offline")),
                ):
                    await bot.deliver_strategy_alerts()
                assert (
                    len(await IntentStore(database).get_pending_strategy_alerts()) == 2
                )
                sender = AsyncMock()
                with patch.object(bot._bot, "send_message", new=sender):
                    await bot.deliver_strategy_alerts()
                    await bot.deliver_strategy_alerts()
                assert sender.await_count == 2
                for state in alerts:
                    assert any(
                        str(state.strategy_id) in call.kwargs["text"]
                        for call in sender.call_args_list
                    )
                assert not await IntentStore(database).get_pending_strategy_alerts()
                assert len(exchange.requests) == request_count
            finally:
                await bot.close()

    asyncio.run(run())
