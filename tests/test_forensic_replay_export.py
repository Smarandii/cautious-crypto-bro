"""Forensic export coverage includes unfilled randomized assignments."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from export_forensic_replay import (  # noqa: E402
    MARKET_CANDLE_LOOKBACK_MS,
    _market_candle_coverage,
)


def test_market_candle_coverage_includes_skip_only_symbols_and_signal_history() -> None:
    signal_time = datetime(2026, 10, 9, 12, tzinfo=UTC)
    signal_ms = int(signal_time.timestamp() * 1000)

    symbols, start_ms = _market_candle_coverage(
        {"BTCUSDT"},
        [signal_ms + 5 * 60_000],
        [
            {
                "symbol": "skipusdt",
                "created_at": signal_time.isoformat(),
                "arm": "skip",
            }
        ],
    )

    assert symbols == ["BTCUSDT", "SKIPUSDT"]
    assert start_ms == signal_ms - MARKET_CANDLE_LOOKBACK_MS
