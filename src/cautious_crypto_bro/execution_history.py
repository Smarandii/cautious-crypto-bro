"""Query progress is distinct from observed execution timestamps."""

from dataclasses import dataclass
from typing import Literal

HISTORY_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
RECENT_HISTORY_MS = 48 * 60 * 60 * 1000
HISTORY_RETENTION_MS = 730 * 24 * 60 * 60 * 1000
HISTORY_QUERY_INTERVAL_MS = 60_000
HISTORY_QUERY_BUDGET = 4
HISTORY_PAGE_BUDGET = 100


@dataclass(frozen=True)
class ExecutionHistoryPage:
    rows: tuple[dict, ...]
    next_cursor: str


@dataclass(frozen=True)
class HistoryQuery:
    symbol: str
    lane: Literal["recent", "archive"]
    start_ms: int
    end_ms: int
    cursor: str
    seen_cursors: tuple[str, ...]
    attempted_at_ms: int
    limited_before_ms: int
