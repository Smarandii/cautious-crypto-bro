import json
from datetime import UTC, datetime
from pathlib import Path

from cautious_crypto_bro.dashboard_metrics import (
    summarize_intent_outcomes,
    summarize_open_positions,
    summarize_position_sides,
    summarize_reconciled_positions,
)


def test_intent_outcomes_separate_safety_stops_from_other_failures() -> None:
    outcomes = summarize_intent_outcomes(
        {"EXECUTED": 3, "FAILED": 5, "SKIPPED": 1},
        [
            "Market moved enough that execution would exceed risk budget",
            "Existing strategy owns BTCUSDT",
            "Stale manual approval request; delivery queue was lost",
            "Configured notional too small: qty below minOrderQty",
            "Bybit rejected order: invalid API key",
        ],
    )

    assert outcomes == {
        "executed": 3,
        "failed": 5,
        "skipped": 1,
        "pending": 0,
        "failed_by_reason": {
            "risk_budget_guard": 1,
            "ownership_guard": 1,
            "stale_request": 1,
            "minimum_order_guard": 1,
            "exchange_or_other": 1,
            "uncategorized": 0,
        },
    }


def test_reconciled_metrics_exclude_incomplete_positions() -> None:
    positions = [
        {"complete": True, "net_pnl": 40.0, "net_r": 0.5, "risk_usdt": 20.0},
        {"complete": True, "net_pnl": -60.0, "net_r": -1.0, "risk_usdt": 40.0},
        {
            "complete": False,
            "net_pnl": 900.0,
            "net_r": 9.0,
            "risk_usdt": 100.0,
        },
    ]

    metrics = summarize_reconciled_positions(positions)

    assert metrics == {
        "available": True,
        "basis": "fully_reconciled_positions",
        "position_count": 2,
        "positive_count": 1,
        "negative_count": 1,
        "win_rate_pct": 50.0,
        "net_pnl_usdt": -20.0,
        "avg_win_usdt": 40.0,
        "avg_loss_usdt": -60.0,
        "avg_win_to_loss_ratio": 0.6667,
        "break_even_avg_win_usdt_at_observed_counts": 60.0,
        "break_even_avg_loss_usdt_at_observed_counts": -40.0,
        "avg_win_increase_pct_to_break_even": 50.0,
        "avg_loss_reduction_pct_to_break_even": 33.33,
        "break_even_win_rate_pct": 60.0,
        "profit_factor": 0.6667,
        "expectancy_usdt": -10.0,
        "avg_win_r": 0.5,
        "avg_loss_r": -1.0,
        "expectancy_r": -0.25,
        "avg_initial_risk_win_usdt": 20.0,
        "avg_initial_risk_loss_usdt": 40.0,
    }


def test_direction_metrics_use_only_complete_positions_with_known_side() -> None:
    cohorts = summarize_position_sides(
        [
            {"complete": True, "side": "LONG", "net_pnl": -10},
            {"complete": True, "side": "SHORT", "net_pnl": 20},
            {"complete": False, "side": "LONG", "net_pnl": 900},
            {"complete": True, "side": "UNKNOWN", "net_pnl": 50},
        ]
    )

    assert cohorts["LONG"]["position_count"] == 1
    assert cohorts["LONG"]["net_pnl_usdt"] == -10.0
    assert cohorts["SHORT"]["position_count"] == 1
    assert cohorts["SHORT"]["net_pnl_usdt"] == 20.0


def test_open_position_metrics_keep_unrealized_and_stop_risk_separate() -> None:
    metrics = summarize_open_positions(
        [
            {
                "side": "Buy",
                "size": "2",
                "markPrice": "100",
                "stopLoss": "90",
                "unrealisedPnl": "-5",
            },
            {
                "side": "Sell",
                "size": "3",
                "markPrice": "100",
                "stopLoss": "110",
                "unrealisedPnl": "-7",
            },
            {"side": "Buy", "size": "0", "unrealisedPnl": "1000"},
        ]
    )

    assert metrics["position_count"] == 2
    assert metrics["long_count"] == 1
    assert metrics["short_count"] == 1
    assert metrics["unrealized_pnl_usdt"] == -12.0
    assert metrics["positions_with_stop"] == 2
    assert metrics["estimated_stop_risk_usdt"] == 50.0


def test_open_position_stop_risk_is_unavailable_when_any_stop_is_missing() -> None:
    metrics = summarize_open_positions(
        [
            {
                "side": "Buy",
                "size": "1",
                "markPrice": "100",
                "unrealisedPnl": "0",
            }
        ]
    )

    assert metrics["available"] is True
    assert metrics["estimated_stop_risk_usdt"] is None


def test_open_position_stop_risk_ignores_malformed_exchange_numeric_values() -> None:
    metrics = summarize_open_positions(
        [
            {
                "side": "Buy",
                "size": "1",
                "markPrice": "100",
                "stopLoss": [],
                "unrealisedPnl": "0",
            }
        ]
    )

    assert metrics["available"] is True
    assert metrics["positions_with_stop"] == 0
    assert metrics["estimated_stop_risk_usdt"] is None


def test_reconciled_metrics_report_unavailable_without_complete_positions() -> None:
    metrics = summarize_reconciled_positions(
        [{"complete": False, "net_pnl": 12.0, "net_r": 1.0}]
    )

    assert metrics["available"] is False
    assert metrics["position_count"] == 0
    assert metrics["avg_win_usdt"] is None
    assert metrics["avg_loss_usdt"] is None
    assert metrics["avg_initial_risk_win_usdt"] is None
    assert metrics["avg_initial_risk_loss_usdt"] is None


def test_dashboard_builder_keeps_account_rows_separate_from_trade_metrics(
    monkeypatch,
) -> None:
    import importlib.util
    import sqlite3

    exporter_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "export_dashboard_data.py"
    )
    spec = importlib.util.spec_from_file_location(
        "export_dashboard_data", exporter_path
    )
    assert spec is not None and spec.loader is not None
    export_dashboard_data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export_dashboard_data)

    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE account_closed_pnl (closed_pnl REAL);
        CREATE TABLE execution_policy (id INTEGER, risk_per_trade_pct REAL);
        CREATE TABLE account_pnl_sync (
            id INTEGER, history_start_at TEXT, last_synced_at TEXT
        );
        CREATE TABLE intents (intent_id TEXT, status TEXT, error TEXT);
        CREATE TABLE source_messages (id INTEGER);
        CREATE TABLE execution_plans (
            intent_id TEXT, payload_json TEXT, created_at TEXT
        );
        CREATE TABLE position_strategies (id INTEGER);
        CREATE TABLE position_actions (id INTEGER);
        INSERT INTO account_closed_pnl VALUES (300.0);
        INSERT INTO execution_policy VALUES (1, 1.0);
        INSERT INTO account_pnl_sync VALUES (1, '2026-01-01', '2026-10-07');
        """
    )
    database.execute(
        "INSERT INTO intents VALUES (?, ?, ?)",
        ("live-long", "EXECUTED", None),
    )
    database.execute(
        "INSERT INTO execution_plans VALUES (?, ?, ?)",
        (
            "live-long",
            json.dumps(
                {
                    "side": "LONG",
                    "policy": {
                        "risk_per_trade_pct": "0.10",
                        "strategy_v2": {"exit_profile": "payoff_early_trail"},
                    },
                }
            ),
            "2026-10-08T08:50:00+00:00",
        ),
    )
    monkeypatch.setattr(
        export_dashboard_data,
        "reconcile",
        lambda _path: [
            {
                "complete": True,
                "net_pnl": 20.0,
                "net_r": 0.2,
                "side": "LONG",
                "start": "2026-10-08T08:51:00+00:00",
                "risk_per_trade_pct": "0.10",
                "exit_profile": "payoff_early_trail",
            },
            {
                "complete": False,
                "net_pnl": 900.0,
                "net_r": 9.0,
                "start": "2026-10-08T08:52:00+00:00",
                "risk_per_trade_pct": "0.10",
                "exit_profile": "payoff_early_trail",
            },
        ],
    )
    monkeypatch.setattr(
        export_dashboard_data,
        "read_open_positions_snapshot",
        lambda _path: (
            [
                {
                    "side": "Buy",
                    "size": "2",
                    "markPrice": "100",
                    "stopLoss": "90",
                    "unrealisedPnl": "-5",
                }
            ],
            "2026-10-07T21:34:00+00:00",
        ),
    )

    snapshot = export_dashboard_data.build_snapshot(
        database,
        database_source="memory",
        forensic_bundle=Path("forensic.zip"),
        snapshot_at=datetime(2026, 10, 7, tzinfo=UTC),
        benchmark_provider=lambda *_args: None,
        demo_long_risk_multiplier=0.10,
        demo_exit_profile="payoff_early_trail",
    )

    assert snapshot["strategy_pnl"]["position_count"] == 1
    assert snapshot["strategy_pnl"]["avg_win_usdt"] == 20.0
    assert snapshot["strategy_pnl"]["by_side"]["LONG"]["position_count"] == 1
    assert snapshot["account_open_positions"]["unrealized_pnl_usdt"] == -5.0
    assert snapshot["account_open_positions"]["estimated_stop_risk_usdt"] == 20.0
    assert snapshot["account_open_positions"]["snapshot_available"] is True
    assert snapshot["account_open_positions"]["as_of"] == "2026-10-07T21:34:00+00:00"
    exit_experiment = snapshot["risk"]["demo_exit_experiment"]
    assert exit_experiment["active"] is True
    assert exit_experiment["planned_count"] == 1
    assert exit_experiment["executed_plan_count"] == 1
    assert exit_experiment["filled_position_count"] == 2
    assert exit_experiment["completed_position_count"] == 1
    assert exit_experiment["performance"]["position_count"] == 1
    assert snapshot["strategy_pnl"]["performance_bootstrap_95ci"]["circular_block_4"][
        "mean_net_pnl_usdt"
    ] == (20.0, 20.0)
    assert snapshot["engineering"]["intent_outcomes"]["executed"] == 1
    assert snapshot["account_pnl_records"]["record_count"] == 1
    assert snapshot["account_pnl_records"]["avg_win_usdt"] == 300.0
    experiment = snapshot["risk"]["demo_long_experiment"]
    assert experiment["active"] is True
    assert experiment["multiplier"] == 0.10
    assert experiment["effective_long_risk_pct"] == 0.10
    assert experiment["planned_count"] == 1
    assert experiment["executed_plan_count"] == 1
    assert experiment["filled_position_count"] == 1
    assert experiment["completed_position_count"] == 1
    assert experiment["status"] == "collecting"

    without_forensic_bundle = export_dashboard_data.build_snapshot(
        database,
        database_source="memory",
        forensic_bundle=None,
        snapshot_at=datetime(2026, 10, 7, tzinfo=UTC),
        benchmark_provider=lambda *_args: None,
        demo_long_risk_multiplier=0.10,
    )

    assert without_forensic_bundle["strategy_pnl"]["available"] is False
    assert without_forensic_bundle["account_open_positions"]["available"] is False
    assert (
        without_forensic_bundle["account_open_positions"]["snapshot_available"] is False
    )
    assert (
        without_forensic_bundle["risk"]["demo_long_experiment"]["status"]
        == "awaiting_forensic_data"
    )
    assert without_forensic_bundle["account_pnl_records"]["avg_win_usdt"] == 300.0
    database.close()
