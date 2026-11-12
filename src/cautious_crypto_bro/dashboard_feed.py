"""Strict, bounded public contract for the independently refreshed trade feed."""

from __future__ import annotations

import base64
import gzip
import io
import json
import math
import re
from datetime import datetime

from .dashboard_metrics import ENTRY_FAILURE_REASONS

MAX_PUBLIC_FEED_BYTES = 60_000
MAX_ENCODED_PUBLIC_FEED_BYTES = 48_000

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
CHANNEL_CUR_RISK_MAGNITUDE_FIELDS = (
    "current_risk_avg_win_usdt",
    "current_risk_avg_loss_usdt",
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
CHANNEL_SUMMARY_FIELDS_V5 = (
    *CHANNEL_SUMMARY_FIELDS_V4,
    *CHANNEL_CUR_RISK_MAGNITUDE_FIELDS,
)
CHANNEL_SUMMARY_FIELDS_V6 = (
    *CHANNEL_SUMMARY_FIELDS_V5,
    "active_open_strategies",
)
CHANNEL_ROW_FIELDS = ("channel", *CHANNEL_SUMMARY_FIELDS)
CHANNEL_ROW_FIELDS_V2 = ("channel", *CHANNEL_SUMMARY_FIELDS_V2)
CHANNEL_ROW_FIELDS_V3 = ("channel", *CHANNEL_SUMMARY_FIELDS_V3)
CHANNEL_ROW_FIELDS_V4 = ("channel", *CHANNEL_SUMMARY_FIELDS_V4)
CHANNEL_ROW_FIELDS_V5 = ("channel", *CHANNEL_SUMMARY_FIELDS_V5)
CHANNEL_ROW_FIELDS_V6 = ("channel", *CHANNEL_SUMMARY_FIELDS_V6)
EXIT_COHORT_INT_FIELDS = (
    "completed_trades",
    "winning_trades",
    "losing_trades",
    *CHANNEL_SIZE_REC_INT_FIELDS,
    *CHANNEL_CUR_RISK_INT_FIELDS,
)
EXIT_COHORT_PCT_FIELDS = (
    "win_rate_pct",
    *CHANNEL_SIZE_REC_PCT_FIELDS,
    *CHANNEL_CUR_RISK_PCT_FIELDS,
)
EXIT_COHORT_SIGNED_FIELDS = (
    "net_pnl_usdt",
    "net_r",
    "expectancy_r",
    *CHANNEL_SIZE_REC_SIGNED_FIELDS,
    *CHANNEL_CUR_RISK_SIGNED_FIELDS,
    *CHANNEL_CUR_RISK_MAGNITUDE_FIELDS,
)
EXIT_COHORT_FIELDS = (
    "exit_profile",
    *EXIT_COHORT_INT_FIELDS,
    *EXIT_COHORT_PCT_FIELDS,
    *EXIT_COHORT_SIGNED_FIELDS,
    "profit_factor",
    "size_reconciled_profit_factor",
    "current_risk_profit_factor",
)
LADDER_COHORT_FIELDS = (
    "ladder_depth",
    *EXIT_COHORT_INT_FIELDS,
    *EXIT_COHORT_PCT_FIELDS,
    *EXIT_COHORT_SIGNED_FIELDS,
    "profit_factor",
    "size_reconciled_profit_factor",
    "current_risk_profit_factor",
)
SETUP_COHORT_FIELDS = (
    "setup",
    *EXIT_COHORT_INT_FIELDS,
    *EXIT_COHORT_PCT_FIELDS,
    *EXIT_COHORT_SIGNED_FIELDS,
    "profit_factor",
    "size_reconciled_profit_factor",
    "current_risk_profit_factor",
)
LADDER_DEPTHS = {"E1_only", "E1_E2", "E1_E2_E3_full"}
SIGNAL_SETUPS = {
    "MARKET_TRADER",
    "MARKET_POLICY",
    "LIMIT_TRADER",
    "LIMIT_POLICY",
    "RANGE_TRADER",
    "RANGE_POLICY",
}
DECISION_COHORT_INT_FIELDS = (
    "audited_posts",
    "non_signal_commentary",
    "normalized_actionable",
    "update_existing_recap",
    "evidence_filtered",
    "planning_rejected",
)
DECISION_COHORT_FIELDS = (
    "channel",
    *DECISION_COHORT_INT_FIELDS,
    "actionable_ratio_pct",
)
CHANNEL_EXIT_COHORT_FIELDS = ("channel", *EXIT_COHORT_FIELDS)
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
    if "current_risk_avg_win_usdt" in summary:
        signed_fields.extend(CHANNEL_CUR_RISK_MAGNITUDE_FIELDS)
    if "active_open_strategies" in summary:
        int_fields.append("active_open_strategies")
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
    if "failed_by_reason" in summary:
        _validate_entry_failure_counts(
            summary["failed_by_reason"], summary["failed_intents"]
        )


def _validate_entry_failure_counts(value: object, failed_intents: int) -> None:
    counts = _fields(value, ENTRY_FAILURE_REASONS)
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise ValueError("Invalid public entry failure reason count")
    if sum(counts.values()) != failed_intents:
        raise ValueError("Entry failure reason counts do not match the failure total")


def _expected_channel_fields(item: object, *, with_channel: bool) -> tuple[str, ...]:
    if isinstance(item, dict) and "active_open_strategies" in item:
        fields = CHANNEL_ROW_FIELDS_V6 if with_channel else CHANNEL_SUMMARY_FIELDS_V6
        return (*fields, "failed_by_reason") if "failed_by_reason" in item else fields
    if isinstance(item, dict) and "current_risk_avg_win_usdt" in item:
        return CHANNEL_ROW_FIELDS_V5 if with_channel else CHANNEL_SUMMARY_FIELDS_V5
    if isinstance(item, dict) and "current_risk_trades" in item:
        return CHANNEL_ROW_FIELDS_V4 if with_channel else CHANNEL_SUMMARY_FIELDS_V4
    if isinstance(item, dict) and "size_reconciled_trades" in item:
        return CHANNEL_ROW_FIELDS_V3 if with_channel else CHANNEL_SUMMARY_FIELDS_V3
    if isinstance(item, dict) and "long_trades" in item:
        return CHANNEL_ROW_FIELDS_V2 if with_channel else CHANNEL_SUMMARY_FIELDS_V2
    return CHANNEL_ROW_FIELDS if with_channel else CHANNEL_SUMMARY_FIELDS


def _validate_cohort_numbers(row: dict) -> None:
    for field in EXIT_COHORT_INT_FIELDS:
        if type(row[field]) is not int or row[field] < 0:
            raise ValueError("Invalid public exit cohort integer count")
    for field in EXIT_COHORT_PCT_FIELDS:
        _number(row[field], nonnegative=True)
        if row[field] is not None and row[field] > 100:
            raise ValueError("Public exit cohort percentage exceeds 100")
    for pf in (
        "profit_factor",
        "size_reconciled_profit_factor",
        "current_risk_profit_factor",
    ):
        _number(row[pf], nonnegative=True)
    for field in EXIT_COHORT_SIGNED_FIELDS:
        _number(row[field])


def _validate_exit_cohorts(cohorts: object) -> None:
    if not isinstance(cohorts, list) or len(cohorts) > 20:
        raise ValueError("Invalid public exit cohorts")
    seen: set[str] = set()
    for item in cohorts:
        row = _fields(item, EXIT_COHORT_FIELDS)
        profile = row["exit_profile"]
        if (
            not isinstance(profile, str)
            or profile not in EXIT_PROFILES
            or profile in seen
        ):
            raise ValueError("Invalid or duplicate public exit profile cohort")
        seen.add(profile)
        _validate_cohort_numbers(row)


def _validate_ladder_cohorts(cohorts: object) -> None:
    if not isinstance(cohorts, list) or len(cohorts) > 10:
        raise ValueError("Invalid public ladder cohorts")
    seen: set[str] = set()
    for item in cohorts:
        row = _fields(item, LADDER_COHORT_FIELDS)
        depth = row["ladder_depth"]
        if not isinstance(depth, str) or depth not in LADDER_DEPTHS or depth in seen:
            raise ValueError("Invalid or duplicate public ladder depth cohort")
        seen.add(depth)
        _validate_cohort_numbers(row)


def _validate_setup_cohorts(cohorts: object) -> None:
    if not isinstance(cohorts, list) or len(cohorts) > 10:
        raise ValueError("Invalid public setup cohorts")
    seen: set[str] = set()
    for item in cohorts:
        row = _fields(item, SETUP_COHORT_FIELDS)
        setup = row["setup"]
        if not isinstance(setup, str) or setup not in SIGNAL_SETUPS or setup in seen:
            raise ValueError("Invalid or duplicate public signal setup cohort")
        seen.add(setup)
        _validate_cohort_numbers(row)


def _validate_decision_cohorts(cohorts: object) -> None:
    if not isinstance(cohorts, list) or len(cohorts) > 50:
        raise ValueError("Invalid public decision cohorts")
    seen: set[str] = set()
    for item in cohorts:
        row = _fields(item, DECISION_COHORT_FIELDS)
        channel = row["channel"]
        if (
            not isinstance(channel, str)
            or not re.fullmatch(r"source_[1-9][0-9]{0,2}", channel)
            or channel in seen
        ):
            raise ValueError("Invalid or duplicate public decision channel")
        seen.add(channel)
        for field in DECISION_COHORT_INT_FIELDS:
            if type(row[field]) is not int or row[field] < 0:
                raise ValueError("Invalid public decision integer count")
        if (
            row["non_signal_commentary"]
            + row["normalized_actionable"]
            + row["update_existing_recap"]
            + row["evidence_filtered"]
            != row["audited_posts"]
        ):
            raise ValueError("Public decision disposition counts do not sum to total")
        _number(row["actionable_ratio_pct"], nonnegative=True)
        if (
            row["actionable_ratio_pct"] is not None
            and row["actionable_ratio_pct"] > 100
        ):
            raise ValueError("Public decision percentage exceeds 100")


def _validate_channel_exit_cohorts(cohorts: object) -> None:
    if not isinstance(cohorts, list) or len(cohorts) > 100:
        raise ValueError("Invalid public channel-exit cohorts")
    seen: set[tuple[str, str]] = set()
    for item in cohorts:
        row = _fields(item, CHANNEL_EXIT_COHORT_FIELDS)
        channel = row["channel"]
        profile = row["exit_profile"]
        if (
            not isinstance(channel, str)
            or not re.fullmatch(r"source_[1-9][0-9]{0,2}", channel)
            or not isinstance(profile, str)
            or profile not in EXIT_PROFILES
            or (channel, profile) in seen
        ):
            raise ValueError("Invalid or duplicate public channel-exit cohort")
        seen.add((channel, profile))
        _validate_cohort_numbers(row)


def _validate_optional_analytics_cohorts(value: object) -> dict:
    extra = (
        tuple(
            key
            for key in (
                "exit_cohorts",
                "ladder_cohorts",
                "setup_cohorts",
                "decision_cohorts",
                "channel_exit_cohorts",
            )
            if key in value
        )
        if isinstance(value, dict)
        else ()
    )
    analytics = _fields(value, (*CHANNEL_ANALYTICS_FIELDS, *extra))
    for key, validator in (
        ("exit_cohorts", _validate_exit_cohorts),
        ("ladder_cohorts", _validate_ladder_cohorts),
        ("setup_cohorts", _validate_setup_cohorts),
        ("decision_cohorts", _validate_decision_cohorts),
        ("channel_exit_cohorts", _validate_channel_exit_cohorts),
    ):
        if key in analytics:
            validator(analytics[key])
    return analytics


def _validate_channel_analytics(value: object) -> None:
    analytics = _validate_optional_analytics_cohorts(value)
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
    fields = RISK_FIELDS
    if isinstance(value, dict) and "long_exit_control_fraction" in value:
        fields = (*fields, "long_exit_control_fraction")
    risk = _fields(value, fields)
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
    control = risk.get("long_exit_control_fraction")
    _number(control, nonnegative=True)
    if control is not None and control > 1:
        raise ValueError("Invalid LONG exit control fraction")


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
    public_risk = {field: risk.get(field) for field in RISK_FIELDS}
    if "long_exit_control_fraction" in risk:
        public_risk["long_exit_control_fraction"] = risk["long_exit_control_fraction"]
    payload: dict[str, object] = {
        "schema_version": 1,
        "generated_at": snapshot["generated_at"],
        "risk": public_risk,
        "trade_history": snapshot["trade_history"],
    }
    if snapshot.get("channel_analytics") is not None:
        payload["channel_analytics"] = snapshot["channel_analytics"]
    return validate_public_feed(payload)


def encode_public_feed(value: object) -> str:
    """Compress and base64-encode a validated public trade feed for dispatch."""
    feed = validate_public_feed(value)
    raw = json.dumps(feed, separators=(",", ":"), allow_nan=False).encode("utf-8")
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    if len(encoded) > MAX_ENCODED_PUBLIC_FEED_BYTES:
        raise ValueError("Encoded public trade feed exceeds dispatch budget")
    return encoded


def decode_public_feed(raw: str) -> dict:
    """Accept either raw JSON or gzip+base64 public trade feed and validate it."""
    if len(raw.encode("utf-8")) > MAX_PUBLIC_FEED_BYTES:
        raise ValueError("Trade feed exceeds public payload limit")
    stripped = raw.strip()
    if stripped.startswith("{"):
        return validate_public_feed(json.loads(stripped))
    try:
        compressed = base64.b64decode(stripped, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            expanded = stream.read(MAX_PUBLIC_FEED_BYTES + 1)
        if len(expanded) > MAX_PUBLIC_FEED_BYTES:
            raise ValueError("Expanded public trade feed exceeds size budget")
        return validate_public_feed(json.loads(expanded.decode("utf-8")))
    except (OSError, UnicodeError, ValueError, EOFError) as exc:
        raise ValueError("Public trade feed validation failed") from exc
