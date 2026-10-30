"""Entry admission must be durable before a real client mutates fake HTTP state."""

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from decimal import Decimal

import httpx
import pytest
from test_instrument_leverage_integration import LeverageLimitedExchange
from test_remaining_concerns import executor_for, make_intent

from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryPreflightError,
    EntryType,
    ExecutionPolicy,
    IntentStatus,
    OpenRelation,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class TierExchange(LeverageLimitedExchange):
    def __init__(self, database):
        super().__init__()
        self.database = database
        self.persisted_at_submission = []
        self.pre_submission_plans = []
        self.risk_reads = 0
        self.symbol_reads = 0
        self.current_risk_id = 1
        self.risk_mode = "normal"
        self.inject_unowned_on_submission = False
        self.server_failure = 0

    def handle(self, request):
        path = request.url.path
        if path == "/v5/market/risk-limit":
            self.risk_reads += 1
            cap = "2" if self.risk_mode == "changed" and self.risk_reads >= 2 else "5"
            if self.risk_mode == "invalid":
                cap = "NaN"
            first_limit = "100000" if self.risk_mode == "current_stricter" else "10"
            second_limit = (
                "200000" if self.risk_mode == "current_stricter" else "100000"
            )
            if self.risk_mode == "too_large":
                first_limit, second_limit = "1", "2"
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "id": 1,
                                "symbol": "BTCUSDT",
                                "riskLimitValue": first_limit,
                                "maxLeverage": "12.5",
                            },
                            {
                                "id": 2,
                                "symbol": "BTCUSDT",
                                "riskLimitValue": second_limit,
                                "maxLeverage": cap,
                            },
                        ],
                        "nextPageCursor": "repeated"
                        if self.risk_mode == "repeated_cursor"
                        else "",
                    },
                },
            )
        if path == "/v5/position/list" and request.url.params.get("symbol"):
            self.symbol_reads += 1
            if self.inject_unowned_on_submission and self.symbol_reads == 2:
                self.create(
                    {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "orderType": "Limit",
                        "qty": "1",
                        "price": "80",
                        "orderLinkId": "manual-entry",
                        "reduceOnly": False,
                    }
                )
            row = self.position() | {
                "riskId": self.current_risk_id,
                "riskLimitValue": "0",
                "positionIdx": 0,
            }
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"list": [row], "nextPageCursor": ""}},
            )
        if path == "/v5/position/set-leverage":
            body = json.loads(request.content)
            value = Decimal(body["buyLeverage"])
            self.selected_leverages.append(value)
            with closing(sqlite3.connect(self.database)) as db:
                payload = json.loads(
                    db.execute("SELECT payload_json FROM execution_plans").fetchone()[0]
                )
                self.persisted_at_submission.append(Decimal(payload["leverage"]))
                self.pre_submission_plans.append(payload)
            if self.server_failure:
                return httpx.Response(
                    200,
                    json={
                        "retCode": self.server_failure,
                        "retMsg": "Synthetic margin/permission rejection",
                    },
                )
            if value > 5:
                return httpx.Response(
                    200,
                    json={
                        "retCode": 110013,
                        "retMsg": "Leverage exceeds applicable tier",
                    },
                )
            return httpx.Response(200, json={"retCode": 0, "result": {}})
        return super().handle(request)


async def run_entry(
    database,
    exchange,
    entry_type=EntryType.LIMIT,
    saved_leverage="12.5",
    fail_persist=False,
    bad_preparation=None,
):
    store = IntentStore(database)
    await store.initialize()
    intent = make_intent(mode=ApprovalMode.AUTO).model_copy(
        update={
            "leverage": 50.0,
            "relation": OpenRelation.NEW,
            "entry": Entry(type=EntryType.RANGE, range_low=100, range_high=102)
            if entry_type is EntryType.RANGE
            else Entry(
                type=entry_type,
                price=100 if entry_type is EntryType.LIMIT else None,
            ),
        }
    )
    with executor_for(exchange) as executor:
        planner = ExecutionPlanner()
        context = await executor.market_context(intent.symbol)
        original = planner.plan(
            intent, ExecutionPolicy(trading_capital_usdt=Decimal("1000")), context
        )
        saved = original.model_copy(update={"leverage": Decimal(saved_leverage)})
        claim = await store.claim_source(intent.source, lease_seconds=300)
        assert claim is not None
        assert await store.create_signal_batch_and_complete_source(
            [(intent, saved)], (), claim
        )
        coordinator = ExecutionCoordinator(
            store=store, executor=executor, planner=planner, max_age_seconds=900
        )
        if fail_persist:

            async def unavailable_update(_plan):
                raise RuntimeError("Synthetic plan persistence unavailable")

            store.update_execution_plan = unavailable_update
        if bad_preparation:
            real_prepare = executor.prepare_entry_plan

            async def invalid_prepare(plan):
                prepared = await real_prepare(plan)
                if bad_preparation == "increase":
                    return prepared.model_copy(update={"leverage": Decimal("100")})
                order = prepared.orders[0].model_copy(
                    update={"quantity": prepared.orders[0].quantity * 2}
                )
                return prepared.model_copy(
                    update={"orders": (order, *prepared.orders[1:])}
                )

            executor.prepare_entry_plan = invalid_prepare
        outcome = await coordinator.execute_intent(
            intent.intent_id, approval_mode=ApprovalMode.AUTO
        )
        return store, intent, original, outcome


@pytest.mark.parametrize(
    "entry_type", [EntryType.LIMIT, EntryType.MARKET, EntryType.RANGE]
)
@pytest.mark.parametrize("saved_leverage", ["50", "12.5"])
def test_prepared_tier_leverage_is_saved_before_submission(
    tmp_path, entry_type, saved_leverage
):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = TierExchange(database)
        store, intent, original, outcome = await run_entry(
            database, exchange, entry_type, saved_leverage
        )
        assert outcome.status is IntentStatus.EXECUTED
        assert exchange.selected_leverages == [Decimal("5")]
        assert exchange.persisted_at_submission == [Decimal("5")]
        restored = await store.get_execution_plan(intent.intent_id)
        assert restored is not None and restored.leverage == 5
        original_intent = await store.get_intent(intent.intent_id)
        assert original_intent is not None and original_intent.leverage == 50.0
        assert outcome.plan is not None and outcome.plan.leverage == 5
        recorded = dict(exchange.pre_submission_plans[0])
        recorded.pop("leverage")
        recorded.pop("created_at")
        assert recorded == original.model_dump(
            mode="json", exclude={"leverage", "created_at"}
        )

    asyncio.run(run())


def test_entry_preparation_reads_all_risk_tier_pages(tmp_path):
    class PagedTierExchange(TierExchange):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path != "/v5/market/risk-limit":
                return response
            body = response.json()
            second_page = request.url.params.get("cursor") == "second"
            body["result"]["list"] = body["result"]["list"][
                1 if second_page else 0 : 2 if second_page else 1
            ]
            body["result"]["nextPageCursor"] = "" if second_page else "second"
            return httpx.Response(200, json=body)

    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = PagedTierExchange(database)
        _, _, _, outcome = await run_entry(database, exchange)
        assert outcome.status is IntentStatus.EXECUTED
        assert exchange.risk_reads == 4  # Two pages at preparation and submission.
        assert exchange.persisted_at_submission == [Decimal("5")]
        assert exchange.selected_leverages == [Decimal("5")]

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        "invalid",
        "repeated_cursor",
        "portfolio_margin",
        "changed",
        "unowned_order",
        "persist",
        "margin",
        "permission",
        "too_large",
        "unknown_tier",
    ],
)
def test_preflight_failures_do_not_submit_entries_or_replay(tmp_path, failure):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = TierExchange(database)
        if failure in {"invalid", "repeated_cursor", "changed", "too_large"}:
            exchange.risk_mode = failure
        if failure == "portfolio_margin":
            exchange.current_risk_id = 0
        if failure == "unknown_tier":
            exchange.current_risk_id = 999
        if failure == "unowned_order":
            exchange.inject_unowned_on_submission = True
        if failure in {"margin", "permission"}:
            exchange.server_failure = 110007 if failure == "margin" else 10005
        store, intent, _, outcome = await run_entry(
            database, exchange, fail_persist=failure == "persist"
        )
        assert outcome.status is IntentStatus.FAILED
        assert not any(link.startswith("ccb-") for link in exchange.orders)
        assert exchange.selected_leverages == (
            [Decimal("5")] if failure in {"margin", "permission"} else []
        )
        restored = await store.get_intent(intent.intent_id)
        assert restored is not None and restored.status is IntentStatus.FAILED
        before = len(exchange.selected_leverages)
        with executor_for(exchange) as executor:
            coordinator = ExecutionCoordinator(
                store=store,
                executor=executor,
                planner=ExecutionPlanner(),
                max_age_seconds=900,
            )
            assert (
                await coordinator.execute_intent(
                    intent.intent_id, approval_mode=ApprovalMode.AUTO
                )
            ).status is IntentStatus.FAILED
        assert len(exchange.selected_leverages) == before

    asyncio.run(run())


def test_current_tier_can_be_stricter_than_projected_notional_tier(tmp_path):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = TierExchange(database)
        exchange.current_risk_id = 2
        exchange.risk_mode = "current_stricter"
        _, _, _, outcome = await run_entry(database, exchange)
        assert outcome.status is IntentStatus.EXECUTED
        assert exchange.persisted_at_submission == [Decimal("5")]

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["increase", "geometry"])
def test_preparation_cannot_increase_leverage_or_change_risk_geometry(tmp_path, kind):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = TierExchange(database)
        _, _, _, outcome = await run_entry(database, exchange, bad_preparation=kind)
        assert outcome.status is IntentStatus.FAILED
        assert "frozen risk geometry or increased leverage" in outcome.message
        assert exchange.selected_leverages == [] and exchange.orders == {}

    asyncio.run(run())


@pytest.mark.parametrize("entry_type", [EntryType.LIMIT, EntryType.MARKET])
def test_direct_submission_rejects_a_plan_that_requires_preparation(
    tmp_path, entry_type
):
    async def run():
        exchange = TierExchange(tmp_path / "unused.sqlite3")
        with executor_for(exchange) as executor:
            intent = make_intent().model_copy(
                update={
                    "entry": Entry(
                        type=entry_type,
                        price=100 if entry_type is EntryType.LIMIT else None,
                    ),
                    "leverage": 50.0,
                }
            )
            context = await executor.market_context(intent.symbol)
            plan = ExecutionPlanner().plan(
                intent, ExecutionPolicy(trading_capital_usdt=Decimal("1000")), context
            )
            with pytest.raises(EntryPreflightError, match="prepare and persist"):
                if entry_type is EntryType.MARKET:
                    await executor.execute_market_primary(plan)
                else:
                    await executor.execute(plan)
        assert exchange.selected_leverages == [] and exchange.orders == {}

    asyncio.run(run())


def test_cancellation_drains_preparation_and_keeps_claim_until_terminal_result(
    tmp_path,
):
    class BlockingTierExchange(TierExchange):
        def __init__(self, database):
            super().__init__(database)
            self.started = threading.Event()
            self.release = threading.Event()

        def handle(self, request):
            if (
                request.url.path == "/v5/market/risk-limit"
                and not self.release.is_set()
            ):
                self.started.set()
                assert self.release.wait(10), "Preparation test did not release HTTP"
            return super().handle(request)

    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = BlockingTierExchange(database)
        execution = asyncio.create_task(run_entry(database, exchange))
        try:
            assert await asyncio.to_thread(exchange.started.wait, 3)
            execution.cancel()
            await asyncio.sleep(0)
            assert not execution.done()
            with closing(sqlite3.connect(database)) as db:
                assert (
                    db.execute("SELECT status FROM intents").fetchone()[0]
                    == "EXECUTING"
                )
            exchange.release.set()
            with pytest.raises(asyncio.CancelledError):
                await execution
            with closing(sqlite3.connect(database)) as db:
                assert (
                    db.execute("SELECT status FROM intents").fetchone()[0] == "EXECUTED"
                )
                assert (
                    Decimal(
                        json.loads(
                            db.execute(
                                "SELECT payload_json FROM execution_plans"
                            ).fetchone()[0]
                        )["leverage"]
                    )
                    == 5
                )
            assert exchange.selected_leverages == [Decimal("5")]
            assert len(exchange.orders) == 3
        finally:
            exchange.release.set()
            await asyncio.gather(execution, return_exceptions=True)

    asyncio.run(run())
