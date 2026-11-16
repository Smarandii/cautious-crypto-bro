import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from test_execution import context, market_intent, policy

from cautious_crypto_bro.execution import ExecutionPlanner


def test_mirror_report_compares_saved_plans_and_counts_missing_coverage(tmp_path):
    planner = ExecutionPlanner()
    instrument = context("100")
    standard = planner.plan(market_intent(), policy(), instrument)
    mirror = planner.plan_mirror(standard, instrument)
    planned_at = datetime(2026, 11, 1, tzinfo=UTC)
    comparison = {
        "planned_at": planned_at.isoformat(),
        "standard_plan": standard.model_dump(mode="json"),
        "mirror_plan": mirror.model_dump(mode="json"),
    }
    candle = {
        "startTime": int(planned_at.timestamp() * 1000) + 60_000,
        "open": "100",
        "high": "100.1",
        "low": "89",
        "close": "90",
    }
    bundle = tmp_path / "forensic.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("README", "Synthetic fixture")
        archive.writestr(
            "forensic/analysis/mirror_comparisons.jsonl",
            "\n".join(
                map(
                    json.dumps,
                    [
                        comparison,
                        {**comparison, "standard_plan": None},
                        {**comparison, "planned_at": "2026-11-02T00:00:00+00:00"},
                    ],
                )
            ),
        )
        archive.writestr("forensic/market_1m/BTCUSDT.jsonl", json.dumps(candle))

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "replay_demo_mirror.py"
            ),
            str(bundle),
            "--fee",
            "0.00055",
            "--horizon-minutes",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["candidates"] == 3
    assert report["excluded"] == {"missing_plan": 1, "missing_candles": 1}
    source_long = report["by_source_side"]["LONG"]
    assert source_long["all"]["standard"]["expectancy_r"] < 0
    assert source_long["all"]["mirror"]["expectancy_r"] > 0
    assert source_long["latest_30_percent"]["paired_delta"]["count"] == 1
    assert report["by_source_side"]["SHORT"]["all"]["paired_delta"]["count"] == 0
