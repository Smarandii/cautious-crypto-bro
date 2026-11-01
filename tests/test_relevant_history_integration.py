"""Execution catch-up must not scan time before any relevant allocation."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

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

from cautious_crypto_bro.domain import Side
from cautious_crypto_bro.execution_history import HISTORY_RETENTION_MS
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import ZoneOrder

pytestmark = pytest.mark.integration
DAY_MS = 86_400_000


class HistoryExchange(ExchangeTransport):
    def __init__(self, rows=()):
        super().__init__()
        self.rows = rows
        self.windows = []

    def handle(self, request):
        assert request.method == "GET", "History recording attempted exchange mutation"
        if request.url.path != "/v5/execution/list":
            return super().handle(request)
        start = int(request.url.params["startTime"])
        end = int(request.url.params["endTime"])
        self.windows.append((start, end))
        rows = [row for row in self.rows if start <= int(row["execTime"]) <= end]
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": rows,
                    "nextPageCursor": "",
                },
            },
        )


def allocation_time(database, created_at):
    with closing(sqlite3.connect(database)) as db:
        for zone, payload in db.execute(
            "SELECT zone_id,plan_json FROM zone_allocations"
        ).fetchall():
            plan = json.loads(payload)
            plan["created_at"] = created_at
            db.execute(
                "UPDATE zone_allocations SET plan_json=? WHERE zone_id=?",
                (json.dumps(plan), zone),
            )
        db.commit()


@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("age_days", [0, 4, 23])
def test_http_reads_start_at_relevant_frozen_history(tmp_path, side, age_days):
    async def run():
        database = tmp_path / "history.sqlite3"
        store, group, plans = await zones(database, side)
        created = datetime.now(UTC) - timedelta(days=age_days)
        allocation_time(database, created.isoformat())
        await _bind_entries(store, group, plans)
        timestamp = int((created + timedelta(days=1)).timestamp() * 1000)
        if not age_days:
            timestamp = int(datetime.now(UTC).timestamp() * 1000) - 1000
        order = f"id-{plans[0].intent_id.hex[:6]}-e1"
        exchange = HistoryExchange(
            [
                _row(
                    "owned",
                    order,
                    "Buy" if side is Side.LONG else "Sell",
                    ".1",
                    "0",
                    "100",
                    timestamp,
                )
            ]
        )
        with executor_for(exchange) as executor:
            await _supervisor(store, executor).reconcile_once()
        assert exchange.windows
        assert all(
            start >= int(created.timestamp() * 1000) - DAY_MS
            for start, _ in exchange.windows
        )
        if not age_days:
            assert len(exchange.windows) == 1
        assert [row.execution_id for row in await _recorded_rows(store, group)] == [
            "owned"
        ]
        assert not exchange.orders

    asyncio.run(run())


@pytest.mark.parametrize(
    "bad_time", [None, "2026-01-01T00:00:00", "invalid", "2099-01-01T00:00:00+00:00"]
)
def test_unknown_legacy_clock_keeps_conservative_read_scope(tmp_path, bad_time):
    async def run():
        database = tmp_path / "unknown.sqlite3"
        store, group, plans = await zones(database)
        allocation_time(database, bad_time)
        await _bind_entries(store, group, plans)
        exchange = HistoryExchange()
        with executor_for(exchange) as executor:
            await _supervisor(store, executor).reconcile_once()
        assert len(exchange.windows) == 2
        oldest = min(start for start, _ in exchange.windows)
        assert oldest < int(datetime.now(UTC).timestamp() * 1000) - 700 * DAY_MS
        assert await _recorded_rows(store, group) == []

    asyncio.run(run())


def test_old_cached_queries_do_not_force_irrelevant_requests(tmp_path):
    async def run():
        database = tmp_path / "cached.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        now = int(datetime.now(UTC).timestamp() * 1000)
        old = next(
            q
            for q in await store.prepare_zone_history_queries(("BTCUSDT",), now)
            if q.lane == "archive"
        )
        await store.finish_zone_history_query(old, "old-page", now)
        store, group, plans = await zones(database)
        await _bind_entries(store, group, plans)
        _make_replay_due(store)
        exchange = HistoryExchange()
        with executor_for(exchange) as executor:
            await _supervisor(IntentStore(database), executor).reconcile_once()
        assert len(exchange.windows) == 1
        with closing(sqlite3.connect(database)) as db:
            assert db.execute(
                "SELECT cursor,completed_end_ms FROM zone_history_queries WHERE lane='archive'"
            ).fetchone() == ("old-page", None)

    asyncio.run(run())


def test_cursor_completion_wraps_tail_despite_clock_advancement(tmp_path):
    async def run():
        database = tmp_path / "cursor.sqlite3"
        created = datetime.now(UTC) - timedelta(days=4)
        store, group, plans = await zones(database, created_at=created)
        await _bind_entries(store, group, plans)
        now = int(datetime.now(UTC).timestamp() * 1000)
        first = next(
            q
            for q in await store.prepare_zone_history_queries(("BTCUSDT",), now)
            if q.lane == "archive"
        )
        await store.finish_zone_history_query(first, "page-2", now + 300000)
        restarted = IntentStore(database)
        page = next(
            q
            for q in await restarted.prepare_zone_history_queries(
                ("BTCUSDT",), now + 360000
            )
            if q.lane == "archive"
        )
        assert (page.start_ms, page.end_ms, page.cursor) == (
            first.start_ms,
            first.end_ms,
            "page-2",
        )
        await restarted.finish_zone_history_query(page, "", now + 420000)
        replay = next(
            q
            for q in await IntentStore(database).prepare_zone_history_queries(
                ("BTCUSDT",), now + 480000
            )
            if q.lane == "archive"
        )
        assert replay.start_ms == first.start_ms
        assert replay.end_ms > first.end_ms and replay.cursor == ""
        with closing(sqlite3.connect(database)) as db:
            assert db.execute(
                "SELECT completed_start_ms,completed_end_ms FROM zone_history_queries WHERE lane='archive'"
            ).fetchone() == (first.start_ms, first.end_ms)

    asyncio.run(run())


@pytest.mark.parametrize("side", list(Side))
def test_older_allocation_enrolled_after_recent_reads_remains_recoverable(
    tmp_path, side
):
    async def run():
        database = tmp_path / "older-binding.sqlite3"
        store, group, plans = await zones(database, side)
        await _bind_entries(store, group, plans)
        exchange = HistoryExchange()
        with executor_for(exchange) as executor:
            await _supervisor(store, executor).reconcile_once()
            assert len(exchange.windows) == 1
            original = await store.get_intent(plans[0].intent_id)
            created = datetime.now(UTC) - timedelta(days=6)
            source = original.source.model_copy(
                update={
                    "message_id": original.source.message_id + 1,
                    "published_at": created,
                    "received_at": created,
                }
            )
            intent = original.model_copy(
                update={"intent_id": uuid4(), "source": source, "created_at": created}
            )
            plan = plans[0].model_copy(
                update={"intent_id": intent.intent_id, "created_at": created}
            )
            claim = await store.claim_source(source, lease_seconds=300)
            assert await store.create_signal_batch_and_complete_source(
                ((intent, plan),), (), claim
            )
            await store.register_zone(group, intent.intent_id)
            await store.bind_zone_order(
                ZoneOrder(
                    zone_id=intent.intent_id,
                    order_id="older-owned",
                    order_link_id="older-entry",
                    role="ENTRY",
                    quantity=plan.orders[0].quantity,
                )
            )
            exchange.rows = [
                _row(
                    "older-fill",
                    "older-owned",
                    "Buy" if side is Side.LONG else "Sell",
                    ".1",
                    "0",
                    "100",
                    int((created + timedelta(days=1)).timestamp() * 1000),
                )
            ]
            _make_replay_due(store)
            await _supervisor(IntentStore(database), executor).reconcile_once()
        assert [row.execution_id for row in await _recorded_rows(store, group)] == [
            "older-fill"
        ]
        assert len(exchange.windows) == 3 and not exchange.orders

    asyncio.run(run())


def test_allocation_older_than_exchange_retention_keeps_explicit_limit(tmp_path):
    async def run():
        database = tmp_path / "retained.sqlite3"
        created = datetime.now(UTC) - timedelta(days=800)
        store, group, plans = await zones(database, created_at=created)
        await _bind_entries(store, group, plans)
        now = int(datetime.now(UTC).timestamp() * 1000)
        archive = next(
            q
            for q in await store.prepare_zone_history_queries(("BTCUSDT",), now)
            if q.lane == "archive"
        )
        assert archive.start_ms == archive.limited_before_ms
        assert archive.start_ms > now - HISTORY_RETENTION_MS
        assert archive.allocation_since_ms < archive.limited_before_ms
        assert await _recorded_rows(store, group) == []

    asyncio.run(run())
