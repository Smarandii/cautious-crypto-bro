"""Real extraction/service/SQLite/Telegram path; only external transports fake."""

import asyncio
import json
import runpy
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from test_filled_entry_direction_integration import candidate
from test_remaining_concerns import executor_for, make_intent, prepare
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.domain import (
    AutoApprovalMode,
    IncomingPost,
    IntentExtraction,
    IntentStatus,
    Side,
    SignalPositionContext,
    SourceOpenContext,
)
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.signals.normalization import _signals_from_extraction
from cautious_crypto_bro.state_ownership import state_ownership
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration
CAPTION = "My BTCUSDT limit entry just filled."


class ReviewModel:
    def __init__(self, caption=CAPTION, *, evidence=None, symbol="BTCUSDT", intents=()):
        self.payload = {
            "actionable": bool(intents),
            "reason": "Synthetic missing-direction review",
            "entry_reviews": [{"symbol": symbol, "evidence_text": evidence or caption}],
            "intents": list(intents),
        }

    async def complete(self, request):
        return LLMResponse(json.dumps(self.payload))


@pytest.mark.parametrize(
    "caption",
    [
        CAPTION,
        "Лимитка по BTCUSDT исполнилась.",
        "Лимитный ордер на вход по BTCUSDT только что исполнился.",
        "My BTCUSDT entry limit has filled now.",
        "Сейчас исполнилась лимитка на вход по BTCUSDT.",
    ],
)
def test_review_reaches_bot_without_plan_order_or_execute(tmp_path, caption):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        exchange = ExchangeTransport()
        source = make_intent().source.model_copy(
            update={"text": caption, "channel_username": "synthetic_review_fixture"}
        )
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, ReviewModel(caption), mode=AutoApprovalMode.ALL
            )
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    await service.on_message(IncomingPost(source=source))
                    await service.on_message(IncomingPost(source=source))
                    await service.delivery.recover_entry_reviews()
                assert sender.await_count == 1
                message = sender.call_args.kwargs
                assert "HUMAN REVIEW REQUIRED" in message["text"]
                assert "direction unavailable" in message["text"]
                assert source.telegram_url in message["text"]
                assert not message.get("reply_markup")
                assert not exchange.orders
                assert not any(
                    p
                    in {
                        "/v5/order/create",
                        "/v5/order/cancel",
                        "/v5/position/trading-stop",
                    }
                    for p, _ in exchange.requests
                )
                with closing(sqlite3.connect(path)) as db:
                    for table in (
                        "intents",
                        "position_actions",
                        "execution_plans",
                        "manual_deliveries",
                    ):
                        assert (
                            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                            == 0
                        )
                    payload, fingerprint, delivered = db.execute(
                        "SELECT payload_json,evaluation_fingerprint,delivered_at FROM entry_reviews"
                    ).fetchone()
                    decision = json.loads(
                        db.execute(
                            "SELECT payload_json FROM source_decisions"
                        ).fetchone()[0]
                    )
                    assert (
                        fingerprint == decision["evaluation_fingerprint"] and delivered
                    )
                    assert json.loads(payload)["source"]["text"] == caption
                    assert (
                        decision["normalized"]["entry_reviews"][0]["symbol"]
                        == "BTCUSDT"
                    )
                    assert (
                        db.execute("SELECT status FROM source_messages").fetchone()[0]
                        == "COMPLETED"
                    )
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "caption,evidence",
    [
        (
            "Yesterday my BTCUSDT limit entry just filled.",
            "my BTCUSDT limit entry just filled.",
        ),
        (
            "If my BTCUSDT limit entry just filled, I would celebrate.",
            "my BTCUSDT limit entry just filled",
        ),
        (
            "Imagine my BTCUSDT limit entry just filled.",
            "my BTCUSDT limit entry just filled.",
        ),
        (
            "My BTCUSDT limit entry has not filled.",
            "My BTCUSDT limit entry just filled.",
        ),
        (
            "Results: my BTCUSDT limit entry just filled.",
            "my BTCUSDT limit entry just filled.",
        ),
        ("My ETHUSDT limit entry just filled.", "My ETHUSDT limit entry just filled."),
        (
            "My BTCUSDT and ETHUSDT limit entry just filled.",
            "My BTCUSDT and ETHUSDT limit entry just filled.",
        ),
        (
            "My BTCUSDT SHORT limit entry just filled.",
            "My BTCUSDT SHORT limit entry just filled.",
        ),
        ("Лимитка по BTCUSDT не исполнилась.", "Лимитка по BTCUSDT не исполнилась."),
        ("A promotional BTCUSDT results card.", CAPTION),
    ],
)
def test_model_cannot_invent_review_evidence_or_override_exclusions(caption, evidence):
    source = make_intent().source.model_copy(update={"text": caption})
    raw = IntentExtraction.model_validate(
        ReviewModel(caption, evidence=evidence).payload
    )
    assert not _signals_from_extraction(source, raw).entry_reviews


def test_owned_update_and_unknown_context_are_distinct():
    source = make_intent().source.model_copy(update={"text": CAPTION})
    raw = IntentExtraction.model_validate(ReviewModel().payload)
    history = SourceOpenContext(
        symbol="BTCUSDT",
        side=Side.SHORT,
        status=IntentStatus.EXECUTED,
        message_id=1,
        created_at=datetime.now(UTC),
        active_copy=True,
    )
    context = SignalPositionContext(
        source_channel_id=source.channel_id,
        account_state_available=True,
        source_open_history=(history,),
    )
    assert not _signals_from_extraction(source, raw, context).entry_reviews
    unknown = _signals_from_extraction(source, raw).entry_reviews
    assert len(unknown) == 1 and unknown[0].ownership_unknown
    known = _signals_from_extraction(
        source, raw, context.model_copy(update={"source_open_history": ()})
    ).entry_reviews
    assert len(known) == 1 and not known[0].ownership_unknown


def test_review_and_valid_open_preserve_independent_symbols():
    source = make_intent().source.model_copy(
        update={"text": CAPTION + " I just opened ETHUSDT LONG at market."}
    )
    raw = IntentExtraction.model_validate(
        ReviewModel(intents=[candidate("ETHUSDT", side="LONG")]).payload
    )
    result = _signals_from_extraction(source, raw)
    assert [r.symbol for r in result.entry_reviews] == ["BTCUSDT"]
    assert [i.symbol for i in result.open_intents] == ["ETHUSDT"]
    same = IntentExtraction.model_validate(
        ReviewModel(intents=[candidate(side="SHORT")]).payload
    )
    assert not _signals_from_extraction(source, same).entry_reviews


def test_failed_send_restart_competing_consumers_and_stale_ack(tmp_path):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        exchange = ExchangeTransport()
        source = make_intent().source.model_copy(update={"text": CAPTION})
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, ReviewModel(), mode=AutoApprovalMode.ALL
            )
            try:
                with patch.object(
                    bot._bot,
                    "send_message",
                    new=AsyncMock(side_effect=OSError("offline")),
                ):
                    await service.on_message(IncomingPost(source=source))
                with closing(sqlite3.connect(path)) as db:
                    original = db.execute(
                        "SELECT payload_json FROM entry_reviews"
                    ).fetchone()[0]
                    assert (
                        db.execute("SELECT delivered_at FROM entry_reviews").fetchone()[
                            0
                        ]
                        is None
                    )
                    db.execute("UPDATE entry_reviews SET retry_after=NULL")
                    db.commit()
                restarted = IntentStore(path)
                await restarted.initialize()
                second, second_bot, _ = workflow(restarted, executor, ReviewModel())
                try:
                    sender = AsyncMock()
                    with (
                        patch.object(bot._bot, "send_message", new=sender),
                        patch.object(second_bot._bot, "send_message", new=sender),
                    ):
                        await asyncio.gather(
                            service.delivery.recover_entry_reviews(),
                            second.delivery.recover_entry_reviews(),
                        )
                        await second.delivery.recover_entry_reviews()
                    assert sender.await_count == 1 and not exchange.orders
                    with closing(sqlite3.connect(path)) as db:
                        assert (
                            db.execute(
                                "SELECT payload_json FROM entry_reviews"
                            ).fetchone()[0]
                            == original
                        )
                        assert db.execute(
                            "SELECT delivered_at FROM entry_reviews"
                        ).fetchone()[0]
                finally:
                    await second_bot.close()
            finally:
                await bot.close()

    asyncio.run(run())


def test_stale_source_claim_cannot_reserve_review(tmp_path):
    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        source = make_intent().source.model_copy(update={"text": CAPTION})
        raw = IntentExtraction.model_validate(ReviewModel().payload)
        reviews = _signals_from_extraction(source, raw).entry_reviews
        token = await store.claim_source(source, lease_seconds=300)
        assert not await store.save_entry_reviews(
            source, "wrong-token", reviews, "synthetic-fingerprint"
        )
        assert not await store.pending_entry_reviews()
        assert await store.save_entry_reviews(
            source, token, reviews, "synthetic-fingerprint"
        )
        assert len(await store.pending_entry_reviews()) == 1

    asyncio.run(run())


def test_review_persistence_failure_retries_source_without_losing_notice(tmp_path):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        exchange = ExchangeTransport()
        source = make_intent().source.model_copy(update={"text": CAPTION})
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, ReviewModel())
            try:
                sender = AsyncMock()
                with patch.object(bot._bot, "send_message", new=sender):
                    with patch.object(
                        store,
                        "save_entry_reviews",
                        new=AsyncMock(side_effect=OSError("synthetic disk failure")),
                    ):
                        await service.on_message(IncomingPost(source=source))
                    with closing(sqlite3.connect(path)) as db:
                        assert db.execute(
                            "SELECT status FROM source_messages"
                        ).fetchone() == ("FAILED",)
                        assert db.execute(
                            "SELECT count(*) FROM entry_reviews"
                        ).fetchone() == (0,)
                    assert sender.await_count == 0
                    await service.on_message(IncomingPost(source=source))
                assert sender.await_count == 1 and not exchange.orders
                with closing(sqlite3.connect(path)) as db:
                    assert db.execute(
                        "SELECT status FROM source_messages"
                    ).fetchone() == ("COMPLETED",)
                    assert db.execute(
                        "SELECT count(*) FROM entry_reviews"
                    ).fetchone() == (1,)
            finally:
                await bot.close()

    asyncio.run(run())


def test_abandoned_delivery_lease_recovery_and_stale_ack(tmp_path):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        source = make_intent().source.model_copy(update={"text": CAPTION})
        review = _signals_from_extraction(
            source, IntentExtraction.model_validate(ReviewModel().payload)
        ).entry_reviews[0]
        claim = await store.claim_source(source, lease_seconds=300)
        await store.save_entry_reviews(
            source, claim, (review,), "synthetic-fingerprint"
        )
        old = await store.claim_entry_review(review)
        assert old and not await store.claim_entry_review(review)
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "UPDATE entry_reviews SET retry_after=datetime('now','-1 second')"
            )
            db.commit()
        restarted = IntentStore(path)
        new = await restarted.claim_entry_review(review)
        assert new and new != old
        await store.finish_entry_review(review, old, delivered=True)
        with closing(sqlite3.connect(path)) as db:
            assert db.execute(
                "SELECT claim_token,delivered_at FROM entry_reviews"
            ).fetchone() == (new, None)
        await restarted.finish_entry_review(review, new, delivered=True)
        assert not await restarted.pending_entry_reviews()

    asyncio.run(run())


def test_schema16_upgrade_and_offline_rollback_preserve_current_state(tmp_path):
    rollback = runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "scripts/rollback_entry_review_schema.py"
        )
    )["rollback"]

    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP TABLE entry_reviews")
            db.execute("PRAGMA user_version=16")
            db.commit()
        await store.initialize()
        # Include real persisted trade ownership/plan rows, not only an empty DB.
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, _, _, _ = await prepare(path, exchange, executor)
        source = make_intent().source.model_copy(
            update={"text": CAPTION, "message_id": 999}
        )
        claim = await store.claim_source(source, lease_seconds=300)
        review = _signals_from_extraction(
            source, IntentExtraction.model_validate(ReviewModel().payload)
        ).entry_reviews
        await store.save_entry_reviews(source, claim, review, "synthetic-fingerprint")
        with closing(sqlite3.connect(path)) as db:
            tables = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            before = {t: db.execute("SELECT * FROM " + t).fetchall() for t in tables}
        backup = tmp_path / "current.backup"
        with state_ownership(path), pytest.raises(RuntimeError, match="already owned"):
            rollback(path, backup)
        assert not backup.exists()
        rollback(path, backup)
        with (
            closing(sqlite3.connect(path)) as db,
            closing(sqlite3.connect(backup)) as saved,
        ):
            assert db.execute("PRAGMA user_version").fetchone() == (16,)
            assert saved.execute("PRAGMA user_version").fetchone() == (17,)
            assert {
                t: db.execute("SELECT * FROM " + t).fetchall() for t in tables
            } == before
            assert {
                t: saved.execute("SELECT * FROM " + t).fetchall() for t in tables
            } == before
        await store.initialize()
        assert len(await store.pending_entry_reviews()) == 1
        assert await store.get_intent(intent.intent_id) is not None
        assert await store.get_execution_plan(intent.intent_id) is not None
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (17,)
            assert {
                t: db.execute("SELECT * FROM " + t).fetchall() for t in tables
            } == before
        with pytest.raises(ValueError, match="already exists"):
            rollback(path, backup)

    asyncio.run(run())
