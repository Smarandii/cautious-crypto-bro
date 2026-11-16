import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_execution import context, limit_intent, market_intent, policy

from cautious_crypto_bro.execution import ExecutionPlanner


def test_mirror_report_compares_saved_plans_and_counts_missing_coverage(tmp_path):
    planner = ExecutionPlanner()
    instrument = context("100")
    standard = planner.plan(market_intent(), policy(), instrument)
    mirror = planner.plan_mirror(standard, instrument)
    standard_limit = planner.plan(limit_intent(), policy(), instrument)
    mirror_limit = planner.plan_mirror(standard_limit, instrument)
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
        "low": "99.9",
        "close": "100",
    }
    limit_start = int(datetime(2026, 11, 3, tzinfo=UTC).timestamp() * 1000) + 60_000
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
                        {**comparison, "kind": "historical", "mirror_plan": None},
                        {**comparison, "standard_plan": None},
                        {**comparison, "planned_at": "2026-11-02T00:00:00+00:00"},
                        {
                            "planned_at": "2026-11-03T00:00:00+00:00",
                            "standard_plan": standard_limit.model_dump(mode="json"),
                            "mirror_plan": mirror_limit.model_dump(mode="json"),
                        },
                    ],
                )
            ),
        )
        settlement = candle["startTime"] + 60_000
        archive.writestr(
            "forensic/market_1m/BTCUSDT.jsonl",
            "\n".join(
                map(
                    json.dumps,
                    [
                        candle,
                        {**candle, "startTime": settlement},
                        {
                            **candle,
                            "startTime": settlement + 60_000,
                            "low": "89",
                            "close": "90",
                        },
                        *(
                            {
                                "startTime": limit_start + index * 60_000,
                                "open": "99.9",
                                "high": "99.9",
                                "low": "89",
                                "close": "90",
                            }
                            for index in range(3)
                        ),
                    ],
                )
            ),
        )
        archive.writestr(
            "forensic/market_funding/BTCUSDT.jsonl",
            json.dumps(
                {"fundingRateTimestamp": str(settlement), "fundingRate": "0.0001"}
            ),
        )

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
            "3",
            "--mirror-stop-slippage",
            "0",
            "--mirror-stop-slippage",
            "0.001",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    report, adverse = json.loads(result.stdout)
    assert adverse["mirror_stop_slippage"] == "0.001"
    assert report["candidates"] == 4
    assert report["excluded"] == {"missing_plan": 1, "missing_candles": 1}
    source_long = report["by_source_side"]["LONG"]
    assert source_long["all"]["standard"]["expectancy_r"] < 0
    exit_price = mirror.take_profit
    entry_price = mirror.orders[0].reference_price
    fees = (entry_price + exit_price) * Decimal("0.00055")
    funding = entry_price * Decimal("0.0001")
    expected_r = (entry_price - exit_price - fees + funding) / (
        mirror.stop_loss - entry_price
    )
    assert source_long["all"]["mirror"]["expectancy_r"] == pytest.approx(
        float(expected_r) / 2
    )
    assert source_long["all"]["mirror_unfilled"] == 1
    assert source_long["all"]["standard_unfilled"] == 0
    assert source_long["all"]["resolved_only"]["paired_delta"]["count"] == 1
    assert source_long["latest_30_percent"]["mirror"]["expectancy_r"] == 0
    assert source_long["latest_30_percent"]["candidates"] == 2
    assert source_long["latest_30_percent"]["excluded"] == 1
    historical = subprocess.run(
        [*result.args, "--cohort", "historical"],
        capture_output=True,
        text=True,
        check=True,
    )
    historical_report, _ = json.loads(historical.stdout)
    assert historical_report["cohort"] == "historical"
    assert historical_report["candidates"] == 1
    assert historical_report["by_source_side"]["LONG"]["all"]["mirror"][
        "expectancy_r"
    ] == pytest.approx(float(expected_r))
    assert source_long["latest_30_percent"]["paired_delta"]["count"] == 1
    assert report["by_source_side"]["SHORT"]["all"]["paired_delta"]["count"] == 0
