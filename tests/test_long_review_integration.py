"""LONG review gates agree across real forensic reconciliation and SQLite reports."""

import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest
from forensic_fixture import settled_entry_fixture

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import analyze_long_participation as analysis  # noqa: E402
import export_dashboard_data as dashboard  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "completed,invalid,unresolved,historical,expected,current_time",
    [
        (19, False, False, 0, "collecting", "2026-10-28T10:00:00+00:00"),
        (20, False, False, 0, "ready_for_review", "2026-10-28T10:00:00+00:00"),
        (20, True, False, 0, "collecting", "2026-10-28T10:00:00+00:00"),
        (20, False, True, 0, "collecting", "2026-10-28T10:00:00+00:00"),
        (19, False, False, 20, "collecting", "2026-10-28T10:00:00+00:00"),
        (20, False, False, 20, "ready_for_review", "2026-10-27T19:06:35+02:00"),
        (20, False, False, 20, "collecting", "not-a-date"),
        (20, False, False, 20, "collecting", None),
    ],
)
def test_review_requires_complete_valid_and_resolved_sample(
    tmp_path, capsys, completed, invalid, unresolved, historical, expected, current_time
):
    assignments, intents, fills, closes = [], [], [], []
    with sqlite3.connect(":memory:") as database:
        database.executescript(
            "CREATE TABLE intents (intent_id TEXT, status TEXT, approval_mode TEXT);"
            "CREATE TABLE execution_plans "
            "(intent_id TEXT, payload_json TEXT, bybit_order_ids_json TEXT, created_at TEXT);"
        )
        for arm in ("take", "skip"):
            for index in range(20 + int(unresolved and arm == "take") + historical):
                old = index >= 20 + int(unresolved and arm == "take")
                intent_id = f"{arm}-{index}"
                created_at = "2026-10-09T10:00:00+00:00" if old else current_time
                executed = arm == "take" and (
                    old or index < completed or (unresolved and index == 20)
                )
                status = (
                    "EXECUTED" if executed else "FAILED" if arm == "take" else "SKIPPED"
                )
                approval = "AUTO" if arm == "take" else "SKIPPED"
                if invalid and intent_id == "take-0":
                    approval = "MANUAL"
                order_ids = [intent_id] if executed else []
                assignments.append(
                    {
                        "intent_id": intent_id,
                        "arm": arm,
                        "created_at": created_at,
                        "status": status,
                        "approval_mode": approval,
                        "bybit_order_ids": order_ids,
                    }
                )
                database.execute(
                    "INSERT INTO intents VALUES (?, ?, ?)",
                    (intent_id, status, approval),
                )
                database.execute(
                    "INSERT INTO execution_plans VALUES (?, ?, ?, ?)",
                    (
                        intent_id,
                        json.dumps(
                            {
                                "side": "LONG",
                                "policy": {
                                    "strategy_v2": {"long_participation_arm": arm}
                                },
                            }
                        ),
                        json.dumps(order_ids),
                        created_at,
                    ),
                )
                if arm != "take" or (not old and index >= completed):
                    continue
                symbol = f"TEST{index}USDT"
                intents.append(
                    {
                        "intent_id": intent_id,
                        "entry_order_ids": order_ids,
                        "intent": {"symbol": symbol, "side": "LONG", "stop_loss": "90"},
                        "metadata": {"long_participation_arm": "take"},
                    }
                )
                fills.append(
                    {
                        "execType": "Trade",
                        "orderId": intent_id,
                        "execTime": "1000",
                        "execQty": "2",
                        "execPrice": "100",
                        "closedSize": "0",
                    }
                )
                closes.append(
                    {
                        "symbol": symbol,
                        "side": "Sell",
                        "updatedTime": "2000",
                        "closedSize": "2",
                        "closedPnl": "1",
                        "openFee": "0",
                        "closeFee": "0",
                    }
                )
        rows = {
            "analysis/long_participation_assignments.jsonl": [
                {"assignments": assignments}
            ],
            "analysis/trade_lineage.jsonl": [{"intents": intents}],
            "bybit/executions.jsonl": fills,
            "bybit/closed_pnl.jsonl": closes,
            "bybit/transaction_log.jsonl": [],
            **settled_entry_fixture(
                intents, fills, {item["intent_id"]: "2" for item in intents}
            ),
        }
        bundle = tmp_path / "review.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            for name, records in rows.items():
                archive.writestr(
                    f"forensic/{name}",
                    "".join(json.dumps(row) + "\n" for row in records),
                )
        positions = analysis.reconcile(bundle)
        assert len(positions) == completed + historical and all(
            p["complete"] for p in positions
        )
        result = dashboard._demo_long_participation_experiment(
            database, positions, skip_fraction=0.5, forensic_available=True
        )
        assert result["status"] == expected
        expected_completed = (
            completed if current_time not in (None, "not-a-date") else 0
        )
        assert result["take_completed_position_count"] == expected_completed
        assert (
            result["sizing_cohorts"]["pre_cutover"]["take_completed_position_count"]
            == historical
        )
        assert result["unknown_time_assignment_count"] == (
            40 if current_time in (None, "not-a-date") else 0
        )
        assert result["review_target_completed_take_positions"] == 20
        assert result["take_unresolved_count"] == int(unresolved)
        analysis.report(bundle)
        output = capsys.readouterr().out.split(
            "long_participation_sizing_cohort=full_baseline_risk\n"
        )[1]
        assert f"long_participation_take_completed={expected_completed}" in output
        assert "long_participation_review_target_completed_takes=20" in output
        assert f"long_participation_status={expected}/" in output
