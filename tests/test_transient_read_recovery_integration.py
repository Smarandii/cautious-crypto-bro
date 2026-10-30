"""Temporary exchange read responses must not discard actionable sources."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from test_remaining_concerns import executor_for, make_intent
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.domain import (
    ApprovalMode,
    IncomingPost,
    IntentStatus,
    StrategyStatus,
    TradeExecutionError,
)
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.source_coverage import coverage_summary
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class OpenModel:
    calls = 0

    async def complete(self, request):
        self.calls += 1
        return LLMResponse(
            json.dumps(
                {
                    "actionable": True,
                    "reason": "Explicit entry",
                    "intents": [
                        {
                            "symbol": "BTCUSDT",
                            "side": "LONG",
                            "entry": "MARKET",
                            "stop_loss": 90,
                            "relation": "NEW",
                            "summary": "Enter now",
                            "confidence": 1,
                        }
                    ],
                }
            )
        )

    async def close(self):
        pass


class ReadErrorExchange(ExchangeTransport):
    unavailable = True

    def __init__(self, endpoint, code):
        super().__init__()
        self.endpoint, self.code = endpoint, code
        self.failed_reads = 0

    def handle(self, request):
        if (
            self.unavailable
            and request.method == "GET"
            and request.url.path == self.endpoint
        ):
            self.failed_reads += 1
            return httpx.Response(
                200, json={"retCode": self.code, "retMsg": "Synthetic temporary read"}
            )
        return super().handle(request)


@pytest.mark.parametrize("code", [10000, 10006, 10016, 429])
@pytest.mark.parametrize(
    "endpoint",
    [
        "/v5/market/instruments-info",
        "/v5/market/tickers",
        "/v5/account/wallet-balance",
    ],
)
def test_temporary_read_response_retains_original_candidate_and_recovers_once(
    tmp_path, endpoint, code
):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        source = make_intent().source
        model = OpenModel()
        exchange = ReadErrorExchange(endpoint, code)
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, model)
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await service.on_message(IncomingPost(source=source))
                assert exchange.orders == {}
                with closing(sqlite3.connect(database)) as db:
                    rows = db.execute(
                        "SELECT intent_id,deadline,status FROM planning_candidates"
                    ).fetchall()
                    assert len(rows) == 1
                    original_id, original_deadline, status = rows[0]
                    assert status == "PENDING"
                    payload = json.loads(
                        db.execute(
                            "SELECT payload_json FROM planning_candidates"
                        ).fetchone()[0]
                    )
                    original_mode = (
                        ApprovalMode.MANUAL
                        if endpoint == "/v5/account/wallet-balance"
                        else ApprovalMode.AUTO
                    )
                    assert payload["approval_mode"] == original_mode
                    assert db.execute("SELECT count(*) FROM intents").fetchone() == (0,)
                    assert db.execute(
                        "SELECT status FROM source_messages"
                    ).fetchone() == ("COMPLETED",)
                summary = await coverage_summary(
                    database, since=datetime.now(UTC) - timedelta(days=1)
                )
                assert summary["dispositions"] == {"UNRESOLVED": 1}
                assert summary["planning_candidate_statuses"] == {"PENDING": 1}
            finally:
                await bot.close()

            exchange.unavailable = False
            restarted_store = IntentStore(database)
            restarted, restarted_bot, _ = workflow(restarted_store, executor, model)
            try:
                with closing(sqlite3.connect(database)) as db:
                    db.execute(
                        "UPDATE planning_candidates SET retry_after=datetime('now','-1 second')"
                    )
                    db.commit()
                with patch.object(restarted_bot._bot, "send_message", new=AsyncMock()):
                    await restarted.recover_planning_candidates()
                    if original_mode is ApprovalMode.MANUAL:
                        assert exchange.orders == {}
                        with closing(sqlite3.connect(database)) as db:
                            assert db.execute(
                                "SELECT approval_mode,status FROM intents"
                            ).fetchone() == ("MANUAL", "PENDING")
                        await restarted._coordinator.execute_intent(
                            UUID(original_id),
                            approval_mode=ApprovalMode.MANUAL,
                            user_id=1,
                        )
                    submitted = dict(exchange.orders)
                    assert len(submitted) == 3
                    await restarted.recover_planning_candidates()
                    await restarted.on_message(IncomingPost(source=source))
                assert exchange.orders == submitted
                assert model.calls == 1
                summary = await coverage_summary(
                    database, since=datetime.now(UTC) - timedelta(days=1)
                )
                assert summary["dispositions"] == {"EXECUTED": 1}
                with closing(sqlite3.connect(database)) as db:
                    assert db.execute(
                        "SELECT intent_id,deadline,status FROM planning_candidates"
                    ).fetchone() == (original_id, original_deadline, "TRANSFERRED")
                    assert db.execute(
                        "SELECT intent_id,status FROM intents"
                    ).fetchone() == (
                        original_id,
                        "EXECUTED",
                    )
            finally:
                await restarted_bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_persistent_read_failure_obeys_backoff_and_original_deadline(tmp_path, expired):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        model = OpenModel()
        exchange = ReadErrorExchange("/v5/market/instruments-info", 10006)
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, model)
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await service.on_message(IncomingPost(source=make_intent().source))
                    initial_reads = exchange.failed_reads
                    with closing(sqlite3.connect(database)) as db:
                        original_deadline = db.execute(
                            "SELECT deadline FROM planning_candidates"
                        ).fetchone()[0]
                        db.execute(
                            "UPDATE planning_candidates SET retry_after=datetime('now','-1 second')"
                        )
                        if expired:
                            db.execute(
                                "UPDATE planning_candidates SET deadline=?",
                                (
                                    (
                                        datetime.now(UTC) - timedelta(seconds=1)
                                    ).isoformat(),
                                ),
                            )
                        db.commit()
                    await service.recover_planning_candidates()
                    after_first_recovery = exchange.failed_reads
                    await service.recover_planning_candidates()
                assert exchange.failed_reads == after_first_recovery
                assert after_first_recovery == initial_reads + int(not expired)
                assert model.calls == 1 and exchange.orders == {}
                with closing(sqlite3.connect(database)) as db:
                    status, deadline, attempts = db.execute(
                        "SELECT status,deadline,attempt_count FROM planning_candidates"
                    ).fetchone()
                    assert status == ("EXPIRED" if expired else "PENDING")
                    assert attempts == 1
                    if not expired:
                        assert deadline == original_deadline
                    assert db.execute("SELECT count(*) FROM intents").fetchone() == (0,)
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("code", [10001, 10005, 10010, 99999, None, True, "invalid"])
def test_permanent_and_malformed_read_responses_have_explicit_dispositions(
    tmp_path, code
):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        exchange = ReadErrorExchange("/v5/market/instruments-info", code)
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, OpenModel())
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await service.on_message(IncomingPost(source=make_intent().source))
                with closing(sqlite3.connect(database)) as db:
                    decision = json.loads(
                        db.execute(
                            "SELECT payload_json FROM source_decisions"
                        ).fetchone()[0]
                    )["planning"]
                    malformed = (
                        code is None or isinstance(code, bool) or code == "invalid"
                    )
                    assert bool(decision["retryable_errors"]) is malformed
                    assert bool(decision["rejections"]) is not malformed
                    assert db.execute(
                        "SELECT count(*) FROM planning_candidates"
                    ).fetchone() == (int(malformed),)
                    assert db.execute(
                        "SELECT status FROM source_messages"
                    ).fetchone() == ("COMPLETED",)
                assert exchange.orders == {}
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("code", [10000, 10006, 10016, 429])
def test_temporary_mutation_response_after_acceptance_is_never_replayed(tmp_path, code):
    class AcceptedErrorExchange(ExchangeTransport):
        submissions = 0

        def handle(self, request):
            response = super().handle(request)
            if request.method == "POST" and request.url.path == "/v5/order/create":
                self.submissions += 1
                return httpx.Response(
                    200,
                    json={
                        "retCode": code,
                        "retMsg": "Synthetic response after acceptance",
                    },
                )
            return response

    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        exchange = AcceptedErrorExchange()
        model = OpenModel()
        source = make_intent().source
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, model)
            observed_errors = []
            original_post = executor._client.private_post

            def observe_post(path, payload):
                try:
                    return original_post(path, payload)
                except TradeExecutionError as error:
                    observed_errors.append(
                        (
                            error.exchange_code,
                            error.retryable_read,
                            error.is_permanent_read_rejection,
                        )
                    )
                    raise

            try:
                with (
                    patch.object(bot._bot, "send_message", new=AsyncMock()),
                    patch.object(executor._client, "private_post", new=observe_post),
                ):
                    await service.on_message(IncomingPost(source=source))
            finally:
                await bot.close()
            assert observed_errors == [(code, None, False)]
            with closing(sqlite3.connect(database)) as db:
                assert db.execute("SELECT status FROM intents").fetchone() == (
                    IntentStatus.FAILED,
                )
                assert db.execute(
                    "SELECT status FROM position_strategies"
                ).fetchone() == (StrategyStatus.UNCERTAIN,)
                assert db.execute(
                    "SELECT count(*) FROM planning_candidates"
                ).fetchone() == (0,)
            restarted, restarted_bot, _ = workflow(
                IntentStore(database), executor, model
            )
            try:
                with patch.object(restarted_bot._bot, "send_message", new=AsyncMock()):
                    await restarted.recover_auto_execution()
                    await restarted.recover_planning_candidates()
                    await restarted.on_message(IncomingPost(source=source))
                assert exchange.submissions == 1 and len(exchange.orders) == 1
                assert model.calls == 1
            finally:
                await restarted_bot.close()

    asyncio.run(run())
