"""Strict, bounded public contract for the independently refreshed trade feed."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime

RISK_FIELDS = (
    "risk_per_trade_pct",
    "long_risk_multiplier",
    "long_momentum_gate_enabled",
    "long_participation_skip_fraction",
    "portfolio_stop_risk_cap_usdt",
    "exit_profile",
)
TRADE_FIELDS = (
    "symbol",
    "side",
    "quantity",
    "net_pnl_usdt",
    "net_r",
    "closed_at",
    "risk_per_trade_pct",
)
HISTORY_FIELDS = (
    "available",
    "rows",
    "total_completed",
    "omitted_count",
    "limit",
    "as_of",
)
CHANNEL_INT_FIELDS = (
    "total_posts",
    "completed_posts",
    "signal_posts",
    "open_intents",
    "executed_intents",
    "skipped_intents",
    "failed_intents",
    "lifecycle_actions",
    "executed_actions",
    "completed_trades",
    "winning_trades",
    "losing_trades",
)
CHANNEL_EXTENDED_INT_FIELDS = (
    "failed_ownership_guard",
    "failed_risk_budget_guard",
    "failed_other",
    "long_trades",
    "short_trades",
)
CHANNEL_EXTENDED_PCT_FIELDS = (
    "long_win_rate_pct",
    "short_win_rate_pct",
)
CHANNEL_EXTENDED_SIGNED_FIELDS = (
    "long_net_pnl_usdt",
    "long_net_r",
    "short_net_pnl_usdt",
    "short_net_r",
)
CHANNEL_EXTENDED_FIELDS = (
    *CHANNEL_EXTENDED_INT_FIELDS,
    *CHANNEL_EXTENDED_PCT_FIELDS,
    *CHANNEL_EXTENDED_SIGNED_FIELDS,
)
CHANNEL_SIZE_REC_INT_FIELDS = (
    "size_reconciled_trades",
    "size_reconciled_winning_trades",
    "size_reconciled_losing_trades",
)
CHANNEL_SIZE_REC_PCT_FIELDS = ("size_reconciled_win_rate_pct",)
CHANNEL_SIZE_REC_SIGNED_FIELDS = (
    "size_reconciled_net_pnl_usdt",
    "size_reconciled_net_r",
    "size_reconciled_expectancy_r",
)
CHANNEL_SIZE_REC_FIELDS = (
    *CHANNEL_SIZE_REC_INT_FIELDS,
    *CHANNEL_SIZE_REC_PCT_FIELDS,
    *CHANNEL_SIZE_REC_SIGNED_FIELDS,
    "size_reconciled_profit_factor",
)
CHANNEL_CUR_RISK_INT_FIELDS = (
    "current_risk_trades",
    "current_risk_winning_trades",
    "current_risk_losing_trades",
)
CHANNEL_CUR_RISK_PCT_FIELDS = ("current_risk_win_rate_pct",)
CHANNEL_CUR_RISK_SIGNED_FIELDS = (
    "current_risk_net_pnl_usdt",
    "current_risk_net_r",
    "current_risk_expectancy_r",
)
CHANNEL_CUR_RISK_FIELDS = (
    *CHANNEL_CUR_RISK_INT_FIELDS,
    *CHANNEL_CUR_RISK_PCT_FIELDS,
    *CHANNEL_CUR_RISK_SIGNED_FIELDS,
    "current_risk_profit_factor",
)
CHANNEL_SUMMARY_FIELDS = (
    *CHANNEL_INT_FIELDS,
    "signal_post_ratio_pct",
    "pickup_rate_pct",
    "win_rate_pct",
    "net_pnl_usdt",
    "net_r",
    "expectancy_usdt",
    "expectancy_r",
    "profit_factor",
)
CHANNEL_SUMMARY_FIELDS_V2 = (*CHANNEL_SUMMARY_FIELDS, *CHANNEL_EXTENDED_FIELDS)
CHANNEL_SUMMARY_FIELDS_V3 = (
    *CHANNEL_SUMMARY_FIELDS_V2,
    *CHANNEL_SIZE_REC_FIELDS,
)
CHANNEL_SUMMARY_FIELDS_V4 = (
    *CHANNEL_SUMMARY_FIELDS_V3,
    *CHANNEL_CUR_RISK_FIELDS,
)
CHANNEL_ROW_FIELDS = ("channel", *CHANNEL_SUMMARY_FIELDS)
CHANNEL_ROW_FIELDS_V2 = ("channel", *CHANNEL_SUMMARY_FIELDS_V2)
CHANNEL_ROW_FIELDS_V3 = ("channel", *CHANNEL_SUMMARY_FIELDS_V3)
CHANNEL_ROW_FIELDS_V4 = ("channel", *CHANNEL_SUMMARY_FIELDS_V4)
CHANNEL_ANALYTICS_FIELDS = ("available", "rows", "totals")
EXIT_PROFILES = {
    "baseline",
    "payoff_challenger",
    "payoff_early_trail",
    "payoff_early_tight_trail",
    "payoff_early_tight_trail_long_015",
    "payoff_early_tight_trail_long_ab_015",
    "payoff_early_tight_trail_long_ab_020_control",
}


def _fields(value: object, fields: tuple[str, ...]) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("Unexpected public feed fields")
    return value


def _number(value: object, *, nonnegative: bool = False) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("Invalid public feed number")
    if nonnegative and value < 0:
        raise ValueError("Negative public feed quantity or risk")


def _timestamp(value: object, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid public feed timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Public feed timestamps require a timezone")


def validate_public_feed(feed: object) -> dict:
    """Reject extra fields instead of silently publishing arbitrary input."""
    base_fields = ("schema_version", "generated_at", "risk", "trade_history")
    if isinstance(feed, dict) and "channel_analytics" in feed:
        feed = _fields(feed, (*base_fields, "channel_analytics"))
        _validate_channel_analytics(feed["channel_analytics"])
    else:
        feed = _fields(feed, base_fields)
    if type(feed["schema_version"]) is not int or feed["schema_version"] != 1:
        raise ValueError("Unsupported public feed schema")
    _timestamp(feed["generated_at"], optional=False)
    _validate_risk(feed["risk"])
    _validate_history(feed["trade_history"])
    if len(json.dumps(feed, allow_nan=False).encode("utf-8")) > 60000:
        raise ValueError("Public feed exceeds dispatch payload budget")
    return feed


def _channel_field_sets(
    summary: dict,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    int_fields: list[str] = list(CHANNEL_INT_FIELDS)
    pct_fields: list[str] = ["signal_post_ratio_pct", "pickup_rate_pct", "win_rate_pct"]
    signed_fields: list[str] = [
        "net_pnl_usdt",
        "net_r",
        "expectancy_usdt",
        "expectancy_r",
    ]
    if "long_trades" in summary:
        int_fields.extend(CHANNEL_EXTENDED_INT_FIELDS)
        pct_fields.extend(CHANNEL_EXTENDED_PCT_FIELDS)
        signed_fields.extend(CHANNEL_EXTENDED_SIGNED_FIELDS)
    if "size_reconciled_trades" in summary:
        int_fields.extend(CHANNEL_SIZE_REC_INT_FIELDS)
        pct_fields.extend(CHANNEL_SIZE_REC_PCT_FIELDS)
        signed_fields.extend(CHANNEL_SIZE_REC_SIGNED_FIELDS)
    if "current_risk_trades" in summary:
        int_fields.extend(CHANNEL_CUR_RISK_INT_FIELDS)
        pct_fields.extend(CHANNEL_CUR_RISK_PCT_FIELDS)
        signed_fields.extend(CHANNEL_CUR_RISK_SIGNED_FIELDS)
    return tuple(int_fields), tuple(pct_fields), tuple(signed_fields)


def _validate_channel_summary(summary: dict) -> None:
    int_fields, pct_fields, signed_fields = _channel_field_sets(summary)
    for field in int_fields:
        if type(summary[field]) is not int or summary[field] < 0:
            raise ValueError("Invalid public channel integer count")
    for field in pct_fields:
        _number(summary[field], nonnegative=True)
        if summary[field] is not None and summary[field] > 100:
            raise ValueError("Public channel percentage exceeds 100")
    for pf_field in (
        "profit_factor",
        "size_reconciled_profit_factor",
        "current_risk_profit_factor",
    ):
        if pf_field in summary:
            _number(summary[pf_field], nonnegative=True)
    for field in signed_fields:
        _number(summary[field])


def _expected_channel_fields(item: object, *, with_channel: bool) -> tuple[str, ...]:
    if isinstance(item, dict) and "current_risk_trades" in item:
        return CHANNEL_ROW_FIELDS_V4 if with_channel else CHANNEL_SUMMARY_FIELDS_V4
    if isinstance(item, dict) and "size_reconciled_trades" in item:
        return CHANNEL_ROW_FIELDS_V3 if with_channel else CHANNEL_SUMMARY_FIELDS_V3
    if isinstance(item, dict) and "long_trades" in item:
        return CHANNEL_ROW_FIELDS_V2 if with_channel else CHANNEL_SUMMARY_FIELDS_V2
    return CHANNEL_ROW_FIELDS if with_channel else CHANNEL_SUMMARY_FIELDS


def _validate_channel_analytics(value: object) -> None:
    analytics = _fields(value, CHANNEL_ANALYTICS_FIELDS)
    if type(analytics["available"]) is not bool:
        raise ValueError("Invalid channel analytics availability")
    rows = analytics["rows"]
    if not isinstance(rows, list) or len(rows) > 50:
        raise ValueError("Invalid public channel analytics rows")
    seen: set[str] = set()
    for item in rows:
        row = _fields(item, _expected_channel_fields(item, with_channel=True))
        channel = row["channel"]
        if (
            not isinstance(channel, str)
            or not re.fullmatch(r"source_[1-9][0-9]{0,2}", channel)
            or channel in seen
        ):
            raise ValueError("Invalid or duplicate public channel identifier")
        seen.add(channel)
        _validate_channel_summary(row)
    raw_totals = analytics["totals"]
    totals = _fields(
        raw_totals, _expected_channel_fields(raw_totals, with_channel=False)
    )
    _validate_channel_summary(totals)


def _validate_risk(value: object) -> None:
    risk = _fields(value, RISK_FIELDS)
    for field in RISK_FIELDS[:2] + RISK_FIELDS[3:5]:
        _number(risk[field], nonnegative=True)
    if (
        risk["long_momentum_gate_enabled"] is not None
        and type(risk["long_momentum_gate_enabled"]) is not bool
    ):
        raise ValueError("Invalid momentum gate flag")
    if risk["exit_profile"] is not None and risk["exit_profile"] not in EXIT_PROFILES:
        raise ValueError("Unknown public exit profile")
    skip = risk["long_participation_skip_fraction"]
    if skip is not None and skip > 1:
        raise ValueError("Invalid participation fraction")


def _validate_history(value: object) -> None:
    history = _fields(value, HISTORY_FIELDS)
    _timestamp(history["as_of"])
    if type(history["available"]) is not bool:
        raise ValueError("Invalid evidence availability")
    if type(history["limit"]) is not int or not 1 <= history["limit"] <= 100:
        raise ValueError("Invalid public trade limit")
    rows = history["rows"]
    if not isinstance(rows, list) or len(rows) > history["limit"]:
        raise ValueError("Public feed exceeds trade limit")
    if history["available"]:
        for field in ("total_completed", "omitted_count"):
            if type(history[field]) is not int or history[field] < 0:
                raise ValueError("Invalid public trade count")
        if history["total_completed"] != len(rows) + history["omitted_count"]:
            raise ValueError("Public trade count mismatch")
    elif (
        rows
        or history["total_completed"] is not None
        or history["omitted_count"] is not None
    ):
        raise ValueError("Unavailable evidence cannot contain trades")
    for row in rows:
        _validate_trade(row)


def _validate_trade(value: object) -> None:
    row = _fields(value, TRADE_FIELDS)
    if not isinstance(row["symbol"], str) or not re.fullmatch(
        r"[A-Z0-9]{1,40}USDT", row["symbol"]
    ):
        raise ValueError("Invalid public trade symbol")
    if row["side"] not in ("LONG", "SHORT"):
        raise ValueError("Invalid public trade side")
    for field in ("quantity", "risk_per_trade_pct"):
        _number(row[field], nonnegative=True)
    for field in ("net_pnl_usdt", "net_r"):
        _number(row[field])
    _timestamp(row["closed_at"])


def public_feed_from_snapshot(snapshot: dict) -> dict:
    """Project private/local aggregate context down to the explicit public contract."""
    risk = snapshot.get("risk") or {}
    payload: dict[str, object] = {
        "schema_version": 1,
        "generated_at": snapshot["generated_at"],
        "risk": {field: risk.get(field) for field in RISK_FIELDS},
        "trade_history": snapshot["trade_history"],
    }
    if snapshot.get("channel_analytics") is not None:
        payload["channel_analytics"] = snapshot["channel_analytics"]
    return validate_public_feed(payload)
