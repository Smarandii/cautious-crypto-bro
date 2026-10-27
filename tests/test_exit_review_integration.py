"""Exit review isolates saved sizing epochs through real ledgers and SQLite."""

import json
import sqlite3
import sys
import zipfile
from datetime import datetime
from functools import partial
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import analyze_forensic_pnl as analysis
import export_dashboard_data as dashboard

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "profile",
    [
        "payoff_early_tight_trail_long_ab_020_control",
        "payoff_early_tight_trail_long_ab_015",
    ],
)
@pytest.mark.parametrize(
    "new_count,created,current_count,ready",
    [
        (19, "2026-10-27T19:06:35+02:00", 19, False),
        (20, "2026-10-27T19:06:35+02:00", 20, True),
        (20, None, 0, False),
        (20, "2026-10-28T10:00:00", 0, False),
    ],
)
def test_exit_review_uses_plan_creation_not_late_fill(
    tmp_path, capsys, profile, new_count, created, current_count, ready, monkeypatch
):
    # Exercise the real interval calculator with fewer resamples: these cases
    # prove cohort selection, not Monte Carlo convergence (covered separately).
    for consumer in (analysis, dashboard):
        monkeypatch.setattr(
            consumer,
            "bootstrap_performance_intervals",
            partial(consumer.bootstrap_performance_intervals, iterations=25),
        )
    lineage, fills, closes = [], [], []
    fill_ms = int(
        datetime.fromisoformat("2026-10-28T12:00:00+00:00").timestamp() * 1000
    )
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            "CREATE TABLE execution_plans(intent_id TEXT,payload_json TEXT,created_at TEXT);CREATE TABLE intents(intent_id TEXT,status TEXT);"
        )
        for epoch, count, when, side, qty, pnl in [
            ("old", 20, "2026-10-26T10:00:00+00:00", "LONG", "0.2", "1"),
            ("new", new_count, created, "LONG", "2", "20"),
            ("short", 2, "2026-10-26T10:00:00+00:00", "SHORT", "2", "-3"),
        ]:
            for index in range(count):
                identity = f"{epoch}-{index}"
                symbol = f"{epoch.upper()}{index}USDT"
                plan = {
                    "side": side,
                    "policy": {
                        "risk_per_trade_pct": "0.3" if epoch == "new" else "0.1",
                        "strategy_v2": {"exit_profile": profile},
                    },
                }
                db.execute(
                    "INSERT INTO execution_plans VALUES(?,?,?)",
                    (identity, json.dumps(plan), when),
                )
                db.execute("INSERT INTO intents VALUES(?,?)", (identity, "EXECUTED"))
                lineage.append(
                    {
                        "intent_id": identity,
                        "created_at": when,
                        "entry_order_ids": [identity],
                        "intent": {
                            "symbol": symbol,
                            "side": side,
                            "stop_loss": "90" if side == "LONG" else "110",
                        },
                        "metadata": {
                            "exit_profile": profile,
                            "risk_per_trade_pct": plan["policy"]["risk_per_trade_pct"],
                        },
                    }
                )
                fills.append(
                    {
                        "execType": "Trade",
                        "orderId": identity,
                        "execTime": str(fill_ms),
                        "execQty": qty,
                        "execPrice": "100",
                        "closedSize": "0",
                    }
                )
                closes.append(
                    {
                        "symbol": symbol,
                        "side": "Sell" if side == "LONG" else "Buy",
                        "updatedTime": str(fill_ms + 60000),
                        "closedSize": qty,
                        "closedPnl": pnl,
                        "openFee": "0.02",
                        "closeFee": "0.03",
                    }
                )
        bundle = tmp_path / "exit-review.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            for name, rows in {
                "analysis/trade_lineage.jsonl": [{"intents": lineage}],
                "bybit/executions.jsonl": fills,
                "bybit/closed_pnl.jsonl": closes,
                "bybit/transaction_log.jsonl": [],
            }.items():
                archive.writestr(
                    "forensic/" + name, "".join(json.dumps(row) + "\n" for row in rows)
                )
        positions = analysis.reconcile(bundle)
        assert len(positions) == 20 + new_count + 2 and all(
            p["complete"] for p in positions
        )
        result = dashboard._demo_exit_experiment(
            db, positions, profile=profile, forensic_available=True
        )
        assert result["status"] == ("ready_for_review" if ready else "collecting")
        assert result["completed_position_count"] == current_count
        assert result["planned_count"] == current_count
        assert result["sizing_cohorts"]["pre_cutover"]["completed_position_count"] == 20
        assert (
            result["sizing_cohorts"]["unaffected_shorts"]["completed_position_count"]
            == 2
        )
        assert (
            result["sizing_cohorts"]["unaffected_shorts"]["performance"]["net_pnl_usdt"]
            == -6
        )
        assert (
            result["sizing_cohorts"]["unaffected_shorts"]["performance"]["fees_usdt"]
            == 0.1
        )
        assert (
            result["sizing_cohorts"]["pre_cutover"]["performance"]["avg_win_usdt"] == 1
        )
        if current_count:
            assert result["performance"]["avg_win_usdt"] == 20
            assert result["performance"]["expectancy_r"] == 1
        else:
            assert (
                result["sizing_cohorts"]["unknown"]["completed_position_count"]
                == new_count
            )
            assert result["sizing_cohorts"]["unknown"]["status"] == "collecting"
        assert all(
            p["created_at"] == "2026-10-26T10:00:00+00:00"
            for p in positions
            if p["intent_id"].startswith("old-")
        )
        analysis.report_prospective_demo_exit_profile(positions, profile=profile)
        text = capsys.readouterr().out
        current = text.split(
            "prospective_demo_exit_profile_sizing_cohort=full_baseline_risk\n"
        )[1]
        assert f"completed_positions={current_count}" in current
        assert f"status={result['status']}/" in current
