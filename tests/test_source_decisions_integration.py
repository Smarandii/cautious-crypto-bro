import asyncio
import json
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from test_remaining_concerns import executor_for, make_intent
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.domain import IncomingPost
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.signals.extraction import IntentExtractor
from cautious_crypto_bro.source_coverage import (
    coverage_summary,
    decision_disposition,
    planning_rejection_kind,
    production_decision,
    record_disposition_status,
)
from cautious_crypto_bro.storage import LATEST_SCHEMA_SQL, IntentStore
from cautious_crypto_bro.storage.schema import (
    PLANNING_CANDIDATE_SCHEMA_SQL,
    SOURCE_DECISION_SCHEMA_SQL,
    ZONE_HISTORY_SCHEMA_SQL,
    ZONE_LEDGER_SCHEMA_SQL,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            "BTCUSDT: ExecutionPlanningError: Primary V2 entry is too small to "
            "support any fixed partial exit",
            "minimum_size_or_fixed_exit",
        ),
        (
            "BTCUSDT: ExecutionPlanningError: E2 risk allocation rounds below "
            "Bybit minimum quantity",
            "minimum_size_or_fixed_exit",
        ),
        (
            "BTCUSDT: ExecutionPlanningError: Rounded LONG take-profit geometry "
            "is invalid",
            "price_geometry",
        ),
        (
            "BTCUSDT: portfolio stop-risk cap: no capacity",
            "portfolio_risk_cap",
        ),
        (
            "PEPEUSDT: TradeExecutionError: Bybit rejected request: 10001 "
            "params error: symbol invalid",
            "exchange_rejected_request",
        ),
        ("unexpected planning failure", "other"),
    ],
)
def test_planning_rejection_kind_is_aggregate_and_non_identifying(error, expected):
    assert planning_rejection_kind(error) == expected


@pytest.mark.parametrize(
    "case, expected",
    [
        ("commentary", "PROCESSED_NO_OP"),
        ("filtered", "NORMALIZATION_FILTERED"),
        ("planning", "PLANNING_REJECTED"),
        ("transport", "UNRESOLVED"),
        ("executed", "EXECUTED"),
    ],
)
def test_original_decisions_survive_restart_without_model_or_cache(
    tmp_path, case, expected
):
    class Model:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            response = {"actionable": False, "reason": "Commentary"}
            if case == "filtered":
                response.update(
                    actionable=True,
                    position_actions=[
                        {
                            "symbol": "BTCUSDT",
                            "action": "CLOSE",
                            "evidence_text": "Close BTCUSDT now",
                            "summary": "Close",
                            "confidence": 1,
                        }
                    ],
                )
            elif case in {"planning", "transport", "executed"}:
                response.update(
                    actionable=True,
                    intents=[
                        {
                            "symbol": "BTCUSDT",
                            "side": "LONG",
                            "entry": "MARKET",
                            "stop_loss": 110 if case == "planning" else 90,
                            "relation": "NEW",
                            "summary": "Enter now",
                            "confidence": 1,
                        }
                    ],
                )
            return LLMResponse(json.dumps(response))

        async def close(self):
            pass

    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        source = make_intent().source.model_copy(update={"text": "Enter now"})
        provider = Model()
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, provider)
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    with (
                        patch.object(
                            executor,
                            "market_context",
                            side_effect=RuntimeError("temporary market outage"),
                        )
                        if case == "transport"
                        else nullcontext()
                    ):
                        await service.on_message(IncomingPost(source=source))
                assert provider.calls == 1
                if case in {"planning", "transport"}:
                    assert exchange.orders == {}
                if case == "planning":
                    with sqlite3.connect(database) as db:
                        status, reason = db.execute(
                            "SELECT status,last_error FROM source_messages"
                        ).fetchone()
                    assert status == "COMPLETED"
                    assert "ExecutionPlanningError" in reason
                    assert not await store.get_retryable_source_message_ids(
                        source.channel_id, lease_seconds=300
                    )
                    with sqlite3.connect(database) as db:
                        assert db.execute(
                            "SELECT COUNT(*) FROM intents"
                        ).fetchone() == (0,)
                    # Restart and replay must not download/extract/submit again.
                    service, restarted_bot, _ = workflow(
                        IntentStore(database), executor, provider
                    )
                    try:
                        await service.on_message(IncomingPost(source=source))
                        assert provider.calls == 1
                    finally:
                        await restarted_bot.close()
                if case == "transport":
                    assert not await store.get_retryable_source_message_ids(
                        source.channel_id, lease_seconds=300
                    )
                    with sqlite3.connect(database) as db:
                        assert db.execute(
                            "SELECT status FROM source_messages"
                        ).fetchone() == ("COMPLETED",)
                        assert db.execute(
                            "SELECT status FROM planning_candidates"
                        ).fetchone() == ("PENDING",)
            finally:
                await bot.close()
        # A new store process and no provider/cache are sufficient to audit.
        await IntentStore(database).initialize()
        async with aiosqlite.connect(database) as db:
            decision = await production_decision(
                db, source.channel_id, source.message_id
            )
            assert decision["evaluation_fingerprint"]
            assert decision["evaluation_model"] == "test"
            assert decision["source"]["text"] == "Enter now"
            if case == "filtered":
                assert len(decision["extraction"]["position_actions"]) == 1
                assert decision["normalized"]["position_actions"] == []
            if case == "planning":
                assert decision["planning"]["errors"]
            if case == "executed":
                assert decision["planning"]["planned_intent_ids"]
        summary = await coverage_summary(
            database, since=datetime.now(UTC) - timedelta(days=1)
        )
        assert summary["dispositions"] == {expected: 1}
        assert summary["missing_original_decisions"] == 0
        if case == "planning":
            assert sum(summary["planning_rejection_reasons"].values()) == 1
        else:
            assert summary["planning_rejection_reasons"] == {}
        assert "Enter now" not in json.dumps(summary)
        assert source.channel_title not in json.dumps(summary)

    asyncio.run(run())


def test_stale_attempt_cannot_replace_new_evidence_and_history_stays_unknown(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        source = make_intent().source
        first = await store.claim_source(source, lease_seconds=300)
        assert await store.record_source_decision(source, first, '{"attempt":1}')
        assert await store.mark_source_failed(source, first, "retry")
        second = await store.claim_source(source, lease_seconds=300)
        async with aiosqlite.connect(database) as db:
            assert (
                await production_decision(db, source.channel_id, source.message_id)
                is None
            )
        assert not await store.record_source_decision(source, first, '{"stale":true}')
        assert await store.record_source_decision(source, second, '{"attempt":2}')
        assert await store.mark_source_completed(source, second)
        assert not await store.record_source_decision(source, second, '{"late":true}')
        await IntentStore(database).initialize()
        async with aiosqlite.connect(database) as db:
            assert await production_decision(
                db, source.channel_id, source.message_id
            ) == {"attempt": 2}
            rows = await (
                await db.execute(
                    "SELECT payload_json FROM source_decisions ORDER BY attempt_count"
                )
            ).fetchall()
            assert [json.loads(row[0]) for row in rows] == [
                {"attempt": 1},
                {"attempt": 2},
            ]

    asyncio.run(run())


def test_schema10_upgrade_preserves_rows_and_does_not_invent_decisions(tmp_path):
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as db:
        db.executescript(
            LATEST_SCHEMA_SQL.replace(PLANNING_CANDIDATE_SCHEMA_SQL, "")
            .replace(SOURCE_DECISION_SCHEMA_SQL, "")
            .replace(ZONE_LEDGER_SCHEMA_SQL, "")
            .replace(ZONE_HISTORY_SCHEMA_SQL, "")
        )
        db.execute("PRAGMA user_version = 10")
        source = make_intent().source
        db.execute(
            "INSERT INTO source_messages(channel_id,message_id,payload_json) VALUES (?,?,?)",
            (source.channel_id, source.message_id, source.model_dump_json()),
        )
        before = db.execute("SELECT * FROM source_messages").fetchall()
    asyncio.run(IntentStore(database).initialize())
    asyncio.run(IntentStore(database).initialize())
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM source_messages").fetchall() == before
        assert db.execute("SELECT COUNT(*) FROM source_decisions").fetchone() == (0,)
    result = asyncio.run(
        coverage_summary(database, since=datetime.now(UTC) - timedelta(days=1))
    )
    assert result["dispositions"] == {"UNKNOWN": 1}


def test_partial_batch_uncertainty_is_not_hidden_by_one_execution():
    decision = {
        "normalized": {"open_intents": [{}], "position_actions": []},
        "planning": {
            "planned_intent_ids": ["one", "two"],
            "position_action_ids": [],
            "errors": [],
        },
    }
    assert (
        decision_disposition(decision, "COMPLETED", ["EXECUTED", "UNCERTAIN"])
        == "UNRESOLVED"
    )
    assert (
        decision_disposition(decision, "COMPLETED", ["EXECUTED", "PENDING"])
        == "AWAITING_DECISION"
    )
    assert (
        decision_disposition(
            decision, "COMPLETED", [record_disposition_status("PENDING", "SKIPPED")]
        )
        == "SKIPPED"
    )
    assert (
        decision_disposition(
            decision, "COMPLETED", [record_disposition_status("PENDING", "AUTO")]
        )
        == "UNRESOLVED"
    )


def test_media_rejection_uses_durable_source_reason_without_a_model_call(tmp_path):
    class NoModel:
        async def complete(self, request):
            raise AssertionError("Rejected media must never reach the model")

        async def close(self):
            pass

    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        with executor_for(ExchangeTransport()) as executor:
            service, bot, _ = workflow(store, executor, NoModel())
            try:
                await service.on_message(
                    IncomingPost(
                        source=make_intent().source,
                        rejection_reason="Unsupported media",
                    )
                )
            finally:
                await bot.close()
        summary = await coverage_summary(
            database, since=datetime.now(UTC) - timedelta(days=1)
        )
        assert summary["dispositions"] == {"SOURCE_REJECTED": 1}
        assert summary["missing_original_decisions"] == 0

    asyncio.run(run())


def test_failed_decision_write_blocks_exchange_submission_but_retains_original(
    tmp_path,
):
    class Model:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            assert self.calls == 1, "Retry must reuse the original cached evaluation"
            return LLMResponse(
                json.dumps(
                    {
                        "actionable": True,
                        "reason": "Enter now",
                        "intents": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "LONG",
                                "entry": "MARKET",
                                "stop_loss": 90,
                                "relation": "NEW",
                                "summary": "Enter",
                                "confidence": 1,
                            }
                        ],
                    }
                )
            )

        async def close(self):
            pass

    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        with sqlite3.connect(database) as db:
            db.executescript("""
                CREATE TRIGGER fail_planned_decision BEFORE UPDATE ON source_decisions
                WHEN json_extract(NEW.payload_json, '$.phase') = 'PLANNED'
                BEGIN SELECT RAISE(ABORT, 'synthetic unavailable write'); END;
            """)

        class Cache:
            def __init__(self):
                self.values = {}

            async def get_evaluation(self, fingerprint):
                return self.values.get(fingerprint)

            async def cache_evaluation(
                self, fingerprint, payload_json, duration_seconds
            ):
                self.values[fingerprint] = payload_json

        source = make_intent().source
        exchange = ExchangeTransport()
        model = Model()
        cache = Cache()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, model)
            service._extractor = IntentExtractor(
                provider=model, cache_identity="test", evaluation_cache=cache
            )
            try:
                await service.on_message(IncomingPost(source=source))
                assert exchange.size == 0
                with sqlite3.connect(database) as db:
                    assert db.execute("SELECT COUNT(*) FROM intents").fetchone() == (0,)
                    assert db.execute(
                        "SELECT status FROM source_messages"
                    ).fetchone() == ("FAILED",)
                    assert (
                        json.loads(
                            db.execute(
                                "SELECT payload_json FROM source_decisions"
                            ).fetchone()[0]
                        )["phase"]
                        == "NORMALIZED"
                    )
                    db.execute("DROP TRIGGER fail_planned_decision")
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await service.on_message(IncomingPost(source=source))
                assert exchange.size > 0
                assert model.calls == 1
                cache.values.clear()
            finally:
                await bot.close()
        with sqlite3.connect(database) as db:
            receipts = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload_json FROM source_decisions ORDER BY attempt_count"
                )
            ]
            assert [row["phase"] for row in receipts] == ["NORMALIZED", "PLANNED"]
            assert (
                receipts[0]["evaluation_fingerprint"]
                == receipts[1]["evaluation_fingerprint"]
            )
            assert receipts[0]["extraction"] == receipts[1]["extraction"]

    asyncio.run(run())
