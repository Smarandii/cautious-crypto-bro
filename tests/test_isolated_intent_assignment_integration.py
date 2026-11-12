"""Exercise durable handoff between isolated intake and Demo execution state."""

import asyncio
from dataclasses import replace

import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro.domain import ApprovalMode, IntentStatus, StoreError
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.storage.isolated_intake import (
    DemoIntentAssignment,
    IsolatedDemoIntake,
)

pytestmark = pytest.mark.integration


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
