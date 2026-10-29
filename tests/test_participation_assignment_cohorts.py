"""Assignment provenance must survive the real reporting boundaries."""

import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from analyze_long_participation import _momentum_stratum, _report_cohort, report  # noqa: E402
from export_dashboard_data import (  # noqa: E402
    _demo_long_participation_experiment,
    _participation_cohort,
)

pytestmark = pytest.mark.integration


def test_disabled_empty_participation_remains_inactive():
    result = _participation_cohort(
        [], [], skip_fraction=0, forensic_available=False, reviewable=True
    )
    assert result["active"] is False
    assert result["status"] == "inactive"


def _sample(tag: str | None, count: int = 20) -> tuple[list[dict], list[dict]]:
    assignments, positions = [], []
    for arm in ("take", "skip"):
        for index in range(count):
            key = f"{tag}-{arm}-{index}"
            momentum = (
                ("positive" if arm == "take" else "nonpositive")
                if tag == "gated"
                else tag
            )
            assignments.append(
                {
                    "intent_id": key,
                    "arm": arm,
                    "created_at": "2026-10-28T10:00:00+00:00",
                    "status": "EXECUTED" if arm == "take" else "SKIPPED",
                    "approval_mode": "AUTO" if arm == "take" else "SKIPPED",
                    "order_ids": [key] if arm == "take" else [],
                    "bybit_order_ids": [key] if arm == "take" else [],
                    "long_participation_momentum": momentum,
                }
            )
            if arm == "take":
                net = 1.0 if index % 2 else -0.5
                positions.append(
                    {
                        "intent_id": key,
                        "side": "LONG",
                        "long_participation_arm": "take",
                        "complete": True,
                        "start": index,
                        "net_pnl": net,
                        "net_r": net / 10,
                        "risk_usdt": 10,
                        "fees": 0,
                        "funding": 0,
                        "gross_pnl": net,
                    }
                )
    return assignments, positions


@pytest.mark.parametrize("tag", ["gated", "unexpected"])
def test_nonrandomized_sample_never_claims_randomized_readiness(tag, capsys):
    assignments, positions = _sample(tag)
    _report_cohort(
        assignments, {p["intent_id"]: p for p in positions}, {}, reviewable=True
    )
    output = capsys.readouterr().out
    result = _participation_cohort(
        assignments,
        positions,
        skip_fraction=0.5,
        forensic_available=True,
        reviewable=True,
    )
    assert "long_participation_status=ready_for_review" not in output
    assert "long_participation_itt_take_mean" not in output
    assert result["status"] != "ready_for_review"
    assert "SHA-256" not in result["assignment"]
    assert result["take_completed_position_count"] == 20


def test_gated_take_cannot_complete_hash_sample(capsys):
    assignments, positions = _sample("unavailable", 19)
    gated, gated_positions = _sample("gated", 1)
    assignments += gated
    positions += gated_positions
    _report_cohort(
        assignments, {p["intent_id"]: p for p in positions}, {}, reviewable=True
    )
    assert "long_participation_status=ready_for_review" not in capsys.readouterr().out
    result = _participation_cohort(
        assignments,
        positions,
        skip_fraction=0.5,
        forensic_available=True,
        reviewable=True,
    )
    assert result["status"] != "ready_for_review"


def test_hash_fallback_retains_resolved_review_gate(capsys):
    assignments, positions = _sample("unavailable")
    _report_cohort(
        assignments, {p["intent_id"]: p for p in positions}, {}, reviewable=True
    )
    assert "long_participation_status=ready_for_review" in capsys.readouterr().out
    result = _participation_cohort(
        assignments,
        positions,
        skip_fraction=0.5,
        forensic_available=True,
        reviewable=True,
    )
    assert result["status"] == "ready_for_review"


def test_frozen_provenance_survives_sqlite_and_archive_reports(tmp_path, capsys):
    assignments = []
    for tag in (None, "unavailable", "gated", "unrecognized"):
        rows, _ = _sample(tag, 1)
        assignments.extend(rows)
    # A valid momentum value with an inconsistent arm must remain unknown.
    assignments[-1]["long_participation_momentum"] = "positive"
    with closing(sqlite3.connect(":memory:")) as database:
        database.executescript(
            "CREATE TABLE intents (intent_id TEXT, status TEXT, approval_mode TEXT);"
            "CREATE TABLE execution_plans "
            "(intent_id TEXT, payload_json TEXT, bybit_order_ids_json TEXT, created_at TEXT);"
        )
        for row in assignments:
            database.execute(
                "INSERT INTO intents VALUES (?, ?, ?)",
                (
                    row["intent_id"],
                    row["status"],
                    row["approval_mode"],
                ),
            )
            database.execute(
                "INSERT INTO execution_plans VALUES (?, ?, ?, ?)",
                (
                    row["intent_id"],
                    json.dumps(
                        {
                            "side": "LONG",
                            "policy": {
                                "strategy_v2": {
                                    "long_participation_arm": row["arm"],
                                    "long_participation_momentum": row[
                                        "long_participation_momentum"
                                    ],
                                }
                            },
                        }
                    ),
                    json.dumps(row["order_ids"]),
                    row["created_at"],
                ),
            )
        result = _demo_long_participation_experiment(
            database,
            [],
            skip_fraction=0.5,
            forensic_available=True,
        )
    assert result["status"] == "descriptive_only"
    cohorts = result["assignment_cohorts"]
    assert set(cohorts) == {
        "legacy_untagged",
        "hash_fallback",
        "momentum_gate",
        "unknown",
    }
    assert all(cohort["planned_count"] == 2 for cohort in cohorts.values())
    assert cohorts["momentum_gate"]["status"] == "descriptive_only"
    assert cohorts["unknown"]["status"] == "descriptive_only"
    bundle = tmp_path / "mechanisms.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(
            "forensic/analysis/long_participation_assignments.jsonl",
            json.dumps({"assignments": assignments}) + "\n",
        )
        archive.writestr("forensic/analysis/trade_lineage.jsonl", '{"intents": []}\n')
        for name in ("executions", "closed_pnl", "transaction_log"):
            archive.writestr(f"forensic/bybit/{name}.jsonl", "")
    report(bundle)
    output = capsys.readouterr().out
    for name in cohorts:
        assert f"long_participation_assignment_cohort={name}" in output
    assert "long_participation_status=ready_for_review" not in output


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("positive", "skip_positive"),
        ("nonpositive", "keep_nonpositive"),
        ("unavailable", None),
        ("unknown", None),
    ],
)
def test_frozen_momentum_does_not_require_or_reclassify_from_candles(tag, expected):
    assert _momentum_stratum({"long_participation_momentum": tag}, {}) == expected
