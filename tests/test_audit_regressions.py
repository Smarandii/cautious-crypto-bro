import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

import pytest
from test_remaining_concerns import (
    D,
    Exchange,
    executor_for,
    make_intent,
    prepare,
    source,
)

from cautious_crypto_bro.bybit import EntryPreflightError
from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentExtraction,
    IntentStatus,
    StrategyStatus,
)
from cautious_crypto_bro.execution import ExecutionPlanner, InstrumentContext
from cautious_crypto_bro.openrouter import _signals_from_extraction


@pytest.mark.parametrize("stage", ["before_submit", "accepted_timeout", "after_fill"])
def test_entry_failure_only_closes_strategy_before_submission(tmp_path, stage):
    async def run():
        path = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, _ = await prepare(
                path, exchange, executor
            )
            original_post = executor._private_post

            def accepted_timeout(*args, **kwargs):
                original_post(*args, **kwargs)
                raise TimeoutError("Response lost after acceptance")

            if stage == "before_submit":
                exchange.mark = D("200")
            elif stage == "after_fill":
                executor.execute_remaining_entries = AsyncMock(
                    side_effect=EntryPreflightError("Remaining legs failed validation")
                )
            with patch.object(
                executor,
                "_private_post",
                accepted_timeout if stage == "accepted_timeout" else original_post,
            ):
                result = await coordinator.execute_intent(
                    intent.intent_id, approval_mode=ApprovalMode.MANUAL
                )
            assert result.status is IntentStatus.FAILED
            with closing(sqlite3.connect(path)) as db:
                status = db.execute(
                    "SELECT status FROM position_strategies"
                ).fetchone()[0]
            assert status == ("CLOSED" if stage == "before_submit" else "UNCERTAIN")
            assert len(exchange.orders) == (0 if stage == "before_submit" else 1)
            if stage == "before_submit":
                assert not await store.get_active_position_strategies()
                exchange.mark = D("100")
                retry = make_intent().model_copy(
                    update={"source": source("Fresh entry", 2)}
                )
                retry_plan = plan.model_copy(update={"intent_id": retry.intent_id})
                claim = await store.claim_source(retry.source, lease_seconds=300)
                await store.create_signal_batch_and_complete_source(
                    ((retry, retry_plan),), (), claim
                )
                assert (
                    await coordinator.execute_intent(
                        retry.intent_id, approval_mode=ApprovalMode.MANUAL
                    )
                ).status is IntentStatus.EXECUTED

    asyncio.run(run())


@pytest.mark.parametrize("paused", [False, True])
def test_manual_second_entry_cannot_disable_existing_strategy(tmp_path, paused):
    async def run():
        path = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, first, plan, coordinator, supervisor = await prepare(
                path, exchange, executor
            )
            assert (
                await coordinator.execute_intent(
                    first.intent_id, approval_mode=ApprovalMode.MANUAL
                )
            ).status is IntentStatus.EXECUTED
            await supervisor.reconcile_once()
            if paused:
                await store.set_position_strategy_status(
                    first.intent_id, StrategyStatus.MANUAL_OVERRIDE
                )
            second = make_intent().model_copy(
                update={"source": source("New proposal", 2)}
            )
            second_plan = ExecutionPlanner().plan(
                second,
                plan.policy,
                InstrumentContext(D("100"), D(".1"), D(".001"), D(".001"), D("5")),
            )
            claim = await store.claim_source(second.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source(
                ((second, second_plan),), (), claim
            )
            before = len(exchange.orders)
            result = await coordinator.execute_intent(
                second.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert result.status is IntentStatus.FAILED
            assert "EntryPreflightError" in result.message
            await supervisor.reconcile_once()
            assert len(exchange.orders) == before
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute(
                    "SELECT strategy_id, status FROM position_strategies"
                ).fetchall()
            assert rows == [
                (str(first.intent_id), "MANUAL_OVERRIDE" if paused else "OPEN_RISK")
            ]

    asyncio.run(run())


@pytest.mark.parametrize(
    "caption,allowed",
    [
        ("Тейк после перезахода, по текущим, дальше скажу, стоп в бу!", False),
        ("Хоть какой то профит есть, еще не закрываю", False),
        ("Здесь можно делать какие-то тейки. Всех с профитом!", False),
        ("Still holding. Move stop to breakeven.", False),
        ("Беру TAO в лонг маркетом. Тейки позже.", True),
        ("Open TAO long now. Take profit at 310.", True),
        ("TAO long at market. Take profit at 310.", True),
        ("", True),
    ],
)
def test_market_update_cannot_become_new_entry(caption, allowed):
    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Screenshot",
            "intents": [
                {
                    "symbol": "TAOUSDT",
                    "side": "LONG",
                    "entry": "MARKET",
                    "relation": "NEW",
                    "summary": "Screenshot",
                    "confidence": 1,
                }
            ],
        }
    )
    result = _signals_from_extraction(source(caption), extraction)
    assert bool(result.open_intents) is allowed
