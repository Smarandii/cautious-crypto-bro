"""Exercise durable handoff between isolated intake and Demo execution state."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_remaining_concerns import Exchange, executor_for, make_intent, prepare

from cautious_crypto_bro.approval_bot import ApprovalBot, IntentAction
from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    StoreError,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.isolated_demo_dispatch import IsolatedDemoDispatcher
from cautious_crypto_bro.isolated_demo_risk import IsolatedDemoPortfolio
from cautious_crypto_bro.signals.delivery import SignalDelivery
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.storage._connection import sqlite_connection
from cautious_crypto_bro.storage.isolated_intake import (
    ASSIGNMENT_SCHEMA,
    DemoIntentAssignment,
    IsolatedDemoIntake,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "action_type", [PositionActionType.CLOSE, PositionActionType.CANCEL_ENTRIES]
)
def test_saved_lifecycle_target_rejects_replaced_owner(tmp_path, action_type):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, _ = await prepare(
                tmp_path / "worker.db", exchange, executor
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
            source = intent.source.model_copy(
                update={"message_id": intent.source.message_id + 1}
            )
            action = PositionActionIntent(
                source=source,
                symbol=intent.symbol,
                action=action_type,
                expected_side=intent.side,
                target_strategy_id=uuid4(),
                summary="Manage the saved target",
                confidence=1,
            )
            claim = await store.claim_source(source, lease_seconds=300)
            assert claim is not None
            assert await store.create_signal_batch_and_complete_source(
                (), (action,), claim
            )
            before = len(exchange.requests)
            result = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert result.status is IntentStatus.FAILED
            assert "Saved lifecycle target" in result.message
            assert len(exchange.requests) == before

    asyncio.run(run())


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("primary", [False, True])
def test_source_completion_commits_all_account_assignments(tmp_path, conflict, primary):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            _, first, plan, _, _ = await prepare(
                tmp_path / "ordinary.db", exchange, executor
            )
        sibling = make_intent().model_copy(update={"source": first.source})
        sibling_plan = plan.model_copy(update={"intent_id": sibling.intent_id})
        assignments = (
            DemoIntentAssignment(101, first, plan),
            DemoIntentAssignment(202, sibling, sibling_plan),
        )
        path = tmp_path / "intake.db"
        intake = IsolatedDemoIntake(path)
        store = IntentStore(path)
        if primary:
            await store.initialize()
        else:
            await intake.initialize()
        claim = await store.claim_source(first.source, lease_seconds=300)
        assert claim is not None
        items = ((first, plan), (sibling, sibling_plan))
        assert not await store.create_signal_batch_and_complete_source(
            items, (), "stale-claim", isolated_assignments=assignments
        )
        assert await intake.get_assignment(first.intent_id) is None

        if conflict:
            if primary:
                async with sqlite_connection(path) as db:
                    await db.execute(ASSIGNMENT_SCHEMA)
                    await db.commit()
            await intake.assign(replace(assignments[1], uid=101))
            with pytest.raises(StoreError, match="cannot be replaced"):
                await store.create_signal_batch_and_complete_source(
                    items, (), claim, isolated_assignments=assignments
                )
            assert await intake.get_assignment(first.intent_id) is None
            assert await store.get_intent(first.intent_id) is None
            assert not await store.source_completed(
                first.source.channel_id, first.source.message_id
            )
            return

        assert await store.create_signal_batch_and_complete_source(
            items, (), claim, isolated_assignments=assignments
        )
        assert not await store.create_signal_batch_and_complete_source(
            items, (), claim, isolated_assignments=assignments
        )
        reopened = IsolatedDemoIntake(path)
        for assignment in assignments:
            saved = await reopened.get_assignment(assignment.intent.intent_id)
            assert saved == assignment and saved is not None
            worker = IntentStore(tmp_path / f"worker-{assignment.uid}.db")
            await worker.initialize_isolated_demo(assignment.uid)
            await worker.import_isolated_intent(saved)
            assert await worker.get_execution_plan(saved.intent.intent_id) == saved.plan
        assert await store.source_completed(
            first.source.channel_id, first.source.message_id
        )
        assert set(await store.pending_manual_deliveries()) == {
            (first.intent_id, "OPEN"),
            (sibling.intent_id, "OPEN"),
        }
        sender = SimpleNamespace(send_intent=AsyncMock())
        delivery = SignalDelivery(
            store=IntentStore(path),
            executor=executor,
            approval_bot=sender,
            isolated_intake=reopened,
        )
        await delivery.recover_manual()
        assert sender.send_intent.await_count == 2
        assert all(
            call.kwargs == {"isolated": True}
            for call in sender.send_intent.await_args_list
        )
        assert not await store.pending_manual_deliveries()

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["skip_first", "execute_first", "race"])
def test_dispatch_reopens_assignment_and_serializes_approval_outcomes(
    tmp_path, scenario
):
    async def run():
        assigned_exchange, other_exchange = Exchange(), Exchange()
        with (
            executor_for(assigned_exchange) as assigned,
            executor_for(other_exchange) as other,
        ):
            _, intent, plan, _, _ = await prepare(
                tmp_path / "ordinary.db", assigned_exchange, assigned
            )
            intake_path = tmp_path / "intake.db"
            intake = IsolatedDemoIntake(intake_path)
            await intake.initialize()
            await intake.assign(DemoIntentAssignment(101, intent, plan))
            worker = IntentStore(tmp_path / "assigned.db")
            sibling = IntentStore(tmp_path / "other.db")
            await worker.initialize_isolated_demo(101)
            await sibling.initialize_isolated_demo(202)
            portfolio = IsolatedDemoPortfolio(
                ((assigned, worker), (other, sibling)), Decimal("400")
            )
            dispatcher = IsolatedDemoDispatcher(
                IsolatedDemoIntake(intake_path),
                {101: (assigned, worker), 202: (other, sibling)},
                portfolio,
                ExecutionPlanner(),
                900,
            )
            unavailable = IsolatedDemoDispatcher(
                intake, {202: (other, sibling)}, portfolio, ExecutionPlanner(), 900
            )
            result = await unavailable.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert result.message == "Assigned Demo worker is unavailable"
            assert not assigned_exchange.requests and not other_exchange.requests

            async def execute():
                return await dispatcher.execute_intent(
                    intent.intent_id, approval_mode=ApprovalMode.MANUAL
                )

            if scenario == "skip_first":
                assert await dispatcher.mark_skipped(intent.intent_id, 42)
            if scenario == "race":
                first, second, skipped = await asyncio.gather(
                    execute(), execute(), dispatcher.mark_skipped(intent.intent_id, 42)
                )
            else:
                first, second = await asyncio.gather(execute(), execute())
                skipped = scenario == "skip_first"
            expected = IntentStatus.SKIPPED if skipped else IntentStatus.EXECUTED
            assert first.status is second.status is expected
            assert not await dispatcher.mark_skipped(intent.intent_id, 42)
            assert len(assigned_exchange.orders) == (0 if skipped else 3)
            assert not other_exchange.orders

    asyncio.run(run())


def test_telegram_isolated_skip_and_execute_use_the_same_worker(tmp_path):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor, executor_for(Exchange()) as sibling:
            primary_store, intent, plan, primary, _ = await prepare(
                tmp_path / "primary.db", exchange, executor
            )
            worker = IntentStore(tmp_path / "worker.db")
            await worker.initialize_isolated_demo(101)
            intake = IsolatedDemoIntake(tmp_path / "intake.db")
            await intake.initialize()
            await intake.assign(DemoIntentAssignment(101, intent, plan))
            portfolio = IsolatedDemoPortfolio(
                ((executor, worker), (sibling, primary_store)), Decimal("400")
            )
            dispatcher = IsolatedDemoDispatcher(
                intake, {101: (executor, worker)}, portfolio, ExecutionPlanner(), 900
            )
            bot = ApprovalBot(
                token="123456:offline",
                approval_chat_id=1,
                approver_user_id=42,
                store=primary_store,
                executor=primary,
            )
            bot._edit_message = AsyncMock()
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=42), answer=AsyncMock()
            )
            execute = IntentAction(
                action="iso_execute", intent_id=str(intent.intent_id)
            )
            skip = IntentAction(action="iso_skip", intent_id=str(intent.intent_id))
            try:
                await bot._execute(callback, execute)
                assert await worker.get_intent(intent.intent_id) is None
                bot.bind_isolated_dispatcher(dispatcher)
                await bot._skip(callback, skip)
                await bot._execute(callback, execute)
                assert (
                    await worker.get_intent(intent.intent_id)
                ).status is IntentStatus.SKIPPED
                assert (
                    await primary_store.get_intent(intent.intent_id)
                ).status is IntentStatus.PENDING
                assert not exchange.orders
            finally:
                await bot.close()

    asyncio.run(run())


def test_frozen_assignment_survives_restart_and_duplicate_delivery(tmp_path):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            _, intent, plan, _, _ = await prepare(
                tmp_path / "ordinary.db", exchange, executor
            )
            intake_path = tmp_path / "intake.db"
            intake = IsolatedDemoIntake(intake_path)
            await intake.initialize()
            assignment = DemoIntentAssignment(101, intent, plan)
            await intake.assign(assignment)

            with pytest.raises(
                RuntimeError, match="Unsupported database schema version"
            ):
                await IntentStore(intake_path).initialize()
            with pytest.raises(StoreError, match="fresh or bound"):
                await IntentStore(intake_path).initialize_isolated_demo(101)

            reopened = IsolatedDemoIntake(intake_path)
            await reopened.initialize()
            saved = await reopened.get_assignment(intent.intent_id)
            assert saved == assignment and saved is not None

            sibling = IntentStore(tmp_path / "sibling.db")
            await sibling.initialize_isolated_demo(202)
            with pytest.raises(StoreError, match="does not match"):
                await sibling.import_isolated_intent(saved)
            assert await sibling.get_intent(intent.intent_id) is None

            worker_path = tmp_path / "worker.db"
            worker = IntentStore(worker_path)
            await worker.initialize_isolated_demo(101)
            await asyncio.gather(
                *(worker.import_isolated_intent(saved) for _ in range(2))
            )
            coordinator = ExecutionCoordinator(
                store=worker,
                executor=executor,
                planner=ExecutionPlanner(),
                max_age_seconds=900,
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
            request_count = len(exchange.requests)

            restarted = IntentStore(worker_path)
            await restarted.initialize_isolated_demo(101)
            await restarted.import_isolated_intent(saved)
            current = await restarted.get_intent(intent.intent_id)
            assert current is not None and current.status is IntentStatus.EXECUTED
            with pytest.raises(StoreError, match="cannot be replaced"):
                await restarted.import_isolated_intent(
                    replace(
                        saved, intent=intent.model_copy(update={"summary": "changed"})
                    )
                )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
            assert len(exchange.requests) == request_count

    asyncio.run(run())


def test_competing_intake_assignments_cannot_replace_the_winner(tmp_path):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            _, intent, plan, _, _ = await prepare(
                tmp_path / "ordinary.db", exchange, executor
            )
        intake = IsolatedDemoIntake(tmp_path / "intake.db")
        await intake.initialize()
        candidates = [DemoIntentAssignment(uid, intent, plan) for uid in (101, 202)]
        outcomes = await asyncio.gather(
            *(intake.assign(candidate) for candidate in candidates),
            return_exceptions=True,
        )
        assert outcomes.count(None) == 1
        assert isinstance(
            next(outcome for outcome in outcomes if outcome is not None), StoreError
        )
        winner = candidates[outcomes.index(None)]
        await intake.assign(winner)
        assert await intake.get_assignment(intent.intent_id) == winner

    asyncio.run(run())
