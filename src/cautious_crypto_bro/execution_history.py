"""Query progress is distinct from observed execution timestamps."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from .domain import Side, StoreError
from .entry_ownership import recover_entry_bindings
from .zone_ledger import ZoneExecution

if TYPE_CHECKING:
    from .ports import AccountGateway, PositionSupervisorStore

logger = logging.getLogger(__name__)

HISTORY_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
RECENT_HISTORY_MS = 48 * 60 * 60 * 1000
HISTORY_RETENTION_MS = 730 * 24 * 60 * 60 * 1000
HISTORY_QUERY_INTERVAL_MS = 60_000
HISTORY_QUERY_BUDGET = 4
HISTORY_PAGE_BUDGET = 100
ZONE_EXECUTION_RECORD_INTERVAL_S = 60.0


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
    allocation_since_ms: int | None = None


async def record_zone_executions(
    store: PositionSupervisorStore,
    executor: AccountGateway,
) -> None:
    """Replay bounded pages against exact ownership, including terminal groups."""
    try:
        for _state, plan in await store.get_active_position_strategies():
            if plan.entry_slices:
                await recover_entry_bindings(plan, store, executor)
        groups_by_symbol = await store.zone_order_symbols()
        queries = await store.prepare_zone_history_queries(
            tuple(groups_by_symbol), int(datetime.now(UTC).timestamp() * 1000)
        )
        for query in queries:
            try:
                await _record_history_query(
                    store, executor, query, groups_by_symbol[query.symbol]
                )
            except Exception as exc:
                await store.finish_zone_history_query(
                    query,
                    "",
                    int(datetime.now(UTC).timestamp() * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
                logger.exception(
                    "Zone execution query remains incomplete for %s/%s",
                    query.symbol,
                    query.lane,
                )
    except Exception:
        logger.exception("Zone execution recording pass failed")


async def _record_history_query(
    store: PositionSupervisorStore,
    executor: AccountGateway,
    query: HistoryQuery,
    group_ids: tuple[UUID, ...],
) -> None:
    page = await executor.execution_history_page(
        query.symbol, query.start_ms, query.end_ms, query.cursor
    )
    if any(
        not isinstance(row.get("execType"), str)
        or (
            row["execType"] == "Trade"
            and (not row.get("orderId") or not row.get("execId"))
        )
        for row in page.rows
    ):
        raise StoreError("History page lacks execution identity/type evidence")
    for group_id in group_ids:
        bound = {order.order_id for order in await store.zone_bound_orders(group_id)}
        await _record_group_executions(store, bound, query, page.rows)
    await store.finish_zone_history_query(
        query, page.next_cursor, int(datetime.now(UTC).timestamp() * 1000)
    )


async def _record_group_executions(
    store: PositionSupervisorStore,
    bound: set[str],
    query: HistoryQuery,
    rows: tuple[dict, ...],
) -> None:
    owned = [
        row
        for row in rows
        if row.get("orderId") in bound and row.get("execType") == "Trade"
    ]
    executions = []
    for row in owned:
        if row.get("side") not in {"Buy", "Sell"} or row.get("symbol") != query.symbol:
            raise StoreError("Bound execution has invalid symbol/direction evidence")
        execution = ZoneExecution(
            execution_id=row["execId"],
            order_id=row["orderId"],
            side=Side.LONG if row["side"] == "Buy" else Side.SHORT,
            quantity=Decimal(str(row["execQty"])),
            closed_quantity=Decimal(str(row.get("closedSize") or 0)),
            price=Decimal(str(row["execPrice"])),
            fee=Decimal(str(row["execFee"])),
            executed_at_ms=int(row["execTime"]),
        )
        if not query.start_ms <= execution.executed_at_ms <= query.end_ms:
            raise StoreError(
                "Bound execution lies outside its requested history window"
            )
        executions.append(execution)
    # Zen of Python: "In the face of ambiguity, refuse the temptation to guess."
    # An observed maximum is never a completeness checkpoint. Only consumed
    # query pages advance, and retained execution IDs make replay idempotent.
    for execution in sorted(
        executions, key=lambda e: (e.executed_at_ms, e.execution_id)
    ):
        await store.record_zone_execution(execution)
