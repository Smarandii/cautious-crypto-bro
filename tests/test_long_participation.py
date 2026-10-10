"""Checks for the randomized Demo LONG take/skip evidence pipeline."""

import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from analyze_long_participation import report  # noqa: E402


def _archive(
    path: Path,
    assignments: list[dict],
    market_candles: dict[str, list[dict]] | None = None,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "forensic/analysis/long_participation_assignments.jsonl",
            json.dumps({"assignments": assignments}) + "\n",
        )
        archive.writestr(
            "forensic/analysis/trade_lineage.jsonl",
            json.dumps({"intents": []}) + "\n",
        )
        for name in (
            "bybit/executions.jsonl",
            "bybit/closed_pnl.jsonl",
            "bybit/transaction_log.jsonl",
        ):
            archive.writestr(f"forensic/{name}", "")
        for symbol, candles in (market_candles or {}).items():
            archive.writestr(
                f"forensic/market_1m/{symbol}.jsonl",
                "".join(json.dumps(candle) + "\n" for candle in candles),
            )


def test_participation_report_waits_for_take_arm_resolution(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "participation.zip"
    _archive(
        bundle,
        [
            {
                "intent_id": "take-1",
                "created_at": "2026-10-09T10:00:00+00:00",
                "status": "EXECUTED",
                "approval_mode": "AUTO",
                "arm": "take",
            },
            {
                "intent_id": "skip-1",
                "created_at": "2026-10-09T10:01:00+00:00",
                "status": "SKIPPED",
                "approval_mode": "SKIPPED",
                "arm": "skip",
            },
        ],
    )

    report(bundle)
    output = capsys.readouterr().out

    assert "long_participation_take_assigned=1" in output
    assert "long_participation_take_unresolved=1" in output
    assert "long_participation_skip_assigned=1" in output
    assert (
        "long_participation_status=collecting/take_target:20/skip_target:20" in output
    )
    assert "unresolved takes are excluded" in output


def test_participation_report_handles_archives_without_assignments(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "legacy.zip"
    _archive(bundle, [])
    report(bundle)
    assert (
        "long_participation_status=awaiting_tagged_assignments"
        in capsys.readouterr().out
    )


def test_participation_report_stratifies_randomization_by_prior_momentum(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "momentum-strata.zip"
    signal_time = datetime(2026, 10, 9, 2, tzinfo=UTC)
    signal_ms = int(signal_time.timestamp() * 1000)
    assignments = [
        {
            "intent_id": "keep-take",
            "created_at": signal_time.isoformat(),
            "status": "FAILED",
            "approval_mode": "AUTO",
            "bybit_order_ids": [],
            "arm": "take",
            "symbol": "KEEPUSDT",
            "side": "LONG",
        },
        {
            "intent_id": "skip-positive",
            "created_at": signal_time.isoformat(),
            "status": "SKIPPED",
            "approval_mode": "SKIPPED",
            "bybit_order_ids": [],
            "arm": "skip",
            "symbol": "SKIPUSDT",
            "side": "LONG",
        },
        {
            "intent_id": "unclassified",
            "created_at": signal_time.isoformat(),
            "status": "SKIPPED",
            "approval_mode": "SKIPPED",
            "bybit_order_ids": [],
            "arm": "skip",
            "symbol": "NOCANDLESUSDT",
            "side": "LONG",
        },
    ]
    candles = {
        symbol: [
            {
                "startTime": str(signal_ms - 3_660_000),
                "close": "100",
                "high": "101",
                "low": "99",
            },
            {
                "startTime": str(signal_ms - 60_000),
                "close": latest_close,
                "high": latest_close,
                "low": latest_close,
            },
            # This candle closes after the signal and must not affect the group.
            {
                "startTime": str(signal_ms),
                "close": "1000",
                "high": "1001",
                "low": "999",
            },
        ]
        for symbol, latest_close in (
            ("KEEPUSDT", "90"),
            ("SKIPUSDT", "110"),
        )
    }
    _archive(bundle, assignments, candles)

    report(bundle)
    output = capsys.readouterr().out

    assert "long_participation_momentum_unclassified=1" in output
    assert (
        "long_participation_momentum_stratum=keep_nonpositive "
        "take_assigned=1 skip_assigned=0 take_completed=0 "
        "take_failed_before_fill=1 take_unresolved=0 "
        "take_mean_net_usdt_per_assignment=0.0 "
        "take_day_cluster_95ci_usdt=n/a "
        "take_mean_net_r_per_assignment=0.0"
    ) in output
    assert (
        "long_participation_momentum_stratum=skip_positive "
        "take_assigned=0 skip_assigned=1 take_completed=0 "
        "take_failed_before_fill=0 take_unresolved=0 "
        "take_mean_net_usdt_per_assignment=n/a"
    ) in output
