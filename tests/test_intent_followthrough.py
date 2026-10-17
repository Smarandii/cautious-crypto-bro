"""Tests for all-intent signal-time movement diagnostics."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import analyze_intent_followthrough as analysis


def test_signed_horizon_return_uses_first_fully_closed_minute() -> None:
    case = {"time_ms": 23_000, "side": "LONG"}
    candles = [
        {"time": 0, "close": 100.0},
        {"time": 60_000, "close": 110.0},
        {"time": 300_000, "close": 120.0},
    ]

    result = analysis.signed_horizon_return(
        case,
        candles,
        horizon_minutes=5,
    )

    assert result == 120 / 110 - 1


def test_signed_horizon_return_reverses_short_direction() -> None:
    case = {"time_ms": 0, "side": "SHORT"}
    candles = [
        {"time": 0, "close": 100.0},
        {"time": 60_000, "close": 90.0},
        {"time": 120_000, "close": 80.0},
    ]

    result = analysis.signed_horizon_return(case, candles, horizon_minutes=2)

    assert result is not None and abs(result - 0.1) < 1e-12


def test_signed_horizon_return_omits_missing_data_and_invalid_sides() -> None:
    candles = [{"time": 0, "close": 100.0}]

    assert (
        analysis.signed_horizon_return(
            {"time_ms": 0, "side": "LONG"}, [], horizon_minutes=1
        )
        is None
    )
    assert (
        analysis.signed_horizon_return(
            {"time_ms": 0, "side": "UNKNOWN"}, candles, horizon_minutes=1
        )
        is None
    )


def test_benchmark_adjusted_followthrough_pairs_same_window_directional_returns() -> (
    None
):
    case = {
        "time_ms": 30_000,
        "side": "LONG",
        "status": "EXECUTED",
        "symbol": "ALTUSDT",
    }
    candles = {
        "ALTUSDT": [
            {"time": 60_000, "close": 110.0},
            {"time": 120_000, "close": 121.0},
        ],
        "BTCUSDT": [
            {"time": 60_000, "close": 105.0},
            {"time": 120_000, "close": 110.25},
        ],
    }

    grouped = analysis.benchmark_adjusted_followthrough(
        [case],
        candles,
        horizon_minutes=2,
    )

    observation = grouped[("LONG", "ALL")][0]
    assert observation["asset_return"] == pytest.approx(0.1)
    assert observation["benchmark_return"] == pytest.approx(0.05)
    assert observation["relative_return"] == pytest.approx(0.05)
    assert grouped[("LONG", "EXECUTED")] == grouped[("LONG", "ALL")]


def test_day_cluster_interval_is_reproducible_and_empty_safe() -> None:
    observations = [
        {"day": "2026-10-01", "relative_return": 1.0},
        {"day": "2026-10-01", "relative_return": 2.0},
        {"day": "2026-10-02", "relative_return": -1.0},
    ]

    assert analysis.day_cluster_interval(
        observations, iterations=100, seed=11
    ) == analysis.day_cluster_interval(observations, iterations=100, seed=11)
    assert analysis.day_cluster_interval([], iterations=100) is None


def test_symbol_cluster_interval_is_reproducible_and_empty_safe() -> None:
    clusters = {"A": [1.0, 1.0], "B": [-1.0]}

    assert analysis.symbol_cluster_interval(clusters, iterations=100, seed=5) == (
        analysis.symbol_cluster_interval(clusters, iterations=100, seed=5)
    )
    assert analysis.symbol_cluster_interval({}, iterations=100) is None


def test_source_side_followthrough_separates_source_side_and_execution_status() -> None:
    cases = [
        {
            "source": "source A",
            "side": "LONG",
            "status": "EXECUTED",
            "symbol": "AAAUSDT",
            "time_ms": 0,
        },
        {
            "source": "source A",
            "side": "LONG",
            "status": "FAILED",
            "symbol": "AAAUSDT",
            "time_ms": 0,
        },
        {
            "source": "source B",
            "side": "SHORT",
            "status": "SKIPPED",
            "symbol": "BBBUSDT",
            "time_ms": 0,
        },
    ]
    candles = {
        symbol: [
            {"time": minute * 60_000, "close": 100.0 + minute * 10}
            for minute in range(4)
        ]
        for symbol in ("AAAUSDT", "BBBUSDT")
    }

    grouped = analysis.source_side_followthrough(
        cases,
        candles,
        horizon_minutes=2,
    )

    assert grouped[("source A", "LONG", "EXECUTED")]["AAAUSDT"] == pytest.approx([0.1])
    assert grouped[("source A", "LONG", "FAILED")]["AAAUSDT"] == pytest.approx([0.1])
    assert grouped[("source A", "LONG", "ALL")]["AAAUSDT"] == pytest.approx([0.1, 0.1])
    assert grouped[("source B", "SHORT", "SKIPPED")]["BBBUSDT"] == pytest.approx([-0.1])


def test_purged_chronological_folds_do_not_cross_horizon_boundaries() -> None:
    cases = [{"time_ms": minute * 60_000, "case": minute} for minute in range(10)]

    folds = analysis.purged_chronological_folds(cases, horizon_minutes=1)

    assert [case["case"] for case in folds["train"]] == [0, 1, 2, 3, 4]
    assert [case["case"] for case in folds["validation"]] == [5, 6]
    assert [case["case"] for case in folds["holdout"]] == [7, 8, 9]
    assert all(case["time_ms"] + 60_000 <= 5 * 60_000 for case in folds["train"])
    assert all(case["time_ms"] + 60_000 <= 7 * 60_000 for case in folds["validation"])
