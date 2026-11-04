"""Stdlib-only public BTC benchmark contract, also used by the Pages installer."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

BENCHMARK_START = datetime(2026, 10, 4, tzinfo=UTC)
_AMOUNT = re.compile(r"\d{1,20}(?:\.\d{1,18})?")


def _positive(value: object) -> Decimal:
    if not isinstance(value, str) or _AMOUNT.fullmatch(value) is None:
        raise ValueError("Invalid BTC benchmark amount")
    result = Decimal(value)
    if result <= 0:
        raise ValueError("BTC benchmark amounts must be positive")
    return result


def _value(capital: str, entry: str, price: str) -> str:
    with localcontext() as context:
        context.prec = 60
        value = _positive(capital) * _positive(price) / _positive(entry)
        return format(value.quantize(Decimal("0.00000001")), "f")


def _benchmark_day(day: object, capital: str, entry: str, as_of: datetime) -> date:
    if not isinstance(day, dict) or set(day) != {"date", "price", "value", "complete"}:
        raise ValueError("Unexpected BTC benchmark day")
    current = date.fromisoformat(day["date"])
    if current.isoformat() != day["date"]:
        raise ValueError("BTC benchmark date must use canonical ISO format")
    if not BENCHMARK_START.date() <= current <= as_of.date():
        raise ValueError("BTC benchmark day outside capture")
    if type(day["complete"]) is not bool or day["complete"] != (current < as_of.date()):
        raise ValueError("BTC benchmark completion disagrees with capture")
    if day["value"] != _value(capital, entry, day["price"]):
        raise ValueError("BTC benchmark value disagrees with fixed holding")
    return current


def _benchmark_capture(history: object) -> datetime:
    if not isinstance(history, dict) or set(history) != {
        "start",
        "source",
        "fee_basis",
        "capital",
        "entry_price",
        "as_of",
        "days",
    }:
        raise ValueError("Unexpected BTC benchmark history")
    if (
        history["start"] != BENCHMARK_START.isoformat()
        or history["source"] != "BYBIT_SPOT_BTCUSDT"
        or history["fee_basis"] != "GROSS_NO_SUBSEQUENT_FLOWS"
    ):
        raise ValueError("Unsupported BTC benchmark semantics")
    _positive(history["capital"])
    _positive(history["entry_price"])
    if not isinstance(history["as_of"], str) or len(history["as_of"]) > 40:
        raise ValueError("Invalid BTC benchmark capture timestamp")
    as_of = datetime.fromisoformat(history["as_of"])
    if as_of.tzinfo is None or as_of.utcoffset() != timedelta(0):
        raise ValueError("BTC benchmark timestamp must be UTC")
    if as_of < BENCHMARK_START:
        raise ValueError("BTC benchmark capture precedes purchase")
    return as_of


def validate_benchmark(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"refresh_failed", "history"}:
        raise ValueError("Unexpected BTC benchmark fields")
    if type(value["refresh_failed"]) is not bool:
        raise ValueError("Invalid BTC benchmark refresh state")
    history = value["history"]
    if history is None:
        if not value["refresh_failed"]:
            raise ValueError("Unavailable benchmark cannot claim a successful refresh")
        return value
    as_of = _benchmark_capture(history)
    capital, entry = history["capital"], history["entry_price"]
    days = history["days"]
    if not isinstance(days, list) or not 1 <= len(days) <= 31:
        raise ValueError("Invalid BTC benchmark day coverage")
    expected = max(BENCHMARK_START.date(), (as_of - timedelta(days=30)).date())
    for day in days:
        current = _benchmark_day(day, capital, entry, as_of)
        if current != expected:
            raise ValueError("BTC benchmark price gap")
        expected = current + timedelta(days=1)
    if expected != as_of.date() + timedelta(days=1):
        raise ValueError("BTC benchmark lacks its current endpoint")
    return value
