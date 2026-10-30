"""Venue-style symbols resolve before planning without rewriting source evidence."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from test_remaining_concerns import executor_for, make_intent
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.contract_symbols import resolve_open_contract
from cautious_crypto_bro.domain import (
    AutoApprovalMode,
    Entry,
    EntryType,
    IncomingPost,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
    SignalExtraction,
    SignalPositionContext,
    SourceOpenContext,
    TradeExecutionError,
)
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class ContractExchange(ExchangeTransport):
    def __init__(self, side="LONG"):
        super().__init__()
        self.side = side

    def handle(self, request):
        symbol = request.url.params.get("symbol")
        if request.url.path == "/v5/market/instruments-info":
            if symbol == "PEPEUSDT":
                return httpx.Response(
                    200,
                    json={"retCode": 10001, "retMsg": "params error: symbol invalid"},
                )
            if symbol == "1000PEPEUSDT":
                return httpx.Response(
                    200,
                    json={
                        "retCode": 0,
                        "result": {
                            "list": [
                                {
                                    "symbol": symbol,
                                    "status": "Trading",
                                    "baseCoin": "1000PEPE",
                                    "quoteCoin": "USDT",
                                    "lotSizeFilter": {
                                        "qtyStep": "100",
                                        "minOrderQty": "100",
                                        "minNotionalValue": "5",
                                    },
                                    "priceFilter": {"tickSize": "0.000001"},
                                }
                            ]
                        },
                    },
                )
        if request.url.path == "/v5/market/tickers" and symbol == "1000PEPEUSDT":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": symbol,
                                "lastPrice": "0.0052"
                                if self.side == "LONG"
                                else "0.0048",
                            }
                        ]
                    },
                },
            )
        return super().handle(request)


def test_post_resolution_outage_retains_raw_identity_for_restart(tmp_path):
    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        intent = make_intent().model_copy(
            update={
                "symbol": "PEPEUSDT",
                "entry": Entry(type=EntryType.LIMIT, price=0.005),
                "stop_loss": 0.0045,
            }
        )
        with executor_for(ContractExchange()) as executor:
            service, bot, _ = workflow(store, executor, ContractModel())
            try:
                snapshot = await service._context_provider.snapshot(
                    intent.source.channel_id
                )
                with patch.object(
                    service._planning,
                    "_capital_frozen_policy",
                    new=AsyncMock(side_effect=RuntimeError("synthetic policy outage")),
                ):
                    planning = await service._planning.plan_open_intents(
                        SignalExtraction(
                            open_intents=(
                                intent,
                                type(intent).model_validate(
                                    {
                                        **intent.model_dump(),
                                        "intent_id": str(uuid4()),
                                        "symbol": "1000PEPEUSDT",
                                    }
                                ),
                            )
                        ),
                        snapshot,
                        original_duplicate=False,
                    )
                planned, errors, candidates = (
                    planning.planned,
                    planning.errors,
                    planning.retry_candidates,
                )
                assert not planned and errors
                saved, duplicate, deadline = candidates[0]
                assert (
                    saved.symbol == "PEPEUSDT" and saved.intent_id == intent.intent_id
                )
                assert duplicate
                assert len(candidates) == 2 and candidates[1][1]
                assert deadline > intent.source.published_at
                restored = type(intent).model_validate_json(saved.model_dump_json())
                with patch.object(
                    executor, "market_context", wraps=executor.market_context
                ) as reads:
                    await resolve_open_contract(restored, executor.market_context)
                assert [call.args[0] for call in reads.await_args_list] == [
                    "PEPEUSDT",
                    "1000PEPEUSDT",
                ]
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "action_type",
    [
        PositionActionType.CLOSE,
        PositionActionType.CANCEL_ENTRIES,
        PositionActionType.REDUCE,
    ],
)
@pytest.mark.parametrize(
    "history_symbols,foreign,expected",
    [
        (["1000PEPEUSDT"], False, "1000PEPEUSDT"),
        (["1000PEPEUSDT"], True, "PEPEUSDT"),
        ([], False, "PEPEUSDT"),
        (["PEPEUSDT", "1000PEPEUSDT"], False, "PEPEUSDT"),
    ],
)
def test_lifecycle_alias_is_bound_to_its_source_history(
    tmp_path, action_type, history_symbols, foreign, expected
):
    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        source = make_intent().source
        action = PositionActionIntent(
            source=source,
            symbol="PEPEUSDT",
            action=action_type,
            close_pct=50 if action_type is PositionActionType.REDUCE else None,
            summary="Synthetic explicit action",
            confidence=1,
        )
        history = tuple(
            SourceOpenContext(
                symbol=symbol,
                side=Side.LONG,
                status=IntentStatus.EXECUTED,
                message_id=1,
                created_at=source.published_at,
                active_copy=True,
            )
            for symbol in history_symbols
        )
        context = SignalPositionContext(
            source_channel_id=source.channel_id + int(foreign),
            account_state_available=False,
            source_open_history=history,
        )
        with executor_for(ContractExchange()) as executor:
            service, bot, _ = workflow(store, executor, ContractModel())
            try:
                resolved = service._planning.plan_position_actions(
                    SignalExtraction(position_actions=(action,)),
                    account_state=None,
                    position_context=context,
                )[0]
                assert resolved.symbol == expected
                assert resolved.action_id == action.action_id
                assert action.symbol == "PEPEUSDT"
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("recovery", ["available", "units_changed", "expired"])
def test_partial_alias_batch_survives_sqlite_restart_without_auto_duplicate(
    tmp_path, recovery
):
    class BatchModel(ContractModel):
        async def complete(self, request):
            result = json.loads((await super().complete(request)).content)
            result["intents"].append({**result["intents"][0], "symbol": "1000PEPEUSDT"})
            return LLMResponse(json.dumps(result))

    class OutageExchange(ContractExchange):
        unavailable = True
        changed_units = False

        def handle(self, request):
            if request.url.params.get("symbol") == "1000PEPEUSDT":
                if (
                    request.url.path == "/v5/market/instruments-info"
                    and self.unavailable
                ):
                    self.unavailable = False
                    raise httpx.ConnectError(
                        "Synthetic single lookup outage", request=request
                    )
                if request.url.path == "/v5/market/tickers" and self.changed_units:
                    return httpx.Response(
                        200,
                        json={"retCode": 0, "result": {"list": [{"lastPrice": "5.2"}]}},
                    )
            return super().handle(request)

    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        source = make_intent().source.model_copy(
            update={"text": "Synthetic PEPE proposals"}
        )
        exchange = OutageExchange()
        provider = BatchModel()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, provider)
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await service.on_message(IncomingPost(source=source))
            finally:
                await bot.close()
            with closing(sqlite3.connect(database)) as db:
                candidate = db.execute(
                    "SELECT intent_id,payload_json,deadline,duplicate_in_batch FROM planning_candidates"
                ).fetchone()
                assert candidate is not None
                assert json.loads(candidate[1])["symbol"] == "PEPEUSDT"
                assert candidate[3] == 1
                assert db.execute("SELECT approval_mode FROM intents").fetchall() == [
                    ("MANUAL",)
                ]
                db.execute(
                    "UPDATE planning_candidates SET retry_after=datetime('now','-1 second')"
                )
                if recovery == "expired":
                    db.execute(
                        "UPDATE planning_candidates SET deadline=?",
                        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
                    )
                db.commit()
            exchange.changed_units = recovery == "units_changed"
            restarted_store = IntentStore(database)
            await restarted_store.initialize()
            restarted, restarted_bot, _ = workflow(restarted_store, executor, provider)
            try:
                with patch.object(restarted_bot._bot, "send_message", new=AsyncMock()):
                    await restarted.recover_planning_candidates()
            finally:
                await restarted_bot.close()
            with closing(sqlite3.connect(database)) as db:
                record = db.execute(
                    "SELECT intent_id,status,deadline,last_error FROM planning_candidates"
                ).fetchone()
                assert record[0] == candidate[0]
                assert (
                    record[1]
                    == {
                        "available": "TRANSFERRED",
                        "units_changed": "REJECTED",
                        "expired": "EXPIRED",
                    }[recovery]
                )
                if recovery != "expired":
                    assert record[2] == candidate[2]
                if recovery == "units_changed":
                    assert "Ambiguous alias quote units" in record[3]
                approvals = db.execute("SELECT approval_mode FROM intents").fetchall()
                assert approvals and all(row == ("MANUAL",) for row in approvals)
            assert not exchange.orders

    asyncio.run(run())


class ContractModel:
    def __init__(self, side="LONG", scale=1):
        self.side, self.scale = side, scale

    async def complete(self, request):
        return LLMResponse(
            json.dumps(
                {
                    "actionable": True,
                    "reason": "Synthetic explicit limit",
                    "intents": [
                        {
                            "symbol": "PEPEUSDT",
                            "side": self.side,
                            "entry": "LIMIT",
                            "price": 0.005 * self.scale,
                            "stop_loss": (0.0045 if self.side == "LONG" else 0.0055)
                            * self.scale,
                            "relation": "NEW",
                            "relation_evidence": "Synthetic explicit new order",
                            "summary": "New order",
                            "confidence": 1,
                        }
                    ],
                }
            )
        )

    async def close(self):
        pass


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("scale", [1, 0.001, 1000])
def test_contract_scale_alias_reaches_real_plan_and_preserves_raw_decision(
    tmp_path, side, scale
):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        source = make_intent().source.model_copy(
            update={"text": "Synthetic PEPE limit proposal"}
        )
        with executor_for(ContractExchange(side)) as executor:
            service, bot, _ = workflow(
                store,
                executor,
                ContractModel(side, scale),
                mode=AutoApprovalMode.DISABLED,
            )
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await service.on_message(IncomingPost(source=source))
            finally:
                await bot.close()
        with closing(sqlite3.connect(database)) as db:
            row = db.execute("SELECT payload_json FROM execution_plans").fetchone()
            if scale != 1:
                assert row is None, "Uncertain quote units must never reach planning"
                decision = json.loads(
                    db.execute("SELECT payload_json FROM source_decisions").fetchone()[
                        0
                    ]
                )
                assert decision["planning"]["retryable_errors"] == []
                assert "Ambiguous alias quote units" in str(
                    decision["planning"]["rejections"]
                )
                return
            assert row is not None, "Valid canonical contract must reach planning"
            plan = json.loads(row[0])
            assert plan["symbol"] == "1000PEPEUSDT"
            intent = json.loads(
                db.execute("SELECT payload_json FROM intents").fetchone()[0]
            )
            assert intent["symbol"] == "1000PEPEUSDT"
            assert intent["entry"]["price"] == 0.005
            assert intent["stop_loss"] == (0.0045 if side == "LONG" else 0.0055)
            decision = json.loads(
                db.execute("SELECT payload_json FROM source_decisions").fetchone()[0]
            )
            assert decision["extraction"]["intents"][0]["symbol"] == "PEPEUSDT"

    asyncio.run(run())


@pytest.mark.parametrize(
    "entry",
    [
        Entry(type=EntryType.MARKET),
        Entry(type=EntryType.RANGE, range_low=0.0049, range_high=0.005),
    ],
)
def test_reviewed_alias_preserves_market_and_range_price_semantics(entry):
    async def run():
        intent = make_intent().model_copy(
            update={
                "symbol": "PEPEUSDT",
                "entry": entry,
                "stop_loss": 0.0045,
                "take_profit": 0.006,
            }
        )
        with executor_for(ContractExchange()) as executor:
            resolved, context = await resolve_open_contract(
                intent, executor.market_context
            )
        assert resolved.symbol == "1000PEPEUSDT" and context is not None
        assert resolved.entry == entry
        assert (
            resolved.stop_loss == intent.stop_loss
            and resolved.take_profit == intent.take_profit
        )
        assert (
            resolved.intent_id == intent.intent_id and resolved.source == intent.source
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", [10010, 10003, "transport", "invalid-target", "exact"]
)
def test_alias_fallback_respects_exchange_errors_and_exact_contract(failure):
    class Exchange(ContractExchange):
        def __init__(self):
            super().__init__()
            self.lookups = []

        def handle(self, request):
            symbol = request.url.params.get("symbol")
            if request.url.path == "/v5/market/instruments-info":
                self.lookups.append(symbol)
                if symbol == "PEPEUSDT":
                    if failure == "transport":
                        raise httpx.ConnectError(
                            "synthetic unavailable", request=request
                        )
                    if isinstance(failure, int):
                        return httpx.Response(
                            200,
                            json={"retCode": failure, "retMsg": "synthetic rejection"},
                        )
                    if failure == "exact":
                        # A real exact contract takes precedence over the reviewed alias.
                        response = super().handle(
                            httpx.Request(
                                "GET",
                                "https://example.com/v5/market/instruments-info?symbol=1000PEPEUSDT",
                            )
                        )
                        body = response.json()
                        body["result"]["list"][0]["symbol"] = "PEPEUSDT"
                        return httpx.Response(200, json=body)
                if symbol == "1000PEPEUSDT" and failure == "invalid-target":
                    return httpx.Response(
                        200, json={"retCode": 10001, "retMsg": "symbol invalid"}
                    )
            if (
                request.url.path == "/v5/market/tickers"
                and symbol == "PEPEUSDT"
                and failure == "exact"
            ):
                return httpx.Response(
                    200,
                    json={"retCode": 0, "result": {"list": [{"lastPrice": "0.0052"}]}},
                )
            return super().handle(request)

    async def run():
        exchange = Exchange()
        intent = make_intent().model_copy(
            update={
                "symbol": "PEPEUSDT",
                "entry": Entry(type=EntryType.LIMIT, price=0.005),
                "stop_loss": 0.0045,
            }
        )
        with executor_for(exchange) as executor:
            if failure == "exact":
                resolved, _ = await resolve_open_contract(
                    intent, executor.market_context
                )
                assert resolved.symbol == "PEPEUSDT"
            else:
                with pytest.raises((TradeExecutionError, httpx.ConnectError)):
                    await resolve_open_contract(intent, executor.market_context)
        assert exchange.lookups == (
            ["PEPEUSDT", "1000PEPEUSDT"]
            if failure == "invalid-target"
            else ["PEPEUSDT"]
        )
        assert not exchange.orders

    asyncio.run(run())
