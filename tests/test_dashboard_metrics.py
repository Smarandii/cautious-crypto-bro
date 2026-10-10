import json
from datetime import UTC, datetime
from pathlib import Path

from cautious_crypto_bro.dashboard_metrics import (
    summarize_intent_outcomes,
    summarize_open_positions,
    summarize_portfolio_stop_risk,
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
        {
            "complete": True,
            "net_pnl": 40.0,
            "net_r": 0.5,
            "risk_usdt": 20.0,
            "gross_pnl": 41.0,
            "fees": 2.0,
            "funding": 1.0,
        },
        {
            "complete": True,
            "net_pnl": -60.0,
            "net_r": -1.0,
            "risk_usdt": 40.0,
            "gross_pnl": -55.0,
            "fees": 3.0,
            "funding": -2.0,
        },
        {
            "complete": False,
            "net_pnl": 900.0,
            "net_r": 9.0,
            "risk_usdt": 100.0,
            "gross_pnl": 1000.0,
            "fees": 10.0,
            "funding": 10.0,
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
        "gross_price_pnl_usdt": -14.0,
        "fees_usdt": 5.0,
        "signed_funding_usdt": -1.0,
        "fee_share_of_abs_gross_pct": 35.7143,
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
        "avg_win_to_loss_ratio_r": 0.5,
        "break_even_avg_win_r_at_observed_counts": 1.0,
        "break_even_avg_loss_r_at_observed_counts": -0.5,
        "avg_win_r_increase_pct_to_break_even": 100.0,
        "avg_loss_r_reduction_pct_to_break_even": 50.0,
        "break_even_win_rate_r_pct": 66.67,
        "expectancy_r": -0.25,
        "avg_initial_risk_win_usdt": 20.0,
        "avg_initial_risk_loss_usdt": 40.0,
    }


def test_risk_normalized_payoff_is_independent_of_position_size() -> None:
    metrics = summarize_reconciled_positions(
        [
            {
                "complete": True,
                "net_pnl": 50.0,
                "net_r": 0.5,
                "risk_usdt": 100.0,
            },
            {
                "complete": True,
                "net_pnl": -50.0,
                "net_r": -1.0,
                "risk_usdt": 50.0,
            },
        ]
    )

    assert metrics["avg_win_to_loss_ratio"] == 1.0
    assert metrics["avg_win_to_loss_ratio_r"] == 0.5
    assert metrics["break_even_win_rate_r_pct"] == 66.67


def test_risk_normalized_payoff_is_unavailable_without_both_outcomes() -> None:
    metrics = summarize_reconciled_positions(
        [{"complete": True, "net_pnl": 3.0, "net_r": 0.3}]
    )

    assert metrics["avg_win_to_loss_ratio_r"] is None
    assert metrics["break_even_avg_win_r_at_observed_counts"] is None
    assert metrics["break_even_avg_loss_r_at_observed_counts"] is None
    assert metrics["break_even_win_rate_r_pct"] is None


def test_risk_normalized_metrics_are_null_when_all_positions_are_losses() -> None:
    metrics = summarize_reconciled_positions(
        [{"complete": True, "net_pnl": -3.0, "net_r": -0.3}]
    )

    assert metrics["avg_win_to_loss_ratio_r"] is None
    assert metrics["break_even_avg_win_r_at_observed_counts"] is None
    assert metrics["break_even_avg_loss_r_at_observed_counts"] is None
    assert metrics["break_even_win_rate_r_pct"] is None


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


def test_portfolio_stop_risk_includes_remaining_entries_and_reports_cap_excess() -> (
    None
):
    metrics = summarize_portfolio_stop_risk(
        [
            {"side": "Buy", "size": "2", "avgPrice": "100", "stopLoss": "90"},
            {"side": "Sell", "size": "3", "avgPrice": "100", "stopLoss": "110"},
        ],
        [
            {"leavesQty": "4", "price": "105", "stopLoss": "100"},
            {"leavesQty": "2", "price": "90", "stopLoss": "100"},
            {"leavesQty": "10", "reduceOnly": True},
            {"leavesQty": "0", "price": "100", "stopLoss": "90"},
        ],
        cap_usdt=80,
    )

    assert metrics["risk_bounded"] is True
    assert metrics["position_stop_risk_usdt"] == 50.0
    assert metrics["entry_order_count"] == 2
    assert metrics["entry_orders_with_stop"] == 2
    assert metrics["entry_order_stop_risk_usdt"] == 40.0
    assert metrics["combined_stop_risk_usdt"] == 90.0
    assert metrics["remaining_capacity_usdt"] == -10.0
    assert metrics["cap_utilization_pct"] == 112.5
    assert metrics["cap_exceeded"] is True


def test_portfolio_stop_risk_is_unavailable_when_entry_protection_is_missing() -> None:
    metrics = summarize_portfolio_stop_risk(
        [{"side": "Buy", "size": "1", "avgPrice": "100", "stopLoss": "90"}],
        [{"leavesQty": "2", "price": "100"}],
        cap_usdt=100,
    )

    assert metrics["risk_bounded"] is False
    assert metrics["position_stop_risk_usdt"] == 10.0
    assert metrics["entry_order_stop_risk_usdt"] is None
    assert metrics["combined_stop_risk_usdt"] is None
    assert metrics["cap_exceeded"] is None


def test_portfolio_stop_risk_is_unavailable_when_an_entry_has_no_plan() -> None:
    metrics = summarize_portfolio_stop_risk(
        [],
        [],
        cap_usdt=100,
        unmatched_entry_order_count=1,
    )

    assert metrics["risk_bounded"] is False
    assert metrics["unmatched_entry_order_count"] == 1
    assert metrics["combined_stop_risk_usdt"] is None
    assert metrics["cap_exceeded"] is None


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

    monkeypatch.setattr(
        export_dashboard_data,
        "forensic_closed_pnl_updates",
        lambda _bundle, *, last_synced_at: ([], None),
    )

    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE account_closed_pnl (
            record_id TEXT, closed_pnl REAL, closed_at TEXT
        );
        CREATE TABLE execution_policy (id INTEGER, risk_per_trade_pct REAL);
        CREATE TABLE account_pnl_sync (
            id INTEGER, history_start_at TEXT, last_synced_at TEXT
        );
        CREATE TABLE intents (
            intent_id TEXT, status TEXT, approval_mode TEXT, error TEXT
        );
        CREATE TABLE source_messages (id INTEGER);
        CREATE TABLE execution_plans (
            intent_id TEXT, payload_json TEXT, bybit_order_ids_json TEXT,
            created_at TEXT
        );
        CREATE TABLE position_strategies (id INTEGER);
        CREATE TABLE position_actions (id INTEGER);
        INSERT INTO account_closed_pnl VALUES (
            'BTCUSDT:cached', 300.0, '2026-10-06T00:00:00+00:00'
        );
        INSERT INTO execution_policy VALUES (1, 1.0);
        INSERT INTO account_pnl_sync VALUES (1, '2026-01-01', '2026-10-07');
        """
    )
    database.execute(
        "INSERT INTO intents(intent_id, status, error) VALUES (?, ?, ?)",
        ("live-long", "EXECUTED", None),
    )
    database.execute(
        "INSERT INTO execution_plans(intent_id, payload_json, created_at) VALUES (?, ?, ?)",
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
    database.execute(
        "INSERT INTO intents(intent_id, status, error) VALUES (?, ?, ?)",
        ("later-profile-long", "EXECUTED", None),
    )
    database.execute(
        "INSERT INTO execution_plans(intent_id, payload_json, created_at) VALUES (?, ?, ?)",
        (
            "later-profile-long",
            json.dumps(
                {
                    "side": "LONG",
                    "policy": {
                        "risk_per_trade_pct": "0.10",
                        "strategy_v2": {
                            "exit_profile": "payoff_early_tight_trail_long_015"
                        },
                    },
                }
            ),
            "2026-10-08T08:52:00+00:00",
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
            {
                "complete": True,
                "net_pnl": 30.0,
                "net_r": 0.3,
                "side": "LONG",
                "start": "2026-10-08T08:53:00+00:00",
                "risk_per_trade_pct": "0.10",
                "exit_profile": "payoff_early_tight_trail_long_015",
            },
        ],
    )
    monkeypatch.setattr(
        export_dashboard_data,
        "read_open_entry_orders_snapshot",
        lambda _path: (
            [{"leavesQty": "2", "price": "100", "stopLoss": "90"}],
            "2026-10-07T21:34:00+00:00",
            0,
        ),
    )
    monkeypatch.setattr(
        export_dashboard_data,
        "read_open_positions_snapshot",
        lambda _path: (
            [
                {
                    "side": "Buy",
                    "size": "2",
                    "avgPrice": "100",
                    "markPrice": "100",
                    "stopLoss": "90",
                    "unrealisedPnl": "-5",
                }
            ],
            "2026-10-07T21:34:00+00:00",
        ),
    )
    monkeypatch.setattr(
        export_dashboard_data,
        "fill_followthrough_summary",
        lambda _path: {
            "available": True,
            "filled_case_count": 3,
            "by_horizon_minutes": {"60": {"LONG": {"count": 2}}},
        },
    )

    snapshot = export_dashboard_data.build_snapshot(
        database,
        database_source="memory",
        forensic_bundle=Path("forensic.zip"),
        snapshot_at=datetime(2026, 10, 7, tzinfo=UTC),
        benchmark_provider=lambda *_args: None,
        demo_long_risk_multiplier=0.10,
        demo_exit_profile="payoff_early_trail",
        demo_portfolio_stop_risk_cap_usdt=35,
    )

    assert snapshot["strategy_pnl"]["position_count"] == 2
    assert snapshot["strategy_pnl"]["avg_win_usdt"] == 25.0
    assert snapshot["strategy_pnl"]["by_side"]["LONG"]["position_count"] == 2
    assert snapshot["fill_followthrough"]["filled_case_count"] == 3
    assert snapshot["account_open_positions"]["unrealized_pnl_usdt"] == -5.0
    assert snapshot["account_open_positions"]["estimated_stop_risk_usdt"] == 20.0
    assert snapshot["account_open_positions"]["snapshot_available"] is True
    portfolio_risk = snapshot["account_open_positions"]["portfolio_stop_risk"]
    assert portfolio_risk["snapshot_available"] is True
    assert portfolio_risk["position_stop_risk_usdt"] == 20.0
    assert portfolio_risk["entry_order_stop_risk_usdt"] == 20.0
    assert portfolio_risk["combined_stop_risk_usdt"] == 40.0
    assert portfolio_risk["cap_usdt"] == 35.0
    assert portfolio_risk["cap_exceeded"] is True
    assert snapshot["account_open_positions"]["as_of"] == "2026-10-07T21:34:00+00:00"
    exit_experiment = snapshot["risk"]["demo_exit_experiment"]
    assert exit_experiment["active"] is True
    assert exit_experiment["planned_count"] == 1
    assert exit_experiment["executed_plan_count"] == 1
    assert exit_experiment["filled_position_count"] == 2
    assert exit_experiment["completed_position_count"] == 1
    assert exit_experiment["performance"]["position_count"] == 1
    bootstrap_ci = snapshot["strategy_pnl"]["performance_bootstrap_95ci"][
        "circular_block_4"
    ]["mean_net_pnl_usdt"]
    assert bootstrap_ci[0] <= 25.0 <= bootstrap_ci[1]
    assert snapshot["engineering"]["intent_outcomes"]["executed"] == 2
    assert snapshot["account_pnl_records"]["record_count"] == 1
    assert snapshot["account_pnl_records"]["avg_win_usdt"] == 300.0
    assert snapshot["account_pnl_records"]["forensic_archive_supplement_count"] == 0
    experiment = snapshot["risk"]["demo_long_experiment"]
    assert experiment["active"] is True
    assert experiment["multiplier"] == 0.10
    assert experiment["effective_long_risk_pct"] == 0.10
    assert experiment["exit_profile"] == "payoff_early_trail"
    assert experiment["planned_count"] == 1
    assert experiment["executed_plan_count"] == 1
    assert experiment["filled_position_count"] == 1
    assert experiment["completed_position_count"] == 1
    assert experiment["status"] == "collecting"
    tight_trail_snapshot = export_dashboard_data.build_snapshot(
        database,
        database_source="memory",
        forensic_bundle=Path("forensic.zip"),
        snapshot_at=datetime(2026, 10, 7, tzinfo=UTC),
        benchmark_provider=lambda *_args: None,
        demo_long_risk_multiplier=0.10,
        demo_exit_profile="payoff_early_tight_trail",
    )
    tight_trail_experiment = tight_trail_snapshot["risk"]["demo_exit_experiment"]
    assert tight_trail_experiment["active"] is True
    assert tight_trail_experiment["profile"] == "payoff_early_tight_trail"
    assert tight_trail_experiment["planned_count"] == 0
    assert tight_trail_experiment["filled_position_count"] == 0
    assert tight_trail_experiment["completed_position_count"] == 0
    assert tight_trail_experiment["performance"]["available"] is False

    long_015_snapshot = export_dashboard_data.build_snapshot(
        database,
        database_source="memory",
        forensic_bundle=Path("forensic.zip"),
        snapshot_at=datetime(2026, 10, 7, tzinfo=UTC),
        benchmark_provider=lambda *_args: None,
        demo_long_risk_multiplier=0.10,
        demo_exit_profile="payoff_early_tight_trail_long_015",
    )
    long_015_experiment = long_015_snapshot["risk"]["demo_exit_experiment"]
    assert long_015_experiment["active"] is True
    assert long_015_experiment["profile"] == "payoff_early_tight_trail_long_015"
    assert long_015_experiment["planned_count"] == 1
    assert long_015_experiment["completed_position_count"] == 1
    assert long_015_experiment["performance"]["position_count"] == 1

    for intent_id, profile in (
        (
            "exit-ab-control",
            "payoff_early_tight_trail_long_ab_020_control",
        ),
        (
            "exit-ab-treatment",
            "payoff_early_tight_trail_long_ab_015",
        ),
    ):
        database.execute(
            "INSERT INTO intents(intent_id, status, error) VALUES (?, ?, ?)",
            (intent_id, "EXECUTED", None),
        )
        database.execute(
            "INSERT INTO execution_plans(intent_id, payload_json, created_at) VALUES (?, ?, ?)",
            (
                intent_id,
                json.dumps(
                    {
                        "side": "LONG",
                        "policy": {
                            "risk_per_trade_pct": "0.10",
                            "strategy_v2": {"exit_profile": profile},
                        },
                    }
                ),
                "2026-10-09T11:00:00+00:00",
            ),
        )
    existing_positions = export_dashboard_data.reconcile(Path("forensic.zip"))
    randomized_positions = existing_positions + [
        {
            "complete": True,
            "net_pnl": 10.0,
            "net_r": 0.2,
            "side": "LONG",
            "start": "2026-10-09T11:01:00+00:00",
            "risk_per_trade_pct": "0.10",
            "exit_profile": "payoff_early_tight_trail_long_ab_020_control",
        },
        {
            "complete": True,
            "net_pnl": -10.0,
            "net_r": -0.2,
            "side": "LONG",
            "start": "2026-10-09T11:02:00+00:00",
            "risk_per_trade_pct": "0.10",
            "exit_profile": "payoff_early_tight_trail_long_ab_015",
        },
    ]
    monkeypatch.setattr(
        export_dashboard_data,
        "reconcile",
        lambda _path: randomized_positions,
    )

    randomized_snapshot = export_dashboard_data.build_snapshot(
        database,
        database_source="memory",
        forensic_bundle=Path("forensic.zip"),
        snapshot_at=datetime(2026, 10, 9, 12, tzinfo=UTC),
        benchmark_provider=lambda *_args: None,
        demo_long_risk_multiplier=0.10,
        demo_long_exit_control_fraction=0.5,
        demo_exit_profile="payoff_early_tight_trail_long_015",
    )
    arms = randomized_snapshot["risk"]["demo_exit_experiment"]["randomized_long_ab"]
    assert arms["active"] is True
    assert arms["control_fraction"] == 0.5
    assert arms["control"]["completed_position_count"] == 1
    assert arms["control"]["performance"]["expectancy_r"] == 0.2
    assert arms["treatment"]["completed_position_count"] == 1
    assert arms["treatment"]["performance"]["expectancy_r"] == -0.2

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
    assert without_forensic_bundle["fill_followthrough"] is None
    database.close()


def test_open_entry_order_details_compare_age_to_saved_ttl() -> None:
    import importlib.util
    import sqlite3

    exporter_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "export_dashboard_data.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dashboard_ttl_exporter", exporter_path
    )
    assert spec is not None and spec.loader is not None
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    database = sqlite3.connect(":memory:")
    database.execute(
        "CREATE TABLE execution_plans (bybit_order_ids_json TEXT, created_at TEXT, payload_json TEXT)"
    )
    database.execute(
        "INSERT INTO execution_plans VALUES (?, ?, ?)",
        (
            '["old-order"]',
            "2026-10-08T00:00:00+00:00",
            json.dumps({"policy": {"strategy_v2": {"entry_order_ttl_minutes": 240}}}),
        ),
    )
    database.execute(
        "INSERT INTO execution_plans VALUES (?, ?, ?)",
        ('["legacy-order"]', "2026-10-08T00:00:00+00:00", "{}"),
    )
    details = exporter.build_open_entry_order_details(
        database,
        [
            {"orderId": "old-order", "symbol": "BTCUSDT", "createdTime": "0"},
            {"orderId": "legacy-order", "symbol": "ETHUSDT", "createdTime": "0"},
        ],
        datetime(2026, 10, 8, 5, tzinfo=UTC),
    )

    assert details is not None
    assert details[0]["age_minutes"] == 300
    assert details[0]["ttl_status"] == "past_ttl"
    assert details[0]["ttl_minutes"] == 240
    assert details[1]["ttl_status"] == "no_ttl"
    assert details[1]["ttl_minutes"] is None


def test_account_pnl_history_merges_only_covered_archive_updates(tmp_path) -> None:
    import importlib.util
    import sqlite3
    from zipfile import ZipFile

    exporter_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "export_dashboard_data.py"
    )
    spec = importlib.util.spec_from_file_location(
        "export_dashboard_data_archive_merge", exporter_path
    )
    assert spec is not None and spec.loader is not None
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    def closed_row(symbol: str, order_id: str, pnl: float, updated: datetime) -> dict:
        return {
            "symbol": symbol,
            "orderId": order_id,
            "side": "Sell",
            "closedPnl": str(pnl),
            "closedSize": "1",
            "avgEntryPrice": "100",
            "avgExitPrice": "101",
            "createdTime": str(int(updated.timestamp() * 1000)),
            "updatedTime": str(int(updated.timestamp() * 1000)),
        }

    watermark = "2026-10-07T10:00:00+00:00"
    archive_path = tmp_path / "forensic.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "bundle/metadata.json",
            json.dumps(
                {
                    "created_at": "2026-10-08T14:30:00+00:00",
                    "closed_pnl_coverage_start": "2026-10-06T00:00:00+00:00",
                    "closed_pnl_as_of": "2026-10-08T14:20:00+00:00",
                }
            ),
        )
        archive.writestr(
            "bundle/analysis/trade_lineage.jsonl",
            json.dumps({"intents": [{"created_at": "2026-10-07T00:00:00+00:00"}]})
            + "\n",
        )
        rows = [
            closed_row(
                "XRPUSDT",
                "refresh",
                12.0,
                datetime(2026, 10, 8, 11, tzinfo=UTC),
            ),
            closed_row(
                "SOLUSDT",
                "new",
                5.0,
                datetime(2026, 10, 8, 11, 30, tzinfo=UTC),
            ),
            closed_row(
                "DOGEUSDT",
                "stale",
                999.0,
                datetime(2026, 10, 7, 9, tzinfo=UTC),
            ),
        ]
        archive.writestr(
            "bundle/bybit/closed_pnl.jsonl",
            "".join(json.dumps(row) + "\n" for row in rows),
        )

    database = sqlite3.connect(":memory:")
    database.execute(
        "CREATE TABLE account_closed_pnl "
        "(record_id TEXT, closed_pnl REAL, closed_at TEXT)"
    )
    database.executemany(
        "INSERT INTO account_closed_pnl VALUES (?, ?, ?)",
        [
            ("XRPUSDT:refresh", 10.0, "2026-10-07T09:00:00+00:00"),
            ("ADAUSDT:cached", -2.0, "2026-10-06T09:00:00+00:00"),
        ],
    )

    records, archive_as_of, supplemental_count = exporter._account_pnl_history(
        database,
        archive_path,
        watermark,
    )

    assert sorted(records) == [-2.0, 5.0, 12.0]
    assert archive_as_of == "2026-10-08T14:20:00+00:00"
    assert supplemental_count == 2
    database.close()


def test_dashboard_exporter_accepts_explicit_demo_experiment_settings(
    monkeypatch,
) -> None:
    import importlib.util
    import sys

    exporter_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "export_dashboard_data.py"
    )
    spec = importlib.util.spec_from_file_location(
        "export_dashboard_data_cli", exporter_path
    )
    assert spec is not None and spec.loader is not None
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    monkeypatch.setenv("DEMO_LONG_RISK_MULTIPLIER", "1")
    monkeypatch.setenv("DEMO_EXIT_PROFILE", "baseline")
    monkeypatch.setenv("DEMO_PORTFOLIO_STOP_RISK_CAP_USDT", "340")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_dashboard_data.py",
            "--demo-long-risk-multiplier",
            "0.10",
            "--demo-long-exit-control-fraction",
            "0.50",
            "--demo-long-participation-skip-fraction",
            "0.50",
            "--demo-exit-profile",
            "payoff_early_trail",
        ],
    )

    args = exporter.parse_args()

    assert args.demo_long_risk_multiplier == 0.10
    assert args.demo_long_exit_control_fraction == 0.50
    assert args.demo_long_participation_skip_fraction == 0.50
    assert args.demo_exit_profile == "payoff_early_trail"
    assert args.demo_portfolio_stop_risk_cap_usdt == 340.0


def test_participation_dashboard_counts_skips_without_claiming_an_edge() -> None:
    import importlib.util
    import sqlite3

    exporter_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "export_dashboard_data.py"
    )
    spec = importlib.util.spec_from_file_location(
        "export_dashboard_data_participation", exporter_path
    )
    assert spec is not None and spec.loader is not None
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE intents (intent_id TEXT, status TEXT, approval_mode TEXT);
        CREATE TABLE execution_plans (
            intent_id TEXT, payload_json TEXT, bybit_order_ids_json TEXT
        );
        """
    )
    for intent_id, status, approval_mode, arm, order_ids in (
        ("take-1", "EXECUTED", "AUTO", "take", '["order-1"]'),
        ("take-2", "FAILED", "AUTO", "take", '["order-2"]'),
        ("skip-1", "SKIPPED", "SKIPPED", "skip", None),
        ("skip-2", "SKIPPED", "AUTO", "skip", None),
    ):
        database.execute(
            "INSERT INTO intents VALUES (?, ?, ?)",
            (intent_id, status, approval_mode),
        )
        database.execute(
            "INSERT INTO execution_plans VALUES (?, ?, ?)",
            (
                intent_id,
                json.dumps(
                    {
                        "side": "LONG",
                        "policy": {"strategy_v2": {"long_participation_arm": arm}},
                    }
                ),
                order_ids,
            ),
        )

    experiment = exporter._demo_long_participation_experiment(
        database,
        positions=[],
        skip_fraction=0.5,
        forensic_available=True,
    )
    assert experiment["take_assigned_count"] == 2
    assert experiment["skip_assigned_count"] == 2
    assert experiment["skip_records_valid_count"] == 1
    assert experiment["take_unresolved_count"] == 2
    assert experiment["status"] == "collecting"
    assert experiment["take_completed_performance"]["available"] is False
    database.close()


def test_participation_review_uses_resolved_assignments_not_only_filled_trades() -> (
    None
):
    import importlib.util
    import sqlite3

    exporter_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "export_dashboard_data.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dashboard_assignments", exporter_path
    )
    assert spec is not None and spec.loader is not None
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE intents (intent_id TEXT, status TEXT, approval_mode TEXT);
        CREATE TABLE execution_plans (
            intent_id TEXT, payload_json TEXT, bybit_order_ids_json TEXT
        );
        """
    )
    for index in range(20):
        for arm, status, approval_mode in (
            ("take", "FAILED", "AUTO"),
            ("skip", "SKIPPED", "SKIPPED"),
        ):
            intent_id = f"{arm}-{index}"
            database.execute(
                "INSERT INTO intents VALUES (?, ?, ?)",
                (intent_id, status, approval_mode),
            )
            database.execute(
                "INSERT INTO execution_plans VALUES (?, ?, ?)",
                (
                    intent_id,
                    json.dumps(
                        {
                            "side": "LONG",
                            "policy": {"strategy_v2": {"long_participation_arm": arm}},
                        }
                    ),
                    None,
                ),
            )

    experiment = exporter._demo_long_participation_experiment(
        database,
        positions=[],
        skip_fraction=0.5,
        forensic_available=True,
    )
    assert experiment["take_unresolved_count"] == 0
    assert experiment["take_completed_position_count"] == 0
    assert experiment["skip_records_valid_count"] == 20
    assert experiment["status"] == "ready_for_review"
    database.close()
