import asyncio
import json
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from test_remaining_concerns import executor_for, make_intent
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.domain import AutoApprovalMode, IncomingPost
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.source_coverage import coverage_summary
from cautious_crypto_bro.storage import LATEST_SCHEMA_SQL, IntentStore
from cautious_crypto_bro.storage.schema import PLANNING_CANDIDATE_SCHEMA_SQL

pytestmark = pytest.mark.integration


def test_schema12_upgrade_preserves_sources_without_replaying_history(tmp_path):
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as db:
        db.executescript(LATEST_SCHEMA_SQL.replace(PLANNING_CANDIDATE_SCHEMA_SQL, ""))
        db.execute("PRAGMA user_version=12")
        db.execute(
            "INSERT INTO source_messages(channel_id,message_id,payload_json,status) "
            "VALUES (1,2,'{}','COMPLETED')"
        )
        original = db.execute("SELECT * FROM source_messages").fetchall()
    asyncio.run(IntentStore(database).initialize())
    asyncio.run(IntentStore(database).initialize())
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (14,)
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert db.execute("SELECT * FROM source_messages").fetchall() == original
        assert db.execute("SELECT COUNT(*) FROM planning_candidates").fetchone() == (0,)


@pytest.mark.parametrize(
    "recovery", ["available", "expired", "unavailable", "duplicate", "rollback"]
)
def test_partial_batch_recovers_after_restart_without_reextracting(tmp_path, recovery):
    class Model:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            return LLMResponse(
                json.dumps(
                    {
                        "actionable": True,
                        "reason": "Two explicit entries",
                        "intents": [
                            {
                                "symbol": symbol,
                                "side": "LONG",
                                "entry": "MARKET",
                                "stop_loss": 90,
                                "relation": "NEW",
                                "summary": "Enter now",
                                "confidence": 1,
                            }
                            for symbol in (
                                "BTCUSDT",
                                "BTCUSDT" if recovery == "duplicate" else "ETHUSDT",
                            )
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
        source = make_intent().source.model_copy(update={"text": "Enter both now"})
        provider = Model()
        exchange = ExchangeTransport()
        mode = (
            AutoApprovalMode.OPEN_ONLY
            if recovery == "duplicate"
            else AutoApprovalMode.DISABLED
        )
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, provider, mode=mode)
            market_context = executor.market_context
            market_calls = 0

            async def unavailable_eth(symbol):
                nonlocal market_calls
                market_calls += 1
                if symbol == "ETHUSDT" or (
                    recovery == "duplicate" and market_calls == 2
                ):
                    raise RuntimeError("temporary ETH market outage")
                return await market_context(symbol)

            try:
                with (
                    patch.object(executor, "market_context", unavailable_eth),
                    patch.object(bot._bot, "send_message", new=AsyncMock()),
                ):
                    await service.on_message(IncomingPost(source=source))
                with sqlite3.connect(database) as db:
                    original = db.execute("SELECT intent_id FROM intents").fetchall()
                    assert len(original) == 1
                    decision = json.loads(
                        db.execute(
                            "SELECT payload_json FROM source_decisions"
                        ).fetchone()[0]
                    )
                    expected_ids = {
                        item["intent_id"]
                        for item in decision["normalized"]["open_intents"]
                    }
                assert len(expected_ids) == 2
                summary = await coverage_summary(
                    database, since=datetime.now(UTC) - timedelta(days=1)
                )
                assert summary["dispositions"] == {"UNRESOLVED": 1}
            finally:
                await bot.close()

            restarted_store = IntentStore(database)
            await restarted_store.initialize()
            if recovery == "available":
                first = await restarted_store.claim_planning_candidate()
                assert first is not None
                assert await IntentStore(database).claim_planning_candidate() is None
                with sqlite3.connect(database) as db:
                    db.execute(
                        "UPDATE planning_candidates SET retry_after=datetime('now','-1 second')"
                    )
                second = await IntentStore(database).claim_planning_candidate()
                assert second is not None
                assert second.intent.intent_id == first.intent.intent_id
                assert second.claim_token != first.claim_token
                assert not await restarted_store.finish_planning_candidate(
                    first.intent.intent_id,
                    first.claim_token,
                    status="REJECTED",
                    error="stale owner",
                )
                assert await restarted_store.finish_planning_candidate(
                    second.intent.intent_id,
                    second.claim_token,
                    status="PENDING",
                    error="temporary outage",
                )
                with sqlite3.connect(database) as db:
                    db.execute(
                        "UPDATE planning_candidates SET retry_after=datetime('now','-1 second')"
                    )
            if recovery == "expired":
                with sqlite3.connect(database) as db:
                    db.execute(
                        "UPDATE planning_candidates SET deadline=?",
                        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
                    )
            restarted, restarted_bot, _ = workflow(
                restarted_store, executor, provider, mode=mode
            )
            try:
                if recovery == "rollback":
                    with sqlite3.connect(database) as db:
                        db.execute(
                            "CREATE TRIGGER reject_recovered_delivery BEFORE INSERT ON manual_deliveries "
                            "BEGIN SELECT RAISE(ABORT, 'synthetic delivery persistence failure'); END"
                        )
                    await restarted.recover_planning_candidates()
                    with sqlite3.connect(database) as db:
                        assert db.execute(
                            "SELECT COUNT(*) FROM intents"
                        ).fetchone() == (1,)
                        assert db.execute(
                            "SELECT COUNT(*) FROM execution_plans"
                        ).fetchone() == (1,)
                        assert db.execute(
                            "SELECT status FROM planning_candidates"
                        ).fetchone() == ("PENDING",)
                        db.execute("DROP TRIGGER reject_recovered_delivery")
                        db.execute(
                            "UPDATE planning_candidates SET retry_after=datetime('now','-1 second')"
                        )
                with (
                    patch.object(restarted_bot._bot, "send_message", new=AsyncMock()),
                    patch.object(executor, "market_context", unavailable_eth)
                    if recovery == "unavailable"
                    else nullcontext(),
                ):
                    for _ in range(2):
                        with (
                            patch(
                                "cautious_crypto_bro.service.asyncio.sleep",
                                new=AsyncMock(side_effect=asyncio.CancelledError),
                            ),
                            pytest.raises(asyncio.CancelledError),
                        ):
                            await restarted.run_delivery_recovery()
                    await restarted.on_message(IncomingPost(source=source))
                with sqlite3.connect(database) as db:
                    actual_ids = {
                        row[0] for row in db.execute("SELECT intent_id FROM intents")
                    }
                    assert actual_ids == (
                        expected_ids
                        if recovery in {"available", "duplicate", "rollback"}
                        else {original[0][0]}
                    )
                    assert original[0][0] in actual_ids
                    assert db.execute(
                        "SELECT COUNT(*) FROM execution_plans"
                    ).fetchone() == (
                        2 if recovery in {"available", "duplicate", "rollback"} else 1,
                    )
                    assert db.execute(
                        "SELECT DISTINCT approval_mode FROM intents"
                    ).fetchall() == [("MANUAL",)]
                    if recovery == "unavailable":
                        # Two worker passes must not retry again during backoff.
                        assert db.execute(
                            "SELECT attempt_count FROM planning_candidates"
                        ).fetchone() == (1,)
                assert provider.calls == 1
                assert exchange.orders == {}
                summary = await coverage_summary(
                    database, since=datetime.now(UTC) - timedelta(days=1)
                )
                status, disposition = {
                    "available": ("TRANSFERRED", "AWAITING_DECISION"),
                    "duplicate": ("TRANSFERRED", "AWAITING_DECISION"),
                    "rollback": ("TRANSFERRED", "AWAITING_DECISION"),
                    "expired": ("EXPIRED", "PARTIAL_PLANNING_FAILURE"),
                    "unavailable": ("PENDING", "UNRESOLVED"),
                }[recovery]
                assert summary["planning_candidate_statuses"] == {status: 1}
                assert summary["dispositions"] == {disposition: 1}
                if recovery == "available":
                    # Model the persisted AUTO handoff before dispatch, then prove
                    # that UNCERTAIN is never selected for execution recovery.
                    with sqlite3.connect(database) as db:
                        recovered_id = db.execute(
                            "SELECT intent_id FROM planning_candidates"
                        ).fetchone()[0]
                        db.execute(
                            "UPDATE intents SET approval_mode='AUTO',status='UNCERTAIN' WHERE intent_id=?",
                            (recovered_id,),
                        )
                    auto_service, auto_bot, _ = workflow(
                        IntentStore(database),
                        executor,
                        provider,
                        mode=AutoApprovalMode.OPEN_ONLY,
                    )
                    try:
                        with patch.object(
                            auto_bot._bot, "send_message", new=AsyncMock()
                        ):
                            await auto_service.recover_planning_candidates()
                            assert exchange.orders == {}
                            with sqlite3.connect(database) as db:
                                db.execute(
                                    "UPDATE intents SET status='PENDING' WHERE intent_id=?",
                                    (recovered_id,),
                                )
                            await auto_service.recover_planning_candidates()
                            submitted = dict(exchange.orders)
                            assert submitted
                            await auto_service.recover_planning_candidates()
                            assert exchange.orders == submitted
                            with sqlite3.connect(database) as db:
                                assert db.execute(
                                    "SELECT status FROM intents WHERE intent_id=?",
                                    (recovered_id,),
                                ).fetchone() == ("EXECUTED",)
                            assert provider.calls == 1
                    finally:
                        await auto_bot.close()
            finally:
                await restarted_bot.close()

    asyncio.run(run())
