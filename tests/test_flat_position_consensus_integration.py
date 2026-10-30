"""Every symbol row must authorize flat one-way entry, not just the first row."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from test_flat_tier_preflight_integration import FlatTierExchange
from test_tier_preflight_integration import run_entry

from cautious_crypto_bro.domain import EntryType, IntentStatus

pytestmark = pytest.mark.integration


class DuplicateFlatExchange(FlatTierExchange):
    fault = None
    bad_row = 1

    def handle(self, request):
        response = super().handle(request)
        if (
            request.url.path != "/v5/position/list"
            or not request.url.params.get("symbol")
            or self.size > 0
        ):
            return response
        body = response.json()
        original = body["result"]["list"][0]
        original.update(side="", positionStatus="", avgPrice="", markPrice="")
        rows = [
            original,
            original | {"positionStatus": "Normal", "avgPrice": "100", "seq": "7"},
        ]
        if self.fault == "empty":
            rows = []
        elif self.fault is not None:
            field, value = self.fault
            if value == "missing":
                rows[self.bad_row].pop(field, None)
            else:
                rows[self.bad_row][field] = value
        body["result"]["list"] = rows
        return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    "entry_type", [EntryType.LIMIT, EntryType.MARKET, EntryType.RANGE]
)
def test_consistent_flat_rows_allow_entry_without_changing_risk(tmp_path, entry_type):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = DuplicateFlatExchange(database)
        store, intent, original, outcome = await run_entry(
            database, exchange, entry_type
        )
        assert outcome.status is IntentStatus.EXECUTED
        assert exchange.selected_leverages == [Decimal("5")]
        saved = await store.get_execution_plan(intent.intent_id)
        assert saved is not None
        # MARKET legitimately rebases after its fill; the pre-submit durable plan
        # must still preserve the original quantities/stops and only lower leverage.
        submitted = exchange.pre_submission_plans[0]
        assert all(s["index"] == 0 for s in submitted.pop("entry_slices"))
        expected = original.model_dump(mode="json")
        for key in ("leverage", "created_at"):
            submitted.pop(key)
            expected.pop(key)
        assert submitted == expected

    asyncio.run(run())


@pytest.mark.parametrize("bad_row", [0, 1])
@pytest.mark.parametrize(
    "fault",
    [
        ("symbol", "ETHUSDT"),
        ("symbol", "missing"),
        ("positionIdx", 1),
        ("positionIdx", 2),
        ("positionIdx", True),
        ("positionIdx", "missing"),
        ("size", "1"),
        ("size", "-1"),
        ("size", "NaN"),
        ("size", "missing"),
        ("side", "Buy"),
        ("side", "Sell"),
        ("side", "missing"),
        ("positionStatus", "Liq"),
        ("positionStatus", "Adl"),
        ("positionStatus", "missing"),
        ("riskId", 2),
        ("riskId", 0),
        ("riskId", True),
        ("riskId", "missing"),
    ],
)
def test_every_flat_row_must_agree_before_any_mutation(tmp_path, fault, bad_row):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = DuplicateFlatExchange(database)
        exchange.fault, exchange.bad_row = fault, bad_row
        store, _, _, outcome = await run_entry(database, exchange)
        assert outcome.status is IntentStatus.FAILED
        assert exchange.selected_leverages == [] and exchange.orders == {}
        assert await store.get_active_position_strategies() == ()

    asyncio.run(run())


def test_empty_symbol_rows_do_not_imply_flat_one_way_mode(tmp_path):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = DuplicateFlatExchange(database)
        exchange.fault = "empty"
        _, _, _, outcome = await run_entry(database, exchange)
        assert outcome.status is IntentStatus.FAILED
        assert exchange.selected_leverages == [] and exchange.orders == {}

    asyncio.run(run())
