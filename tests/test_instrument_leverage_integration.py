"""Real planning, SQLite provenance and Bybit HTTP; no exchange mutations."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, make_intent

from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionPolicy,
    InstrumentContext,
    TradeExecutionError,
)
from cautious_crypto_bro.execution import ExecutionPlanner, ExecutionPlanningError
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class LeverageLimitedExchange(Exchange):
    def __init__(self):
        super().__init__()
        self.mark = Decimal("105")
        self.selected_leverages = []
        self.leverage_filter = {
            "minLeverage": "1",
            "maxLeverage": "12.50",
            "leverageStep": "0.01",
        }

    def handle(self, request):
        if request.url.path == "/v5/position/set-leverage":
            body = json.loads(request.content)
            leverage = Decimal(body["buyLeverage"])
            assert body["buyLeverage"] == body["sellLeverage"]
            self.selected_leverages.append(leverage)
            assert (leverage - 1) % Decimal("0.01") == 0
            if leverage > Decimal("12.5"):
                return httpx.Response(
                    200, json={"retCode": 110013, "retMsg": "Cannot set leverage"}
                )
            return httpx.Response(200, json={"retCode": 0, "result": {}})
        response = super().handle(request)
        if request.url.path == "/v5/market/instruments-info":
            payload = response.json()
            instrument = payload["result"]["list"][0]
            if self.leverage_filter is None:
                instrument.pop("leverageFilter", None)
            else:
                instrument["leverageFilter"] = self.leverage_filter
            return httpx.Response(200, json=payload)
        return response


@pytest.mark.parametrize("entry_type", [EntryType.LIMIT, EntryType.MARKET])
@pytest.mark.parametrize(
    "requested,expected", [(50.0, "12.5"), (None, "10"), (2.509, "2.50")]
)
def test_supported_leverage_preserves_risk_and_original_request(
    tmp_path, entry_type, requested, expected
):
    async def run():
        exchange = LeverageLimitedExchange()
        intent = make_intent().model_copy(
            update={
                "leverage": requested,
                "entry": Entry(
                    type=entry_type,
                    price=100 if entry_type is EntryType.LIMIT else None,
                ),
            }
        )
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        planner = ExecutionPlanner()
        policy = ExecutionPolicy(trading_capital_usdt=Decimal("1000"))
        with executor_for(exchange) as executor:
            context = await executor.market_context(intent.symbol)
            baseline = planner.plan(
                intent,
                policy,
                InstrumentContext(
                    market_price=context.market_price,
                    tick_size=context.tick_size,
                    qty_step=context.qty_step,
                    min_qty=context.min_qty,
                    min_notional=context.min_notional,
                ),
            )
            plan = planner.plan(intent, policy, context)
            claim = await store.claim_source(intent.source, lease_seconds=300)
            assert claim is not None
            assert await store.create_signal_batch_and_complete_source(
                [(intent, plan)], (), claim
            )
            restored_intent = await store.get_intent(intent.intent_id)
            restored_plan = await store.get_execution_plan(intent.intent_id)
            assert restored_intent is not None and restored_plan is not None
            if entry_type is EntryType.MARKET:
                await executor.execute_market_primary(restored_plan)
            else:
                await executor.execute(restored_plan)
            assert exchange.selected_leverages == [Decimal(expected)]
            assert restored_intent.leverage == requested
            assert restored_plan.leverage == Decimal(expected)
            assert plan.model_dump(
                exclude={"leverage", "created_at"}
            ) == baseline.model_dump(exclude={"leverage", "created_at"})

    asyncio.run(run())


@pytest.mark.parametrize(
    "raw_filter",
    [
        None,
        {"minLeverage": "1", "maxLeverage": "NaN", "leverageStep": "0.01"},
        {"minLeverage": "1", "maxLeverage": "12.5", "leverageStep": "0"},
        {"minLeverage": "5", "maxLeverage": "2", "leverageStep": "0.01"},
        {"minLeverage": "1", "leverageStep": "0.01"},
    ],
)
def test_invalid_live_leverage_filter_prevents_planning(raw_filter):
    async def run():
        exchange = LeverageLimitedExchange()
        exchange.leverage_filter = raw_filter
        with executor_for(exchange) as executor:
            with pytest.raises(TradeExecutionError, match="instrument leverage filter"):
                await executor.market_context("BTCUSDT")
        assert exchange.selected_leverages == [] and exchange.orders == {}

    asyncio.run(run())


def test_request_below_instrument_minimum_is_rejected_without_increasing_leverage():
    async def run():
        exchange = LeverageLimitedExchange()
        exchange.leverage_filter = {
            "minLeverage": "5",
            "maxLeverage": "12.5",
            "leverageStep": "0.01",
        }
        with executor_for(exchange) as executor:
            context = await executor.market_context("BTCUSDT")
            intent = make_intent().model_copy(update={"leverage": 2.0})
            with pytest.raises(
                ExecutionPlanningError, match="below the instrument minimum"
            ):
                ExecutionPlanner().plan(
                    intent,
                    ExecutionPolicy(trading_capital_usdt=Decimal("1000")),
                    context,
                )
        assert exchange.selected_leverages == [] and exchange.orders == {}

    asyncio.run(run())
