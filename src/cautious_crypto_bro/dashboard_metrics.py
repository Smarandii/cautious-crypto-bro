"""Metrics for complete, reconciled strategy positions shown in the dashboard."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from math import isfinite
from statistics import fmean


def public_trade_history(
    positions: Iterable[Mapping[str, object]], *, available: bool, limit: int = 100
) -> dict[str, object]:
    """Public fields only; private lineage must never travel with a trade row."""
    if limit < 1:
        raise ValueError("Public trade limit must be positive")
    complete = [p for p in positions if p.get("complete") is True]
    rows = []
    for position in complete:
        symbol, side = position.get("symbol"), position.get("side")
        if not isinstance(symbol, str) or not re.fullmatch(
            r"[A-Z0-9]{1,40}USDT", symbol
        ):
            raise ValueError("Invalid public trade symbol")
        if side not in ("LONG", "SHORT"):
            raise ValueError("Invalid public trade side")
        closed_ms = _optional_number(position.get("closed_at"))
        closed_at = None
        if closed_ms is not None and closed_ms > 0:
            try:
                closed_at = datetime.fromtimestamp(closed_ms / 1000, UTC).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        # Zen of Python: "Explicit is better than implicit." Whitelist fields;
        # never relabel a historical trade with the currently configured risk.
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": _optional_number(position.get("entry_qty")),
                "net_pnl_usdt": _optional_number(position.get("net_pnl")),
                "net_r": _optional_number(position.get("net_r")),
                "closed_at": closed_at,
                "risk_per_trade_pct": _optional_number(
                    position.get("risk_per_trade_pct")
                ),
            }
        )
    rows.sort(key=lambda row: str(row["closed_at"] or ""), reverse=True)
    return {
        "available": available,
        "rows": rows[:limit] if available else [],
        "total_completed": len(complete) if available else None,
        "omitted_count": max(0, len(rows) - limit) if available else None,
        "limit": limit,
    }


def summarize_reconciled_positions(
    positions: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Summarize only whole positions with reconciled exchange P&L and fills."""
    complete = [position for position in positions if position.get("complete") is True]
    pnl = [_number(position, "net_pnl") for position in complete]
    net_r = [_number(position, "net_r") for position in complete]
    fees = sum(_number(position, "fees") for position in complete)
    funding = sum(_number(position, "funding") for position in complete)
    gross_price_pnl = sum(_number(position, "gross_pnl") for position in complete)
    winners = [value for value in pnl if value > 0]
    losers = [value for value in pnl if value < 0]
    winning_r = [value for value in net_r if value > 0]
    losing_r = [value for value in net_r if value < 0]
    winning_risk = [
        _number(position, "risk_usdt")
        for position in complete
        if _number(position, "net_pnl") > 0 and _number(position, "risk_usdt") > 0
    ]
    losing_risk = [
        _number(position, "risk_usdt")
        for position in complete
        if _number(position, "net_pnl") < 0 and _number(position, "risk_usdt") > 0
    ]
    avg_win = fmean(winners) if winners else None
    avg_loss = fmean(losers) if losers else None
    gross_loss = abs(sum(losers))
    break_even_avg_win = (
        abs(avg_loss) * len(losers) / len(winners)
        if avg_loss is not None and winners
        else None
    )
    break_even_avg_loss = (
        -avg_win * len(winners) / len(losers)
        if avg_win is not None and losers
        else None
    )
    avg_win_r = fmean(winning_r) if winning_r else None
    avg_loss_r = fmean(losing_r) if losing_r else None
    avg_win_to_loss_ratio_r = (
        avg_win_r / abs(avg_loss_r)
        if avg_win_r is not None and avg_loss_r is not None
        else None
    )
    break_even_win_rate_r_pct = (
        100 * abs(avg_loss_r) / (avg_win_r + abs(avg_loss_r))
        if avg_win_r is not None and avg_loss_r is not None
        else None
    )
    break_even_avg_win_r = (
        abs(avg_loss_r) * len(losing_r) / len(winning_r)
        if avg_loss_r is not None and winning_r and losing_r
        else None
    )
    break_even_avg_loss_r = (
        -avg_win_r * len(winning_r) / len(losing_r)
        if avg_win_r is not None and winning_r and losing_r
        else None
    )

    return {
        "available": bool(complete),
        "basis": "fully_reconciled_positions",
        "position_count": len(complete),
        "positive_count": len(winners),
        "negative_count": len(losers),
        "win_rate_pct": _rounded(100 * len(winners) / len(complete), 2)
        if complete
        else None,
        "net_pnl_usdt": _rounded(sum(pnl), 8),
        "gross_price_pnl_usdt": _rounded(gross_price_pnl, 8),
        "fees_usdt": _rounded(fees, 8),
        "signed_funding_usdt": _rounded(funding, 8),
        "fee_share_of_abs_gross_pct": _rounded(
            100 * fees / abs(gross_price_pnl) if gross_price_pnl else None,
            4,
        ),
        "avg_win_usdt": _rounded(avg_win, 8),
        "avg_loss_usdt": _rounded(avg_loss, 8),
        "avg_win_to_loss_ratio": _rounded(avg_win / abs(avg_loss), 4)
        if avg_win is not None and avg_loss is not None
        else None,
        "break_even_avg_win_usdt_at_observed_counts": _rounded(break_even_avg_win, 8),
        "break_even_avg_loss_usdt_at_observed_counts": _rounded(break_even_avg_loss, 8),
        "avg_win_increase_pct_to_break_even": _rounded(
            100 * (break_even_avg_win - avg_win) / avg_win
            if break_even_avg_win is not None and avg_win is not None
            else None,
            2,
        ),
        "avg_loss_reduction_pct_to_break_even": _rounded(
            100 * (abs(avg_loss) - abs(break_even_avg_loss)) / abs(avg_loss)
            if break_even_avg_loss is not None and avg_loss is not None
            else None,
            2,
        ),
        "break_even_win_rate_pct": _rounded(
            100 * abs(avg_loss) / (avg_win + abs(avg_loss)), 2
        )
        if avg_win is not None and avg_loss is not None
        else None,
        "profit_factor": _rounded(sum(winners) / gross_loss, 4) if gross_loss else None,
        "expectancy_usdt": _rounded(fmean(pnl), 8) if complete else None,
        "avg_win_r": _rounded(avg_win_r, 6),
        "avg_loss_r": _rounded(avg_loss_r, 6),
        "avg_win_to_loss_ratio_r": _rounded(avg_win_to_loss_ratio_r, 4),
        "break_even_avg_win_r_at_observed_counts": _rounded(break_even_avg_win_r, 6),
        "break_even_avg_loss_r_at_observed_counts": _rounded(break_even_avg_loss_r, 6),
        "avg_win_r_increase_pct_to_break_even": _rounded(
            100 * (break_even_avg_win_r - avg_win_r) / avg_win_r
            if break_even_avg_win_r is not None and avg_win_r
            else None,
            2,
        ),
        "avg_loss_r_reduction_pct_to_break_even": _rounded(
            100 * (abs(avg_loss_r) - abs(break_even_avg_loss_r)) / abs(avg_loss_r)
            if break_even_avg_loss_r is not None and avg_loss_r
            else None,
            2,
        ),
        "break_even_win_rate_r_pct": _rounded(break_even_win_rate_r_pct, 2),
        "expectancy_r": _rounded(fmean(net_r), 6) if complete else None,
        "avg_initial_risk_win_usdt": (
            _rounded(fmean(winning_risk), 8) if winning_risk else None
        ),
        "avg_initial_risk_loss_usdt": (
            _rounded(fmean(losing_risk), 8) if losing_risk else None
        ),
    }


def summarize_position_sides(
    positions: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Summarize complete positions by direction; unknown sides stay excluded."""
    complete = [position for position in positions if position.get("complete") is True]
    return {
        side: summarize_reconciled_positions(
            position for position in complete if position.get("side") == side
        )
        for side in ("LONG", "SHORT")
    }


def summarize_open_positions(
    positions: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Summarize account-level open exposure without mixing it into realized P&L."""
    active = [
        position
        for position in positions
        if (_optional_number(position.get("size")) or 0.0) > 0
    ]
    unrealized = [
        _optional_number(position.get("unrealisedPnl")) for position in active
    ]
    stop_risk = []
    for position in active:
        size = _optional_number(position.get("size"))
        mark = _optional_number(position.get("markPrice"))
        stop = _optional_number(position.get("stopLoss"))
        side = position.get("side")
        if size is None or mark is None or mark <= 0 or stop is None or stop <= 0:
            continue
        if side == "Buy":
            stop_risk.append(max(mark - stop, 0.0) * size)
        elif side == "Sell":
            stop_risk.append(max(stop - mark, 0.0) * size)

    stop_data_complete = bool(active) and len(stop_risk) == len(active)
    return {
        "available": bool(active),
        "position_count": len(active),
        "long_count": sum(position.get("side") == "Buy" for position in active),
        "short_count": sum(position.get("side") == "Sell" for position in active),
        "unrealized_pnl_usdt": _rounded(
            sum(value for value in unrealized if value is not None), 8
        )
        if active and all(value is not None for value in unrealized)
        else None,
        "positions_with_stop": sum(
            (_optional_number(position.get("stopLoss")) or 0.0) > 0
            for position in active
        ),
        "estimated_stop_risk_usdt": _rounded(sum(stop_risk), 8)
        if stop_data_complete
        else None,
        "stop_risk_basis": "mark_to_stop_gross_estimate",
    }


def summarize_portfolio_stop_risk(
    positions: Iterable[Mapping[str, object]],
    entry_orders: Iterable[Mapping[str, object]] | None,
    *,
    cap_usdt: float | None,
    unmatched_entry_order_count: int | None = 0,
) -> dict[str, object]:
    """Estimate cap-basis risk across open positions and tracked entry orders."""
    if cap_usdt is not None and (not isfinite(cap_usdt) or cap_usdt <= 0):
        raise ValueError("Portfolio stop-risk cap must be positive and finite")
    position_rows = list(positions)
    active_positions = [
        position
        for position in position_rows
        if (_optional_number(position.get("size")) or 0.0) > 0
    ]
    position_risks = [
        risk
        for position in active_positions
        if (risk := _position_stop_risk(position)) is not None
    ]
    positions_bounded = all(
        _optional_number(row.get("size")) is not None for row in position_rows
    ) and len(position_risks) == len(active_positions)
    active_orders = [order for order in entry_orders or () if _is_entry_order(order)]
    order_risks = [
        risk
        for order in active_orders
        if (risk := _entry_order_stop_risk(order)) is not None
    ]
    orders_bounded = (
        entry_orders is not None
        and unmatched_entry_order_count == 0
        and len(order_risks) == len(active_orders)
    )
    position_risk = sum(position_risks)
    order_risk = sum(order_risks)
    bounded = positions_bounded and orders_bounded
    combined_risk = position_risk + order_risk if bounded else None
    return {
        "risk_bounded": bounded,
        "basis": "position_average_entry_and_remaining_entry_limit_to_stop",
        "position_count": len(active_positions),
        "positions_with_stop": len(position_risks),
        "position_stop_risk_usdt": _rounded(position_risk, 8)
        if positions_bounded
        else None,
        "entry_order_count": len(active_orders) if entry_orders is not None else None,
        "unmatched_entry_order_count": unmatched_entry_order_count,
        "entry_orders_with_stop": len(order_risks)
        if entry_orders is not None
        else None,
        "entry_order_stop_risk_usdt": _rounded(order_risk, 8)
        if orders_bounded
        else None,
        "combined_stop_risk_usdt": _rounded(combined_risk, 8),
        "cap_usdt": _rounded(cap_usdt, 8),
        "remaining_capacity_usdt": _rounded(cap_usdt - combined_risk, 8)
        if cap_usdt is not None and combined_risk is not None
        else None,
        "cap_utilization_pct": _rounded(100 * combined_risk / cap_usdt, 2)
        if cap_usdt and combined_risk is not None
        else None,
        "cap_exceeded": combined_risk > cap_usdt
        if cap_usdt is not None and combined_risk is not None
        else None,
    }


def _position_stop_risk(position: Mapping[str, object]) -> float | None:
    size = _optional_number(position.get("size"))
    entry = _optional_number(position.get("avgPrice"))
    stop = _optional_number(position.get("stopLoss"))
    if size is None or entry is None or stop is None or entry <= 0 or stop <= 0:
        return None
    if position.get("side") == "Buy":
        return max(entry - stop, 0.0) * size
    if position.get("side") == "Sell":
        return max(stop - entry, 0.0) * size
    return None


def _entry_order_stop_risk(order: Mapping[str, object]) -> float | None:
    remaining = _optional_number(order.get("leavesQty"))
    entry = _optional_number(order.get("price"))
    stop = _optional_number(order.get("stopLoss"))
    if remaining is None or entry is None or stop is None or entry <= 0 or stop <= 0:
        return None
    return remaining * abs(entry - stop)


def _is_entry_order(order: Mapping[str, object]) -> bool:
    if order.get("reduceOnly") is True or order.get("closeOnTrigger") is True:
        return False
    remaining = _optional_number(order.get("leavesQty"))
    return remaining is None or remaining > 0


def summarize_intent_outcomes(
    status_counts: Mapping[str, int], failure_reasons: Iterable[str]
) -> dict[str, int | dict[str, int]]:
    """Separate expected no-order safety stops from stale and other failures."""
    categories = {
        "risk_budget_guard": 0,
        "ownership_guard": 0,
        "stale_request": 0,
        "minimum_order_guard": 0,
        "exchange_or_other": 0,
        "uncategorized": 0,
    }
    failed_reasons = list(failure_reasons)
    for reason in failed_reasons:
        normalized = reason.lower()
        if "market moved enough" in normalized or "risk budget" in normalized:
            category = "risk_budget_guard"
        elif "existing strategy owns" in normalized:
            category = "ownership_guard"
        elif "stale" in normalized:
            category = "stale_request"
        elif "minorderqty" in normalized or "notional too small" in normalized:
            category = "minimum_order_guard"
        elif normalized:
            category = "exchange_or_other"
        else:
            category = "uncategorized"
        categories[category] += 1

    failed_count = status_counts.get("FAILED", 0)
    if failed_count > len(failed_reasons):
        categories["uncategorized"] += failed_count - len(failed_reasons)

    return {
        "executed": status_counts.get("EXECUTED", 0),
        "failed": failed_count,
        "skipped": status_counts.get("SKIPPED", 0),
        "pending": status_counts.get("PENDING", 0),
        "failed_by_reason": categories,
    }


def _is_size_reconciled_closed(position: Mapping[str, object]) -> bool:
    if position.get("complete") is True:
        return True
    entry_qty = _optional_number(position.get("entry_qty")) or 0.0
    closed_qty = _optional_number(position.get("closed_qty")) or 0.0
    return (
        entry_qty > 0
        and abs(entry_qty - closed_qty) <= 1e-6
        and not position.get("ambiguous_rows")
        and not position.get("open_entry_orders")
    )


def _cohort_metrics(
    positions: list[Mapping[str, object]], prefix: str
) -> dict[str, object]:
    normalized = [{**p, "complete": True} for p in positions]
    perf = summarize_reconciled_positions(normalized)
    count = int(perf["position_count"])  # type: ignore[arg-type]
    base = {
        f"{prefix}_trades": count,
        f"{prefix}_winning_trades": int(perf["positive_count"]),  # type: ignore[arg-type]
        f"{prefix}_losing_trades": int(perf["negative_count"]),  # type: ignore[arg-type]
        f"{prefix}_win_rate_pct": perf["win_rate_pct"],
        f"{prefix}_net_pnl_usdt": perf["net_pnl_usdt"] if count else 0.0,
        f"{prefix}_net_r": (
            _rounded(sum(_number(p, "net_r") for p in normalized), 6) if count else 0.0
        ),
        f"{prefix}_expectancy_r": perf["expectancy_r"],
        f"{prefix}_profit_factor": perf["profit_factor"],
    }
    if prefix == "current_risk":
        base["current_risk_avg_win_usdt"] = perf["avg_win_usdt"]
        base["current_risk_avg_loss_usdt"] = perf["avg_loss_usdt"]
    return base


def _active_risk_per_trade_pct(
    database, tables: set[str], positions: Sequence[Mapping[str, object]]
) -> float | None:
    if "execution_policy" in tables:
        row = database.execute(
            "SELECT risk_per_trade_pct FROM execution_policy WHERE id=1"
        ).fetchone()
        if row is not None:
            policy_pct = _optional_number(row[0])
            if policy_pct is not None and policy_pct > 0:
                return policy_pct
    rates = [
        pct
        for p in positions
        if (pct := _optional_number(p.get("risk_per_trade_pct"))) is not None
        and pct > 0
    ]
    return max(rates) if rates else None


def _side_metrics(
    positions: list[Mapping[str, object]], side: str
) -> dict[str, object]:
    subset = [{**p, "complete": True} for p in positions if p.get("side") == side]
    perf = summarize_reconciled_positions(subset)
    count = int(perf["position_count"])  # type: ignore[arg-type]
    prefix = side.lower()
    return {
        f"{prefix}_trades": count,
        f"{prefix}_win_rate_pct": perf["win_rate_pct"],
        f"{prefix}_net_pnl_usdt": perf["net_pnl_usdt"] if count else 0.0,
        f"{prefix}_net_r": (
            _rounded(sum(_number(p, "net_r") for p in subset), 6) if count else 0.0
        ),
    }


def _channel_summary(
    *,
    total_posts: int,
    completed_posts: int,
    signal_posts: int,
    open_intents: int,
    executed_intents: int,
    skipped_intents: int,
    failed_intents: int,
    failed_ownership_guard: int,
    failed_risk_budget_guard: int,
    failed_other: int,
    lifecycle_actions: int,
    executed_actions: int,
    active_open_strategies: int,
    positions: list[Mapping[str, object]],
    size_reconciled_positions: list[Mapping[str, object]],
    current_risk_positions: list[Mapping[str, object]],
) -> dict[str, object]:
    perf = summarize_reconciled_positions(positions)
    completed_trades = int(perf["position_count"])  # type: ignore[arg-type]
    net_r_sum = (
        _rounded(sum(_number(p, "net_r") for p in positions), 6)
        if completed_trades
        else 0.0
    )
    return {
        "total_posts": total_posts,
        "completed_posts": completed_posts,
        "signal_posts": signal_posts,
        "signal_post_ratio_pct": (
            _rounded(100 * signal_posts / completed_posts, 2)
            if completed_posts
            else None
        ),
        "open_intents": open_intents,
        "executed_intents": executed_intents,
        "pickup_rate_pct": (
            _rounded(100 * executed_intents / open_intents, 2) if open_intents else None
        ),
        "skipped_intents": skipped_intents,
        "failed_intents": failed_intents,
        "failed_ownership_guard": failed_ownership_guard,
        "failed_risk_budget_guard": failed_risk_budget_guard,
        "failed_other": failed_other,
        "lifecycle_actions": lifecycle_actions,
        "executed_actions": executed_actions,
        "active_open_strategies": active_open_strategies,
        "completed_trades": completed_trades,
        "winning_trades": int(perf["positive_count"]),  # type: ignore[arg-type]
        "losing_trades": int(perf["negative_count"]),  # type: ignore[arg-type]
        "win_rate_pct": perf["win_rate_pct"],
        "net_pnl_usdt": perf["net_pnl_usdt"] if completed_trades else 0.0,
        "net_r": net_r_sum,
        "expectancy_usdt": perf["expectancy_usdt"],
        "expectancy_r": perf["expectancy_r"],
        "profit_factor": perf["profit_factor"],
        **_side_metrics(size_reconciled_positions, "LONG"),
        **_side_metrics(size_reconciled_positions, "SHORT"),
        **_cohort_metrics(size_reconciled_positions, "size_reconciled"),
        **_cohort_metrics(current_risk_positions, "current_risk"),
    }


_INTENT_STATUS_FIELDS = {
    "EXECUTED": "executed_intents",
    "SKIPPED": "skipped_intents",
    "FAILED": "failed_intents",
}


def _failed_reason_field(error: str) -> str:
    normalized = error.lower()
    if "existing strategy owns" in normalized:
        return "failed_ownership_guard"
    if "market moved enough" in normalized or "risk budget" in normalized:
        return "failed_risk_budget_guard"
    return "failed_other"


def _accumulate_channel_intents(
    database, buckets: dict[str, dict[str, object]]
) -> None:
    cols = {row[1] for row in database.execute("PRAGMA table_info(intents)")}
    err_sql = "COALESCE(error, '')" if "error" in cols else "''"
    for cid, mid, status, err in database.execute(
        "SELECT CAST(json_extract(payload_json, '$.source.channel_id') AS TEXT), "
        f"json_extract(payload_json, '$.source.message_id'), status, {err_sql} FROM intents"
    ):
        bucket = buckets.get(str(cid))
        if bucket is None:
            continue
        if mid is not None:
            bucket["signal_msgs"].add(mid)  # type: ignore[union-attr]
        bucket["open_intents"] = int(bucket["open_intents"]) + 1  # type: ignore[arg-type]
        field = _INTENT_STATUS_FIELDS.get(str(status))
        if field is not None:
            bucket[field] = int(bucket[field]) + 1  # type: ignore[arg-type]
        if status == "FAILED":
            r_field = _failed_reason_field(str(err or ""))
            bucket[r_field] = int(bucket[r_field]) + 1  # type: ignore[arg-type]


def _accumulate_channel_actions(
    database, buckets: dict[str, dict[str, object]]
) -> None:
    for cid, mid, status in database.execute(
        "SELECT CAST(json_extract(payload_json, '$.source.channel_id') AS TEXT), "
        "json_extract(payload_json, '$.source.message_id'), status FROM position_actions"
    ):
        bucket = buckets.get(str(cid))
        if bucket is None:
            continue
        if mid is not None:
            bucket["signal_msgs"].add(mid)  # type: ignore[union-attr]
        bucket["lifecycle_actions"] = int(bucket["lifecycle_actions"]) + 1  # type: ignore[arg-type]
        if status == "EXECUTED":
            bucket["executed_actions"] = int(bucket["executed_actions"]) + 1  # type: ignore[arg-type]


def _accumulate_channel_open_strategies(
    database, tables: set[str], buckets: dict[str, dict[str, object]]
) -> None:
    if "position_strategies" not in tables:
        return
    for cid, count in database.execute(
        "SELECT CAST(json_extract(i.payload_json, '$.source.channel_id') AS TEXT), "
        "COUNT(*) FROM position_strategies ps JOIN intents i "
        "ON ps.strategy_id = i.intent_id "
        "WHERE ps.status IN ('ENTERING', 'OPEN_RISK', 'PROFIT_PROTECTED', 'CLOSING') "
        "GROUP BY 1"
    ):
        bucket = buckets.get(str(cid))
        if bucket is not None:
            bucket["active_open_strategies"] = int(count or 0)


def _channel_funnel_buckets(database, tables: set[str]) -> dict[str, dict[str, object]]:
    buckets: dict[str, dict[str, object]] = {}
    for cid, total, completed in database.execute(
        "SELECT CAST(channel_id AS TEXT), COUNT(*), "
        "SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) "
        "FROM source_messages GROUP BY channel_id ORDER BY channel_id ASC"
    ):
        buckets[str(cid)] = {
            "total_posts": int(total or 0),
            "completed_posts": int(completed or 0),
            "signal_msgs": set(),
            "open_intents": 0,
            "executed_intents": 0,
            "skipped_intents": 0,
            "failed_intents": 0,
            "failed_ownership_guard": 0,
            "failed_risk_budget_guard": 0,
            "failed_other": 0,
            "lifecycle_actions": 0,
            "executed_actions": 0,
            "active_open_strategies": 0,
        }
    _accumulate_channel_intents(database, buckets)
    _accumulate_channel_actions(database, buckets)
    _accumulate_channel_open_strategies(database, tables, buckets)
    return buckets


def _bucket_summary(
    bucket: dict[str, object],
    positions: list[Mapping[str, object]],
    size_reconciled: list[Mapping[str, object]],
    current_risk: list[Mapping[str, object]],
) -> dict[str, object]:
    return _channel_summary(
        total_posts=int(bucket["total_posts"]),  # type: ignore[arg-type]
        completed_posts=int(bucket["completed_posts"]),  # type: ignore[arg-type]
        signal_posts=len(bucket["signal_msgs"]),  # type: ignore[arg-type]
        open_intents=int(bucket["open_intents"]),  # type: ignore[arg-type]
        executed_intents=int(bucket["executed_intents"]),  # type: ignore[arg-type]
        skipped_intents=int(bucket["skipped_intents"]),  # type: ignore[arg-type]
        failed_intents=int(bucket["failed_intents"]),  # type: ignore[arg-type]
        failed_ownership_guard=int(bucket["failed_ownership_guard"]),  # type: ignore[arg-type]
        failed_risk_budget_guard=int(bucket["failed_risk_budget_guard"]),  # type: ignore[arg-type]
        failed_other=int(bucket["failed_other"]),  # type: ignore[arg-type]
        lifecycle_actions=int(bucket["lifecycle_actions"]),  # type: ignore[arg-type]
        executed_actions=int(bucket["executed_actions"]),  # type: ignore[arg-type]
        active_open_strategies=int(bucket["active_open_strategies"]),  # type: ignore[arg-type]
        positions=positions,
        size_reconciled_positions=size_reconciled,
        current_risk_positions=current_risk,
    )


def _build_exit_cohorts(
    complete: list[Mapping[str, object]],
    size_reconciled: list[Mapping[str, object]],
    current_risk: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    profiles = sorted(
        {str(p.get("exit_profile") or "baseline") for p in size_reconciled}
    )
    cohorts: list[dict[str, object]] = []
    for profile in profiles:
        p_comp = [
            p for p in complete if str(p.get("exit_profile") or "baseline") == profile
        ]
        p_size = [
            p
            for p in size_reconciled
            if str(p.get("exit_profile") or "baseline") == profile
        ]
        p_cur = [
            p
            for p in current_risk
            if str(p.get("exit_profile") or "baseline") == profile
        ]
        perf = summarize_reconciled_positions(p_comp)
        comp_count = int(perf["position_count"])  # type: ignore[arg-type]
        cohorts.append(
            {
                "exit_profile": profile,
                "completed_trades": comp_count,
                "winning_trades": int(perf["positive_count"]),  # type: ignore[arg-type]
                "losing_trades": int(perf["negative_count"]),  # type: ignore[arg-type]
                "win_rate_pct": perf["win_rate_pct"],
                "net_pnl_usdt": perf["net_pnl_usdt"] if comp_count else 0.0,
                "net_r": (
                    _rounded(sum(_number(p, "net_r") for p in p_comp), 6)
                    if comp_count
                    else 0.0
                ),
                "expectancy_r": perf["expectancy_r"],
                "profit_factor": perf["profit_factor"],
                **_cohort_metrics(p_size, "size_reconciled"),
                **_cohort_metrics(p_cur, "current_risk"),
            }
        )
    cohorts.sort(
        key=lambda c: (
            -int(c["current_risk_trades"]),  # type: ignore[arg-type]
            -int(c["size_reconciled_trades"]),  # type: ignore[arg-type]
            str(c["exit_profile"]),
        )
    )
    return cohorts


_LADDER_DEPTH_ORDER = ("E1_only", "E1_E2", "E1_E2_E3_full")


def _plan_leg_thresholds(database, tables: set[str]) -> dict[str, tuple[float, float]]:
    if "execution_plans" not in tables:
        return {}
    thresholds: dict[str, tuple[float, float]] = {}
    for intent_id, payload_str in database.execute(
        "SELECT CAST(intent_id AS TEXT), payload_json FROM execution_plans"
    ):
        try:
            orders = json.loads(payload_str).get("orders") or []
            q1 = float(orders[0]["quantity"])
            q12 = q1 + (float(orders[1]["quantity"]) if len(orders) > 1 else 0.0)
            thresholds[str(intent_id)] = (q1, q12)
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return thresholds


def _classify_ladder_depth(
    position: Mapping[str, object],
    thresholds: dict[str, tuple[float, float]],
) -> str:
    explicit = position.get("ladder_depth")
    if explicit in _LADDER_DEPTH_ORDER:
        return str(explicit)
    pair = thresholds.get(str(position.get("intent_id") or ""))
    entry_qty = _optional_number(position.get("entry_qty")) or 0.0
    if pair is None or entry_qty <= 0:
        return "E1_only"
    q1, q12 = pair
    if abs(entry_qty - q1) <= max(1e-6, q1 * 0.01):
        return "E1_only"
    if abs(entry_qty - q12) <= max(1e-6, q12 * 0.01):
        return "E1_E2"
    return "E1_E2_E3_full"


def _build_ladder_cohorts(
    database,
    tables: set[str],
    complete: list[Mapping[str, object]],
    size_reconciled: list[Mapping[str, object]],
    current_risk: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    thresholds = _plan_leg_thresholds(database, tables)
    cohorts: list[dict[str, object]] = []
    for depth in _LADDER_DEPTH_ORDER:
        p_comp = [p for p in complete if _classify_ladder_depth(p, thresholds) == depth]
        p_size = [
            p for p in size_reconciled if _classify_ladder_depth(p, thresholds) == depth
        ]
        p_cur = [
            p for p in current_risk if _classify_ladder_depth(p, thresholds) == depth
        ]
        if not p_size:
            continue
        perf = summarize_reconciled_positions(p_comp)
        comp_count = int(perf["position_count"])  # type: ignore[arg-type]
        cohorts.append(
            {
                "ladder_depth": depth,
                "completed_trades": comp_count,
                "winning_trades": int(perf["positive_count"]),  # type: ignore[arg-type]
                "losing_trades": int(perf["negative_count"]),  # type: ignore[arg-type]
                "win_rate_pct": perf["win_rate_pct"],
                "net_pnl_usdt": perf["net_pnl_usdt"] if comp_count else 0.0,
                "net_r": (
                    _rounded(sum(_number(p, "net_r") for p in p_comp), 6)
                    if comp_count
                    else 0.0
                ),
                "expectancy_r": perf["expectancy_r"],
                "profit_factor": perf["profit_factor"],
                **_cohort_metrics(p_size, "size_reconciled"),
                **_cohort_metrics(p_cur, "current_risk"),
            }
        )
    return cohorts


def build_channel_analytics(
    database,
    positions: Iterable[Mapping[str, object]],
) -> dict[str, object] | None:
    """Summarize per-channel signal pickup and reconciled PnL with privacy-safe IDs."""
    tables = {
        row[0]
        for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not {"source_messages", "intents", "position_actions"} <= tables:
        return None
    buckets = _channel_funnel_buckets(database, tables)
    all_positions = list(positions)
    complete = [p for p in all_positions if p.get("complete") is True]
    size_reconciled = [p for p in all_positions if _is_size_reconciled_closed(p)]
    active_risk = _active_risk_per_trade_pct(database, tables, size_reconciled)
    current_risk = [
        p
        for p in size_reconciled
        if active_risk is not None
        and abs((_optional_number(p.get("risk_per_trade_pct")) or 0.0) - active_risk)
        <= 1e-6
    ]
    rows: list[dict[str, object]] = []
    for index, (cid, bucket) in enumerate(buckets.items(), 1):
        ch_positions = [p for p in complete if str(p.get("source_channel_id")) == cid]
        ch_size_rec = [
            p for p in size_reconciled if str(p.get("source_channel_id")) == cid
        ]
        ch_cur_risk = [
            p for p in current_risk if str(p.get("source_channel_id")) == cid
        ]
        rows.append(
            {
                "channel": f"source_{index}",
                **_bucket_summary(bucket, ch_positions, ch_size_rec, ch_cur_risk),
            }
        )
    rows.sort(
        key=lambda r: (
            -int(r["size_reconciled_trades"]),  # type: ignore[arg-type]
            -int(r["total_posts"]),  # type: ignore[arg-type]
            str(r["channel"]),
        )
    )
    known_cids = set(buckets)
    known_positions = [
        p for p in complete if str(p.get("source_channel_id")) in known_cids
    ]
    known_size_rec = [
        p for p in size_reconciled if str(p.get("source_channel_id")) in known_cids
    ]
    known_cur_risk = [
        p for p in current_risk if str(p.get("source_channel_id")) in known_cids
    ]
    total_bucket: dict[str, object] = {
        key: sum(int(b[key]) for b in buckets.values())  # type: ignore[arg-type]
        for key in (
            "total_posts",
            "completed_posts",
            "open_intents",
            "executed_intents",
            "skipped_intents",
            "failed_intents",
            "failed_ownership_guard",
            "failed_risk_budget_guard",
            "failed_other",
            "lifecycle_actions",
            "executed_actions",
            "active_open_strategies",
        )
    }
    total_bucket["signal_msgs"] = range(
        sum(len(b["signal_msgs"]) for b in buckets.values())  # type: ignore[arg-type]
    )
    return {
        "available": bool(rows),
        "rows": rows,
        "totals": _bucket_summary(
            total_bucket, known_positions, known_size_rec, known_cur_risk
        ),
        "exit_cohorts": _build_exit_cohorts(
            known_positions, known_size_rec, known_cur_risk
        ),
        "ladder_cohorts": _build_ladder_cohorts(
            database, tables, known_positions, known_size_rec, known_cur_risk
        ),
    }


def _number(position: Mapping[str, object], key: str) -> float:
    value = position.get(key)
    if not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None
