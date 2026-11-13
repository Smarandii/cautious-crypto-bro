"""Exercise durable handoff between isolated intake and Demo execution state."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro.domain import ApprovalMode, IntentStatus, StoreError
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.isolated_demo_dispatch import IsolatedDemoDispatcher
from cautious_crypto_bro.isolated_demo_risk import IsolatedDemoPortfolio
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.storage.isolated_intake import (
    DemoIntentAssignment,
    IsolatedDemoIntake,
)

pytestmark = pytest.mark.integration


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
