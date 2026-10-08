"""Metrics for complete, reconciled strategy positions shown in the dashboard."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from statistics import fmean


def summarize_reconciled_positions(
    positions: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Summarize only whole positions with reconciled exchange P&L and fills."""
    complete = [position for position in positions if position.get("complete") is True]
    pnl = [_number(position, "net_pnl") for position in complete]
    net_r = [_number(position, "net_r") for position in complete]
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
        "avg_win_r": _rounded(fmean(winning_r), 6) if winning_r else None,
        "avg_loss_r": _rounded(fmean(losing_r), 6) if losing_r else None,
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
