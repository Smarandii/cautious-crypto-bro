"""Persisted review evidence must stay distinct from orders and no-ops."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_filled_entry_review_integration import CAPTION, ReviewModel
from test_remaining_concerns import make_intent

from cautious_crypto_bro import source_coverage
from cautious_crypto_bro.domain import IntentExtraction
from cautious_crypto_bro.signals.normalization import _signals_from_extraction
from cautious_crypto_bro.source_coverage import coverage_summary, decision_disposition
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "delivery", ["pending", "delivered", "missing", "legacy", "mixed", "concurrent"]
)
def test_review_report_uses_durable_delivery_without_writing(
    tmp_path, delivery, monkeypatch
):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        source = make_intent().source.model_copy(update={"text": CAPTION})
        raw = IntentExtraction.model_validate(ReviewModel().payload)
        signals = _signals_from_extraction(source, raw)
        token = await store.claim_source(source, lease_seconds=300)
        decision = {
            "extraction": raw.model_dump(mode="json"),
            "normalized": {
                "open_intents": [],
                "position_actions": [],
                "entry_reviews": [
                    r.model_dump(mode="json") for r in signals.entry_reviews
                ],
            },
        }
        if delivery == "mixed":
            intent = make_intent().model_copy(update={"source": source})
            decision["normalized"]["open_intents"] = [intent.model_dump(mode="json")]
            with sqlite3.connect(path) as db:
                db.execute(
                    "INSERT INTO intents(intent_id,channel_id,message_id,payload_json,"
                    "status,created_at,updated_at) VALUES (?,?,?,?,'UNCERTAIN',"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                    (
                        str(intent.intent_id),
                        source.channel_id,
                        source.message_id,
                        intent.model_dump_json(),
                    ),
                )
        assert await store.record_source_decision(source, token, json.dumps(decision))
        if delivery in {"pending", "delivered", "mixed", "concurrent"}:
            assert await store.save_entry_reviews(
                source, token, signals.entry_reviews, "synthetic-fingerprint"
            )
        if delivery in {"delivered", "mixed"}:
            review = signals.entry_reviews[0]
            lease = await store.claim_entry_review(review)
            assert lease
            await store.finish_entry_review(review, lease, delivered=True)
        assert await store.mark_source_completed(source, token)
        if delivery == "legacy":
            with sqlite3.connect(path) as db:
                db.execute("DROP TABLE entry_reviews")
        if delivery == "concurrent":
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA journal_mode=WAL")
            original = source_coverage.production_decision

            async def concurrent_delivery(db, channel_id, message_id):
                decision = await original(db, channel_id, message_id)
                with sqlite3.connect(path) as writer:
                    writer.execute(
                        "UPDATE entry_reviews SET delivered_at=CURRENT_TIMESTAMP"
                    )
                assert db.total_changes == 0
                return decision

            monkeypatch.setattr(
                source_coverage, "production_decision", concurrent_delivery
            )
        before = path.read_bytes()
        result = await coverage_summary(
            path, since=datetime.now(UTC) - timedelta(days=1)
        )
        if delivery != "concurrent":
            assert path.read_bytes() == before
        else:
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT delivered_at FROM entry_reviews").fetchone()[
                    0
                ]
        assert result["dispositions"] == {
            "UNRESOLVED" if delivery == "mixed" else "HUMAN_REVIEW": 1
        }
        assert result["normalized_entry_reviews"] == 1
        expected = (
            None
            if delivery == "legacy"
            else {
                "PENDING": int(delivery in {"pending", "concurrent"}),
                "DELIVERED": int(delivery in {"delivered", "mixed"}),
            }
        )
        assert result["linked_entry_review_deliveries"] == expected
        assert result["linked_record_statuses"] == (
            {"UNCERTAIN": 1} if delivery == "mixed" else {}
        )
        assert CAPTION not in json.dumps(result) and "BTCUSDT" not in json.dumps(result)

    asyncio.run(run())


@pytest.mark.parametrize(
    "statuses,planning_statuses,expected",
    [
        (["EXECUTED", "UNCERTAIN"], [], "UNRESOLVED"),
        (["EXECUTED", "PENDING"], [], "AWAITING_DECISION"),
        (["EXECUTED"], ["PENDING"], "UNRESOLVED"),
        (["EXECUTED"], ["REJECTED"], "PARTIAL_PLANNING_FAILURE"),
    ],
)
def test_mixed_review_does_not_mask_execution(statuses, planning_statuses, expected):
    decision = {
        "normalized": {
            "open_intents": [{}],
            "position_actions": [],
            "entry_reviews": [{}],
        },
    }
    assert (
        decision_disposition(
            decision, "COMPLETED", statuses, planning_statuses=planning_statuses
        )
        == expected
    )


def test_filtered_review_and_legacy_noop_remain_distinct():
    decision = {
        "normalized": {"open_intents": [], "position_actions": []},
        "extraction": {"intents": [], "position_actions": []},
    }
    assert decision_disposition(decision, "COMPLETED", []) == "PROCESSED_NO_OP"
    decision["extraction"]["entry_reviews"] = [{}]
    assert decision_disposition(decision, "COMPLETED", []) == "NORMALIZATION_FILTERED"
