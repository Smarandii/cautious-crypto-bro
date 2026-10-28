"""Prior-direction momentum classifier shared by runtime gating and review.

The review analyzer (scripts/analyze_long_participation.py) keeps its own
frozen copy of this logic for reproducibility of the #183 checkpoint; the
parity test pins the two together.
"""

from __future__ import annotations

from decimal import Decimal

MOMENTUM_LOOKBACK_MINUTES = 60

Stratum = str  # "keep_nonpositive" or "skip_positive"; None when unclassifiable


def classify(
    candles: list[dict],
    *,
    signal_time_ms: int,
    side: str,
    lookback_minutes: int = MOMENTUM_LOOKBACK_MINUTES,
) -> str | None:
    """Classify direction-aligned prior momentum from closed 1m candles.

    Candles map to {"time": int(ms), "close": float}; a candle counts only
    when its minute has fully closed by the signal time. Returns
    "skip_positive" when the direction-aligned return over the lookback is
    positive, "keep_nonpositive" otherwise, and None when the evidence is
    missing or malformed.
    """
    if side not in {"LONG", "SHORT"}:
        return None
    usable = sorted(
        (
            candle
            for candle in candles
            if _positive_int(candle.get("time"))
            and _finite_number(candle.get("close")) is not None
            and int(candle["time"]) + 60_000 <= signal_time_ms
        ),
        key=lambda candle: int(candle["time"]),
    )
    if not usable:
        return None

    latest = usable[-1]
    lookback_ms = lookback_minutes * 60_000
    prior = next(
        (
            candle
            for candle in reversed(usable[:-1])
            if int(candle["time"]) <= int(latest["time"]) - lookback_ms
        ),
        None,
    )
    if prior is None:
        return None

    latest_close = _finite_number(latest["close"])
    prior_close = _finite_number(prior["close"])
    if latest_close is None or prior_close is None or prior_close <= 0:
        return None

    direction = 1 if side == "LONG" else -1
    aligned_return = direction * (latest_close / prior_close - 1)
    return "skip_positive" if aligned_return > 0 else "keep_nonpositive"


def aligned_return(latest_close: Decimal, prior_close: Decimal, side: str) -> Decimal:
    direction = 1 if side == "LONG" else -1
    return Decimal(direction) * (latest_close / prior_close - 1)


def _positive_int(value) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number
