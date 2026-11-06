"""Candidate-only source commits retain validation, atomicity and approval history."""

import asyncio
import importlib.util
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_remaining_concerns import make_intent

from cautious_crypto_bro.domain import (
    ApprovalMode,
    PlanningCandidate,
    PositionActionIntent,
    PositionActionType,
)
from cautious_crypto_bro.source_coverage import coverage_summary
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("fresh_sibling", [False, True])
def test_source_completion_records_expired_candidate_without_claim(
    tmp_path, fresh_sibling
):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        intent = make_intent()
        source = intent.source
        claim = await store.claim_source(source, lease_seconds=300)
        assert claim is not None
        action = PositionActionIntent(
            source=source,
            symbol="BTCUSDT",
            action=PositionActionType.CLOSE,
            summary="Synthetic close",
            confidence=1,
        )
        assert await store.record_source_decision(
            source,
            claim,
            json.dumps(
                {"normalized": {"open_intents": [{}], "position_actions": [{}]}}
            ),
        )
        deadline = datetime.now(UTC) - timedelta(seconds=30)
        candidates = [
            PlanningCandidate(intent=intent, duplicate_in_batch=True, deadline=deadline)
        ]
        if fresh_sibling:
            fresh = intent.model_copy(update={"intent_id": uuid4()})
            candidates.append(
                PlanningCandidate(
                    intent=fresh,
                    duplicate_in_batch=True,
                    deadline=deadline + timedelta(minutes=10),
                )
            )
        assert await store.create_signal_batch_and_complete_source(
            (), (action,), claim, planning_candidates=candidates
        )
        with closing(sqlite3.connect(database)) as db:
            assert db.execute(
                "SELECT status,attempt_count,claim_token,deadline FROM planning_candidates WHERE intent_id=?",
                (str(intent.intent_id),),
            ).fetchone() == ("EXPIRED", 0, None, deadline.isoformat())
            assert db.execute("SELECT count(*) FROM position_actions").fetchone() == (
                1,
            )
        expected = "UNRESOLVED" if fresh_sibling else "PARTIAL_PLANNING_FAILURE"
        report = await coverage_summary(
            database, since=datetime.now(UTC) - timedelta(days=1)
        )
        assert report["dispositions"] == {expected: 1}
        spec = importlib.util.spec_from_file_location(
            "audit_recent_signals_test",
            Path(__file__).resolve().parents[1] / "scripts/audit_recent_signals.py",
        )
        assert spec is not None and spec.loader is not None
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        state = await audit.current_app_state(
            database, source.channel_id, source.message_id
        )
        assert state["production_disposition"] == expected
        assert "EXPIRED" in state["planning_candidate_statuses"]
        recovered = await store.claim_planning_candidate()
        if fresh_sibling:
            assert recovered is not None
            assert recovered.candidate.intent.intent_id == fresh.intent_id
        else:
            assert recovered is None
        assert await store.claim_planning_candidate() is None

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["empty", "mixed", "naive", "lost", "rollback"])
def test_candidate_only_batch_cannot_complete_without_valid_owned_candidates(
    tmp_path, failure
):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        intent = make_intent()
        claim = await store.claim_source(intent.source, lease_seconds=300)
        assert claim is not None
        active_claim = claim
        deadline = intent.source.published_at + timedelta(seconds=900)
        candidates = [
            PlanningCandidate(
                intent=intent, duplicate_in_batch=False, deadline=deadline
            )
        ]
        if failure == "empty":
            candidates = []
        elif failure == "mixed":
            other = make_intent().model_copy(
                update={"source": intent.source.model_copy(update={"message_id": 2})}
            )
            candidates.append(
                PlanningCandidate(
                    intent=other, duplicate_in_batch=False, deadline=deadline
                )
            )
        elif failure == "naive":
            candidates = [
                PlanningCandidate(
                    intent=intent,
                    duplicate_in_batch=False,
                    deadline=deadline.replace(tzinfo=None),
                )
            ]
        elif failure == "lost":
            assert await store.mark_source_failed(
                intent.source, claim, "Synthetic interruption"
            )
            active_claim = await store.claim_source(intent.source, lease_seconds=300)
            assert active_claim is not None and active_claim != claim
        else:
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "CREATE TRIGGER reject_candidate BEFORE INSERT ON planning_candidates "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END"
                )
                db.commit()
        if failure == "lost":
            assert not await store.create_signal_batch_and_complete_source(
                (), (), claim, planning_candidates=candidates
            )
        else:
            error = sqlite3.IntegrityError if failure == "rollback" else ValueError
            with pytest.raises(error):
                await store.create_signal_batch_and_complete_source(
                    (), (), claim, planning_candidates=candidates
                )
        with closing(sqlite3.connect(database)) as db:
            assert db.execute(
                "SELECT count(*) FROM planning_candidates"
            ).fetchone() == (0,)
            assert db.execute(
                "SELECT status,claim_token FROM source_messages"
            ).fetchone() == ("PROCESSING", active_claim)

    asyncio.run(run())


@pytest.mark.parametrize("mode", [ApprovalMode.AUTO, ApprovalMode.MANUAL])
@pytest.mark.parametrize("legacy", [False, True])
def test_candidate_approval_provenance_is_explicit_and_legacy_defaults_manual(
    tmp_path, mode, legacy
):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        intent = make_intent(mode=mode)
        deadline = intent.source.published_at + timedelta(seconds=900)
        claim = await store.claim_source(intent.source, lease_seconds=300)
        assert claim is not None
        assert await store.create_signal_batch_and_complete_source(
            (),
            (),
            claim,
            planning_candidates=[
                PlanningCandidate(
                    intent=intent, duplicate_in_batch=False, deadline=deadline
                )
            ],
        )
        with closing(sqlite3.connect(database)) as db:
            payload = json.loads(
                db.execute("SELECT payload_json FROM planning_candidates").fetchone()[0]
            )
            assert payload["approval_mode"] == mode
            assert db.execute("SELECT status FROM source_messages").fetchone() == (
                "COMPLETED",
            )
            assert db.execute("SELECT count(*) FROM intents").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM manual_deliveries").fetchone() == (
                0,
            )
            if legacy:
                del payload["approval_mode"]
                db.execute(
                    "UPDATE planning_candidates SET payload_json=?",
                    (json.dumps(payload),),
                )
                db.commit()
        recovered = await IntentStore(database).claim_planning_candidate()
        assert recovered is not None
        candidate = recovered.candidate
        assert (
            candidate.intent.intent_id == intent.intent_id
            and candidate.deadline == deadline
        )
        assert candidate.intent.approval_mode is (
            ApprovalMode.MANUAL if legacy else mode
        )

    asyncio.run(run())
