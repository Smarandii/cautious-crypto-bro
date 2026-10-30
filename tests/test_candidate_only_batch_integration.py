"""Candidate-only source commits retain validation, atomicity and approval history."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from test_remaining_concerns import make_intent

from cautious_crypto_bro.domain import ApprovalMode
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


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
        candidates = [(intent, False, deadline)]
        if failure == "empty":
            candidates = []
        elif failure == "mixed":
            other = make_intent().model_copy(
                update={"source": intent.source.model_copy(update={"message_id": 2})}
            )
            candidates.append((other, False, deadline))
        elif failure == "naive":
            candidates = [(intent, False, deadline.replace(tzinfo=None))]
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
            (), (), claim, planning_candidates=[(intent, False, deadline)]
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
        candidate = await IntentStore(database).claim_planning_candidate()
        assert candidate is not None
        assert (
            candidate.intent.intent_id == intent.intent_id
            and candidate.deadline == deadline
        )
        assert candidate.intent.approval_mode is (
            ApprovalMode.MANUAL if legacy else mode
        )

    asyncio.run(run())
