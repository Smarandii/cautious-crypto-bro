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
    feed = _fields(feed, ("schema_version", "generated_at", "risk", "trade_history"))
    if type(feed["schema_version"]) is not int or feed["schema_version"] != 1:
        raise ValueError("Unsupported public feed schema")
    _timestamp(feed["generated_at"], optional=False)
    _validate_risk(feed["risk"])
    _validate_history(feed["trade_history"])
    if len(json.dumps(feed, allow_nan=False).encode("utf-8")) > 60000:
        raise ValueError("Public feed exceeds dispatch payload budget")
    return feed


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
        r"[A-Z0-9]{2,40}USDT", row["symbol"]
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
    return validate_public_feed(
        {
            "schema_version": 1,
            "generated_at": snapshot["generated_at"],
            "risk": {field: risk.get(field) for field in RISK_FIELDS},
            "trade_history": snapshot["trade_history"],
        }
    )
