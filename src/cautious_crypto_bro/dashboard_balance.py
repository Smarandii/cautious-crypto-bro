"""Strict public balance/activity whitelist and bounded stdlib dispatch encoding."""

from __future__ import annotations

import base64
import gzip
import io
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from .dashboard_benchmark import validate_benchmark

MAX_BALANCE_BYTES = 2_000_000
MAX_ENCODED_BYTES = 48_000
HEADER = (
    "metric",
    "currency",
    "timezone",
    "includes_unrealized_pnl",
    "start",
    "as_of",
    "days",
)
DAY = ("date", "balance", "change", "complete", "activity")
ACTIVITY = (
    "at",
    "kind",
    "category",
    "symbol",
    "side",
    "quantity",
    "price",
    "cash_flow",
    "fee",
    "funding",
    "wallet_change",
)


def _fields(value: object, fields: tuple[str, ...]) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("Unexpected public balance fields")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid public balance timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("Public balance timestamps must be UTC")
    return result


def _amount(value: object) -> Decimal:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"-?\d{1,20}(?:\.\d{1,18})?", value) is None
    ):
        raise ValueError("Invalid public balance amount")
    return Decimal(value)


def _optional_amount(value: object) -> Decimal | None:
    return None if value is None else _amount(value)


def _activity(value: object, day: str, start: datetime, as_of: datetime) -> Decimal:
    row = _fields(value, ACTIVITY)
    at = _timestamp(row["at"])
    if at.date().isoformat() != day or not start <= at <= as_of:
        raise ValueError("Public activity lies outside its coverage")
    for name in ("kind", "category", "symbol"):
        text = row[name]
        if (
            not isinstance(text, str)
            or re.fullmatch(r"[A-Za-z0-9_\-]{0,80}", text) is None
        ):
            raise ValueError("Invalid public activity label")
    if row["side"] not in ("Buy", "Sell", "None", ""):
        raise ValueError("Invalid public execution side")
    amounts = {name: _amount(row[name]) for name in ACTIVITY[5:]}
    # Bybit spot transaction quantities may be signed currency movements.
    if amounts["price"] < 0:
        raise ValueError("Negative public execution price")
    if (
        amounts["wallet_change"]
        != amounts["cash_flow"] + amounts["funding"] - amounts["fee"]
    ):
        raise ValueError("Public activity components disagree")
    return amounts["wallet_change"]


def _day(
    value: object, start: datetime, as_of: datetime
) -> tuple[date, Decimal | None]:
    day = _fields(value, DAY)
    if not isinstance(day["date"], str):
        raise ValueError("Invalid public balance date")
    when = date.fromisoformat(day["date"])
    if when.isoformat() != day["date"] or not start.date() <= when <= as_of.date():
        raise ValueError("Public balance day lies outside its coverage")
    balance = _optional_amount(day["balance"])
    change = _optional_amount(day["change"])
    if balance is None and change is not None:
        raise ValueError("Unknown balance cannot have a known day change")
    if type(day["complete"]) is not bool:
        raise ValueError("Invalid public day completeness")
    if day["complete"] and (
        balance is None
        or when >= as_of.date()
        or datetime.combine(when, datetime.min.time(), UTC) < start
    ):
        raise ValueError("Public day cannot be complete")
    if not isinstance(day["activity"], list) or len(day["activity"]) > 10_000:
        raise ValueError("Invalid public day activity")
    total = sum(
        (_activity(row, day["date"], start, as_of) for row in day["activity"]),
        Decimal(0),
    )
    if change is not None and change != total:
        raise ValueError("Public day cash change does not match its activity")
    return when, balance


def validate_balance_history(value: object) -> dict:
    history = _fields(value, HEADER)
    if (history["metric"], history["currency"], history["timezone"]) != (
        "USDT wallet balance",
        "USDT",
        "UTC",
    ) or history["includes_unrealized_pnl"] is not False:
        raise ValueError("Unsupported public balance semantics")
    start, as_of = _timestamp(history["start"]), _timestamp(history["as_of"])
    if not start < as_of or as_of - start > timedelta(days=90):
        raise ValueError("Invalid public balance coverage")
    days = history["days"]
    if not isinstance(days, list) or not 1 <= len(days) <= 91:
        raise ValueError("Invalid public balance days")
    expected = start.date()
    previous_balance = None
    for raw_day in days:
        day = _fields(raw_day, DAY)
        when, balance = _day(day, start, as_of)
        if when != expected:
            raise ValueError("Public balance dates must be consecutive")
        change = _optional_amount(day["change"])
        if (
            previous_balance is not None
            and balance is not None
            and change is not None
            and balance - previous_balance != change
        ):
            raise ValueError("Public consecutive balances disagree")
        previous_balance = balance
        expected += timedelta(days=1)
    if expected != as_of.date() + timedelta(days=1):
        raise ValueError("Public balance series omits its capture day")
    if len(json.dumps(history, allow_nan=False).encode()) > MAX_BALANCE_BYTES:
        raise ValueError("Public balance history exceeds its size budget")
    return history


def validate_balance_feed(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] not in (1, 2)
    ):
        raise ValueError("Unsupported balance feed version")
    fields = ("schema_version", "refresh_failed", "history")
    if value["schema_version"] == 2:
        fields += ("benchmark",)
    feed = _fields(value, fields)
    if feed["schema_version"] == 2:
        validate_benchmark(feed["benchmark"])
    if type(feed["refresh_failed"]) is not bool:
        raise ValueError("Invalid balance refresh status")
    if feed["history"] is not None:
        validate_balance_history(feed["history"])
    elif not feed["refresh_failed"]:
        raise ValueError("Missing history cannot claim a successful balance refresh")
    if len(json.dumps(feed, allow_nan=False).encode()) > MAX_BALANCE_BYTES:
        raise ValueError("Public balance feed exceeds its size budget")
    return feed


def encode_balance_feed(value: object) -> str:
    feed = validate_balance_feed(value)
    raw = json.dumps(feed, separators=(",", ":"), allow_nan=False).encode()
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    if len(encoded) > MAX_ENCODED_BYTES:
        raise ValueError("Complete balance history exceeds dispatch budget")
    return encoded


def decode_balance_feed(encoded: str) -> dict:
    if len(encoded) > MAX_ENCODED_BYTES:
        raise ValueError("Balance dispatch exceeds its size budget")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_BALANCE_BYTES + 1)
        if len(raw) > MAX_BALANCE_BYTES:
            raise ValueError("Expanded balance payload exceeds its size budget")
        return validate_balance_feed(json.loads(raw))
    except (OSError, UnicodeError, ValueError, EOFError):
        raise ValueError("Invalid public balance dispatch; input withheld") from None
