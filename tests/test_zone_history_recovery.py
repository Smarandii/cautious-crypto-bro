"""Bounded query progress survives restart independently of fill timestamps."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from test_remaining_concerns import executor_for
from test_workflow_integration import ExchangeTransport
from test_zone_execution_recording import (
    _bind_entries,
    _make_replay_due,
    _recorded_rows,
    _row,
    _supervisor,
)
from test_zone_ledger_integration import zones

from cautious_crypto_bro.domain import StoreError, TradeExecutionError
from cautious_crypto_bro.execution_history import (
    HISTORY_QUERY_INTERVAL_MS,
    HISTORY_WINDOW_MS,
)
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


def test_schema14_upgrade_preserves_frozen_evidence_and_is_idempotent(tmp_path):
    async def run():
        database = tmp_path / "migration.sqlite3"
        store, _, _ = await zones(database)
        with closing(sqlite3.connect(database)) as db:
            tables = (
                "zone_groups",
                "zone_allocations",
                "zone_orders",
                "zone_executions",
            )
            before = {
                name: db.execute(f"SELECT * FROM {name}").fetchall() for name in tables
            }
            db.execute("DROP TABLE zone_history_queries")
            db.execute("PRAGMA user_version=14")
            db.commit()
        await store.initialize()
        await IntentStore(database).initialize()
        with closing(sqlite3.connect(database)) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (15,)
            assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert {
                name: db.execute(f"SELECT * FROM {name}").fetchall() for name in tables
            } == before
            assert db.execute(
                "SELECT count(*) FROM zone_history_queries"
            ).fetchone() == (0,)

    asyncio.run(run())


def test_query_cursor_window_throttle_and_failures_survive_restart(tmp_path):
    async def run():
        database = tmp_path / "queries.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        now = int(datetime(2026, 10, 29, 12, tzinfo=UTC).timestamp() * 1000)
        queries = await store.prepare_zone_history_queries(("BTCUSDT",), now)
        assert {q.lane for q in queries} == {"recent", "archive"}
        assert all(
            0 < q.start_ms <= q.end_ms <= q.start_ms + HISTORY_WINDOW_MS
            for q in queries
        )
        recent = next(q for q in queries if q.lane == "recent")
        archive = next(q for q in queries if q.lane == "archive")
        await store.finish_zone_history_query(recent, "page-2", now)
        await store.finish_zone_history_query(archive, "archive-page-2", now)
        restarted = IntentStore(database)
        await restarted.initialize()
        assert await restarted.prepare_zone_history_queries(("BTCUSDT",), now + 1) == ()
        due = await restarted.prepare_zone_history_queries(
            ("BTCUSDT",), now + HISTORY_QUERY_INTERVAL_MS
        )
        resumed = next(q for q in due if q.lane == "recent")
        resumed_archive = next(q for q in due if q.lane == "archive")
        assert resumed_archive.cursor == "archive-page-2"
        assert resumed_archive.start_ms == archive.start_ms
        assert resumed.cursor == "page-2"
        assert (resumed.start_ms, resumed.end_ms) == (recent.start_ms, recent.end_ms)
        with pytest.raises(StoreError, match="cursor repeated"):
            await restarted.finish_zone_history_query(resumed, "page-2", now)
        await restarted.finish_zone_history_query(
            resumed, "", now, error="synthetic incomplete page"
        )
        with closing(sqlite3.connect(database)) as db:
            row = db.execute(
                "SELECT cursor,last_error,completed_at_ms FROM zone_history_queries WHERE lane='recent'"
            ).fetchone()
            assert row == ("", "synthetic incomplete page", None)
        with pytest.raises(StoreError, match="ownership changed"):
            await store.finish_zone_history_query(recent, "", now)

    asyncio.run(run())


def test_global_query_budget_is_fair_and_recent_windows_repeat(tmp_path):
    async def run():
        store = IntentStore(tmp_path / "fairness.sqlite3")
        await store.initialize()
        now = int(datetime(2026, 10, 29, 12, tzinfo=UTC).timestamp() * 1000)
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        first = await store.prepare_zone_history_queries(symbols, now)
        assert len(first) == 4
        for query in first:
            await store.finish_zone_history_query(query, "", now)
        second = await store.prepare_zone_history_queries(symbols, now + 1)
        assert len(second) == 2
        assert {(q.symbol, q.lane) for q in first}.isdisjoint(
            (q.symbol, q.lane) for q in second
        )
        due = await store.prepare_zone_history_queries(
            symbols, now + HISTORY_QUERY_INTERVAL_MS
        )
        assert any(q.lane == "recent" and q.cursor == "" for q in due)

    asyncio.run(run())


def test_persistent_page_budget_never_claims_an_infinite_history_complete(tmp_path):
    async def run():
        database = tmp_path / "page-budget.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        now = int(datetime(2026, 10, 29, 12, tzinfo=UTC).timestamp() * 1000)
        for index in range(100):
            queries = await IntentStore(database).prepare_zone_history_queries(
                ("BTCUSDT",), now + index * HISTORY_QUERY_INTERVAL_MS
            )
            query = next(q for q in queries if q.lane == "archive")
            if index == 99:
                with pytest.raises(StoreError, match="page budget"):
                    await store.finish_zone_history_query(query, "page-100", now)
                break
            await store.finish_zone_history_query(query, f"page-{index + 1}", now)
        with closing(sqlite3.connect(database)) as db:
            cursor, completed, boundary = db.execute(
                "SELECT cursor,completed_end_ms,limited_before_ms FROM zone_history_queries WHERE lane='archive'"
            ).fetchone()
            assert cursor == "page-99" and completed is None and boundary > 0

    asyncio.run(run())


@pytest.mark.parametrize("incomplete", [False, True])
def test_http_pages_to_sqlite_resume_without_inventing_completeness(
    tmp_path, incomplete
):
    class Exchange(ExchangeTransport):
        broken = incomplete
        rows = ()

        def handle(self, request):
            if request.url.path != "/v5/execution/list":
                return super().handle(request)
            start, end = (
                int(request.url.params["startTime"]),
                int(request.url.params["endTime"]),
            )
            rows = [row for row in self.rows if start <= int(row["execTime"]) <= end]
            if not rows:
                result = {"list": [], "nextPageCursor": ""}
            elif request.url.params.get("cursor") == "page-2":
                result = {"list": [rows[1]], "nextPageCursor": ""}
                if self.broken:
                    del result["nextPageCursor"]
            else:
                result = {"list": [rows[0]], "nextPageCursor": "page-2"}
            return httpx.Response(200, json={"retCode": 0, "result": result})

    async def run():
        database = tmp_path / "http-pages.sqlite3"
        store, group, plans = await zones(database)
        await _bind_entries(store, group, plans)
        order_id = f"id-{plans[0].intent_id.hex[:6]}-e1"
        timestamp = int(datetime.now(UTC).timestamp() * 1000) - 1000
        exchange = Exchange()
        exchange.rows = (
            _row("first", order_id, "Buy", "0.1", "0", "100", timestamp),
            _row(
                "older-on-next-page",
                order_id,
                "Buy",
                "0.1",
                "0",
                "100",
                timestamp - 500,
            ),
        )
        with executor_for(exchange) as executor:
            await _supervisor(store, executor).reconcile_once()
            assert [r.execution_id for r in await _recorded_rows(store, group)] == [
                "first"
            ]
            _make_replay_due(store)
            await _supervisor(IntentStore(database), executor).reconcile_once()
            if incomplete:
                assert len(await _recorded_rows(store, group)) == 1
                with closing(sqlite3.connect(database)) as db:
                    error, complete = db.execute(
                        "SELECT last_error,completed_end_ms FROM zone_history_queries WHERE lane='recent'"
                    ).fetchone()
                    assert error and complete is None
                exchange.broken = False
                for _ in range(2):
                    _make_replay_due(store)
                    await _supervisor(IntentStore(database), executor).reconcile_once()
        rows = await _recorded_rows(store, group)
        assert {r.execution_id for r in rows} == {"first", "older-on-next-page"}
        assert sum(r.fee for r in rows) == Decimal("0.02")
        assert not exchange.orders

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault", [None, "result", "list", "cursor", "missing-cursor", "repeated"]
)
def test_execution_adapter_fetches_one_explicit_bounded_page(fault):
    class Exchange(ExchangeTransport):
        query = None

        def handle(self, request):
            if request.url.path == "/v5/execution/list":
                self.query = dict(request.url.params)
                result = {"list": [], "nextPageCursor": "next-page"}
                if fault == "result":
                    result = None
                elif fault == "list":
                    result["list"] = ["invalid"]
                elif fault == "cursor":
                    result["nextPageCursor"] = None
                elif fault == "missing-cursor":
                    del result["nextPageCursor"]
                elif fault == "repeated":
                    result["nextPageCursor"] = "current-page"
                return httpx.Response(200, json={"retCode": 0, "result": result})
            return super().handle(request)

    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            if fault is not None:
                with pytest.raises(TradeExecutionError):
                    await executor.execution_history_page(
                        "BTCUSDT", 1000, 2000, "current-page"
                    )
            else:
                page = await executor.execution_history_page(
                    "BTCUSDT", 1000, 2000, "current-page"
                )
                assert page.rows == () and page.next_cursor == "next-page"
        assert exchange.query == {
            "category": "linear",
            "symbol": "BTCUSDT",
            "startTime": "1000",
            "endTime": "2000",
            "limit": "100",
            "cursor": "current-page",
        }

    asyncio.run(run())
