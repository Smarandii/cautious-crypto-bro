"""Retention headroom must coexist with stable durable pagination."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from cautious_crypto_bro.execution_history import HISTORY_RETENTION_MS
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration
DAY_MS = 24 * 60 * 60 * 1000


def test_retention_floor_has_request_headroom_near_midnight(tmp_path):
    async def run():
        store = IntentStore(tmp_path / "retention.sqlite3")
        await store.initialize()
        prepared_at = int(datetime(2026, 10, 29, 23, 59, tzinfo=UTC).timestamp() * 1000)
        queries = await store.prepare_zone_history_queries(("BTCUSDT",), prepared_at)
        archive = next(q for q in queries if q.lane == "archive")
        server_now = prepared_at + 2 * 60 * 1000
        assert archive.start_ms > server_now - HISTORY_RETENTION_MS
        assert archive.limited_before_ms == archive.start_ms

    asyncio.run(run())


def test_utc_boundary_restarts_old_window_without_fabricated_completion(tmp_path):
    async def run():
        database = tmp_path / "midnight.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        now = int(datetime(2026, 10, 29, 12, tzinfo=UTC).timestamp() * 1000)
        query = next(
            q
            for q in await store.prepare_zone_history_queries(("BTCUSDT",), now)
            if q.lane == "archive"
        )
        await store.finish_zone_history_query(query, "page-2", now)
        same_day = next(
            q
            for q in await IntentStore(database).prepare_zone_history_queries(
                ("BTCUSDT",), now + 60_000
            )
            if q.lane == "archive"
        )
        assert same_day.cursor == "page-2" and same_day.start_ms == query.start_ms
        tomorrow = next(
            q
            for q in await IntentStore(database).prepare_zone_history_queries(
                ("BTCUSDT",), now + DAY_MS
            )
            if q.lane == "archive"
        )
        assert tomorrow.cursor == "" and tomorrow.start_ms == query.start_ms + DAY_MS
        assert tomorrow.limited_before_ms == tomorrow.start_ms
        with closing(sqlite3.connect(database)) as db:
            error, completed = db.execute(
                "SELECT last_error,completed_end_ms FROM zone_history_queries WHERE lane='archive'"
            ).fetchone()
            assert error and completed is None

    asyncio.run(run())
