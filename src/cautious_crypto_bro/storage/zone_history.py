"""Durable bounded replay work; completed pages do not prove immutable history."""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..domain import StoreError
from ..execution_history import (
    HISTORY_PAGE_BUDGET,
    HISTORY_QUERY_BUDGET,
    HISTORY_QUERY_INTERVAL_MS,
    HISTORY_RETENTION_MS,
    HISTORY_WINDOW_MS,
    RECENT_HISTORY_MS,
    HistoryQuery,
)
from ._connection import _Store, sqlite_connection


def _window(lane: str, previous_end: int, now_ms: int) -> tuple[int, int]:
    recent = max(1, now_ms - RECENT_HISTORY_MS)
    if lane == "recent":
        return recent, now_ms
    floor = max(1, now_ms - HISTORY_RETENTION_MS)
    start = previous_end + 1
    if start < floor or start >= recent:
        start = floor
    return start, min(start + HISTORY_WINDOW_MS, recent - 1)


class ZoneHistoryRepositoryImpl(_Store):
    async def prepare_zone_history_queries(
        self, symbols: Sequence[str], now_ms: int
    ) -> tuple[HistoryQuery, ...]:
        if not symbols:
            return ()
        floor = max(1, now_ms - HISTORY_RETENTION_MS)
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            for symbol in sorted(set(symbols)):
                for lane in ("recent", "archive"):
                    start, end = _window(lane, 0, now_ms)
                    await db.execute(
                        "INSERT OR IGNORE INTO zone_history_queries"
                        "(symbol,lane,start_ms,end_ms,limited_before_ms) VALUES (?,?,?,?,?)",
                        (symbol, lane, start, end, floor),
                    )
                    # Retention is an explicit limitation, never fabricated coverage.
                    await db.execute(
                        "UPDATE zone_history_queries SET start_ms=?,end_ms=?,cursor='',"
                        "seen_cursors_json='[]',last_error='History window aged beyond retention' "
                        "WHERE symbol=? AND lane=? AND end_ms<?",
                        (start, end, symbol, lane, floor),
                    )
                    await db.execute(
                        "UPDATE zone_history_queries SET limited_before_ms=? "
                        "WHERE symbol=? AND lane=?",
                        (floor, symbol, lane),
                    )
            placeholders = ",".join("?" for _ in symbols)
            cursor = await db.execute(
                "SELECT symbol,lane,start_ms,end_ms,cursor,seen_cursors_json,limited_before_ms "
                f"FROM zone_history_queries WHERE symbol IN ({placeholders}) "
                "AND last_attempt_ms<=? ORDER BY last_attempt_ms,lane DESC,symbol LIMIT ?",
                (*symbols, now_ms - HISTORY_QUERY_INTERVAL_MS, HISTORY_QUERY_BUDGET),
            )
            queries = tuple(
                HistoryQuery(
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    tuple(json.loads(row[5])),
                    now_ms,
                    row[6],
                )
                for row in await cursor.fetchall()
            )
            for query in queries:
                await db.execute(
                    "UPDATE zone_history_queries SET last_attempt_ms=? WHERE symbol=? AND lane=?",
                    (now_ms, query.symbol, query.lane),
                )
            await db.commit()
        return queries

    async def finish_zone_history_query(
        self,
        query: HistoryQuery,
        next_cursor: str,
        now_ms: int,
        *,
        error: str | None = None,
    ) -> None:
        if error is None and next_cursor:
            if next_cursor in (*query.seen_cursors, query.cursor):
                raise StoreError(
                    "Execution history cursor repeated across persisted pages"
                )
            if len(query.seen_cursors) >= HISTORY_PAGE_BUDGET - 1:
                raise StoreError(
                    "Execution history page budget exceeded; query remains incomplete"
                )
        start, end = query.start_ms, query.end_ms
        seen = (*query.seen_cursors, next_cursor) if next_cursor else ()
        completed = error is None and not next_cursor
        if completed:
            start, end = _window(query.lane, query.end_ms, now_ms)
        if error is not None:
            # Retry the same window from its first page: expired cursors must
            # not strand recovery. Already retained execution IDs are idempotent.
            next_cursor, seen = "", ()
        async with sqlite_connection(self._database_path) as db:
            cursor = await db.execute(
                "UPDATE zone_history_queries SET start_ms=?,end_ms=?,cursor=?,"
                "seen_cursors_json=?,last_error=?,"
                "completed_start_ms=CASE WHEN ? THEN ? ELSE completed_start_ms END,"
                "completed_end_ms=CASE WHEN ? THEN ? ELSE completed_end_ms END,"
                "completed_at_ms=CASE WHEN ? THEN ? ELSE completed_at_ms END "
                "WHERE symbol=? AND lane=? AND start_ms=? AND end_ms=? AND cursor=? "
                "AND last_attempt_ms=?",
                (
                    start,
                    end,
                    next_cursor,
                    json.dumps(seen),
                    error[:1000] if error else None,
                    completed,
                    query.start_ms,
                    completed,
                    query.end_ms,
                    completed,
                    now_ms,
                    query.symbol,
                    query.lane,
                    query.start_ms,
                    query.end_ms,
                    query.cursor,
                    query.attempted_at_ms,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("Execution history query ownership changed")
            await db.commit()
