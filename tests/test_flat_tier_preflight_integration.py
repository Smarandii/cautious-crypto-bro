"""Flat Bybit rows have no position status/mark; public marks must be explicit."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from test_tier_preflight_integration import TierExchange, run_entry

from cautious_crypto_bro.domain import EntryType, IntentStatus

pytestmark = pytest.mark.integration


class FlatTierExchange(TierExchange):
    flat_status = ""
    ticker_fault = None

    def handle(self, request):
        response = super().handle(request)
        body = response.json()
        if request.url.path == "/v5/position/list" and request.url.params.get("symbol"):
            row = body["result"]["list"][0]
            row["positionStatus"] = self.flat_status
            row["markPrice"] = ""
        elif request.url.path == "/v5/market/tickers":
            row = body["result"]["list"][0]
            row.update(symbol="BTCUSDT", markPrice=str(self.mark))
            if self.ticker_fault == "missing":
                row.pop("markPrice")
            elif self.ticker_fault == "symbol":
                row["symbol"] = "ETHUSDT"
            elif self.ticker_fault == "duplicate":
                body["result"]["list"].append(dict(row))
            elif self.ticker_fault is not None:
                row["markPrice"] = self.ticker_fault
        return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    "entry_type", [EntryType.MARKET, EntryType.LIMIT, EntryType.RANGE]
)
def test_empty_flat_position_fields_use_public_mark_before_entry(tmp_path, entry_type):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = FlatTierExchange(database)
        _, _, _, outcome = await run_entry(database, exchange, entry_type)
        assert outcome.status is IntentStatus.EXECUTED
        assert exchange.persisted_at_submission == [Decimal("5")]
        assert exchange.selected_leverages == [Decimal("5")]

    asyncio.run(run())


@pytest.mark.parametrize("status", [None, "Liq", "Adl", "Unknown"])
def test_missing_or_non_normal_flat_status_does_not_authorize_entry(tmp_path, status):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = FlatTierExchange(database)
        exchange.flat_status = status
        _, _, _, outcome = await run_entry(database, exchange)
        assert outcome.status is IntentStatus.FAILED
        assert exchange.selected_leverages == [] and exchange.orders == {}

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    ["missing", "", "0", "-1", "NaN", "Infinity", "invalid", "symbol", "duplicate"],
)
def test_invalid_public_mark_never_falls_back_to_last_price(tmp_path, fault):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = FlatTierExchange(database)
        exchange.flat_status = "Normal"
        exchange.ticker_fault = fault
        _, _, _, outcome = await run_entry(database, exchange)
        assert outcome.status is IntentStatus.FAILED
        assert exchange.selected_leverages == [] and exchange.orders == {}

    asyncio.run(run())
