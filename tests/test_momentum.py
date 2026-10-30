"""Momentum-gated Demo LONG participation: runtime classifier and parity."""

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from fakes import FakeAccountGateway

from cautious_crypto_bro.domain import AutoApprovalMode, ExecutionPolicy, Side
from cautious_crypto_bro.momentum import classify
from cautious_crypto_bro.signals.planning import SignalPlanning

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _minute_series(current_minute: int, *, rising: bool) -> list[tuple[int, Decimal]]:
    closes = [
        (current_minute - (70 - index) * 60_000, Decimal("100") + index)
        for index in range(70)
    ]
    if not rising:
        closes = [(start, Decimal("270") - close) for start, close in closes]
    return closes


def _planning(gateway: FakeAccountGateway, *, gate: bool = True) -> SignalPlanning:
    return SignalPlanning(
        store=object(),
        planner=object(),
        executor=gateway,
        coordinator=object(),
        auto_approval_mode=AutoApprovalMode.ALL,
        demo_long_participation_skip_fraction=Decimal("0.50"),
        demo_long_momentum_gate=gate,
    )


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        trading_capital_usdt=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
    )


@pytest.mark.parametrize(
    "rising,expected", [(True, "skip_positive"), (False, "keep_nonpositive")]
)
def test_momentum_gate_assigns_participation_deterministically(
    rising, expected
) -> None:
    gateway = FakeAccountGateway()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    current_minute = now_ms - now_ms % 60_000
    gateway.minute_closes["BTCUSDT"] = _minute_series(current_minute, rising=rising)

    service = _planning(gateway)
    stratum = asyncio.run(service._prior_momentum_stratum("BTCUSDT"))
    assert stratum == expected

    policy = service._policy_for_intent_side(
        _policy(), Side.LONG, UUID(int=1), momentum_stratum=stratum
    )
    assert policy.strategy_v2.long_participation_arm == (
        "take" if expected == "skip_positive" else "skip"
    )
    assert policy.strategy_v2.long_participation_momentum == (
        "positive" if expected == "skip_positive" else "nonpositive"
    )


def test_momentum_gate_falls_back_to_the_bucket_when_candles_fail() -> None:
    gateway = FakeAccountGateway()
    service = _planning(gateway)

    stratum = asyncio.run(service._prior_momentum_stratum("BTCUSDT"))
    assert stratum is None
    assert gateway.minute_closes_calls == ["BTCUSDT"]

    policy = service._policy_for_intent_side(
        _policy(), Side.LONG, UUID(int=1), momentum_stratum=stratum
    )
    assert policy.strategy_v2.long_participation_momentum == "unavailable"
    assert policy.strategy_v2.long_participation_arm in {"take", "skip"}

    # A raised read fails the same way instead of losing the post.
    def broken(symbol, *, limit=120):
        raise RuntimeError("kline down")

    gateway.one_minute_closes = broken
    assert asyncio.run(service._prior_momentum_stratum("BTCUSDT")) is None


def test_momentum_gate_off_keeps_the_randomized_era_unchanged() -> None:
    gateway = FakeAccountGateway()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    gateway.minute_closes["BTCUSDT"] = _minute_series(
        now_ms - now_ms % 60_000, rising=True
    )
    service = _planning(gateway, gate=False)

    policy = service._policy_for_intent_side(_policy(), Side.LONG, UUID(int=1))
    assert policy.strategy_v2.long_participation_momentum is None
    assert policy.strategy_v2.long_participation_arm in {"take", "skip"}
    assert gateway.minute_closes_calls == []


def test_runtime_momentum_matches_the_frozen_review_classifier() -> None:
    spec = importlib.util.spec_from_file_location(
        "analyze_long_participation_parity",
        SCRIPTS / "analyze_long_participation.py",
    )
    assert spec is not None and spec.loader is not None
    analyzer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = analyzer
    spec.loader.exec_module(analyzer)

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    current_minute = now_ms - now_ms % 60_000
    candles = [
        {"time": start, "close": float(close)}
        for start, close in _minute_series(current_minute, rising=True)
    ]
    # A forming candle must never classify, exactly like the review's rule.
    candles.append({"time": current_minute, "close": "999"})
    signal_time = datetime.fromtimestamp(current_minute / 1000, UTC)

    assignment = {
        "side": "LONG",
        "symbol": "DOGEUSDT",
        "created_at": signal_time.isoformat(),
    }
    review = analyzer._momentum_stratum(assignment, {"DOGEUSDT": list(candles)})
    runtime = classify(candles, signal_time_ms=current_minute, side="LONG")
    assert review == runtime == "skip_positive"

    # A lookback gap (no candle older than 60 minutes) classifies to None.
    short_history = candles[-40:]
    assert (
        analyzer._momentum_stratum(assignment, {"DOGEUSDT": list(short_history)})
        is None
    )
    assert classify(short_history, signal_time_ms=current_minute, side="LONG") is None
