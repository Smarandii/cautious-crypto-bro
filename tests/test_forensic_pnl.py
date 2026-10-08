"""Synthetic checks for closed-PnL-to-position attribution."""

import importlib.util
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "forensic_pnl",
    Path(__file__).parents[1] / "scripts" / "analyze_forensic_pnl.py",
)
assert spec is not None and spec.loader is not None
analyzer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = analyzer
spec.loader.exec_module(analyzer)


def _write_jsonl(archive, name, rows) -> None:
    archive.writestr(
        f"forensic/{name}",
        "".join(json.dumps(row) + "\n" for row in rows),
    )


def test_read_open_positions_supports_archives_with_and_without_snapshot(
    tmp_path,
) -> None:
    current = tmp_path / "current.zip"
    previous = tmp_path / "previous.zip"
    positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "1"}]
    with zipfile.ZipFile(current, "w") as archive:
        _write_jsonl(archive, "bybit/open_positions.jsonl", positions)
    with zipfile.ZipFile(previous, "w") as archive:
        _write_jsonl(archive, "metadata.json", [{"created_at": "2026-01-01"}])

    assert analyzer.read_open_positions(current) == positions
    assert analyzer.read_open_positions_snapshot(current) == (
        positions,
        None,
    )
    assert analyzer.read_open_positions(previous) == []


def test_reconcile_does_not_double_count_funding_in_closed_pnl(tmp_path) -> None:
    bundle = tmp_path / "forensic.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        _write_jsonl(
            archive,
            "analysis/trade_lineage.jsonl",
            [
                {
                    "intents": [
                        {
                            "intent_id": "intent-1",
                            "entry_order_ids": ["entry-1"],
                            "metadata": {
                                "source_channel_id": 123,
                                "confidence": 0.99,
                                "entry_type": "MARKET",
                                "risk_per_trade_pct": "1",
                                "planned_max_loss_usdt": "20",
                                "exit_profile": "payoff_challenger",
                            },
                            "intent": {
                                "symbol": "BTCUSDT",
                                "side": "LONG",
                                "stop_loss": "90",
                            },
                        }
                    ]
                }
            ],
        )
        _write_jsonl(
            archive,
            "bybit/executions.jsonl",
            [
                {
                    "execType": "Trade",
                    "orderId": "entry-1",
                    "execTime": "1000",
                    "execQty": "2",
                    "execPrice": "100",
                    "closedSize": "0",
                    "orderLinkId": "strategy-E1",
                }
            ],
        )
        _write_jsonl(
            archive,
            "bybit/closed_pnl.jsonl",
            [
                {
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "updatedTime": "2000",
                    "closedSize": "2",
                    "closedPnl": "17",
                    "openFee": "0.25",
                    "closeFee": "0.25",
                }
            ],
        )
        _write_jsonl(
            archive,
            "bybit/transaction_log.jsonl",
            [
                {
                    "type": "SETTLEMENT",
                    "symbol": "BTCUSDT",
                    "transactionTime": "1500",
                    "funding": "-1",
                }
            ],
        )

    positions = analyzer.reconcile(bundle)

    assert len(positions) == 1
    assert positions[0]["complete"] is True
    assert positions[0]["closed_qty"] == 2
    assert positions[0]["closed_pnl"] == 17
    assert positions[0]["closed_at"] == 2000
    assert positions[0]["funding"] == -1
    assert positions[0]["net_pnl"] == 17
    assert positions[0]["fees"] == 0.5
    assert positions[0]["gross_pnl"] == 18.5
    assert positions[0]["fill_pattern"] == "E1"
    assert positions[0]["source_channel_id"] == 123
    assert positions[0]["confidence"] == 0.99
    assert positions[0]["entry_type"] == "MARKET"
    assert positions[0]["planned_max_loss_usdt"] == 20
    assert positions[0]["exit_profile"] == "payoff_challenger"
    assert positions[0]["risk_usdt"] == 20
    assert positions[0]["net_r"] == 0.85
    summary = analyzer.summarize(positions)
    assert summary["risk_weighted_return_pct"] == 85
    assert summary["avg_initial_risk_win_usdt"] == 20
    assert summary["avg_planned_max_loss_usdt"] == 20
    assert summary["breakeven_win_rate_r_pct"] is None
    assert summary["expectancy_r"] == 0.85
    assert summary["expectancy_usdt"] == 17
    assert summary["avg_win_r"] == 0.85
    costs = analyzer.cost_summary(positions)
    assert costs["gross_price_pnl_usdt"] == 18.5
    assert costs["fees_usdt"] == 0.5
    assert costs["signed_funding_usdt"] == -1
    assert costs["closed_pnl_net_usdt"] == 17
    assert costs["winner_fee_pct_of_avg_net_win"] == 100 * 0.5 / 17


def test_prospective_demo_exit_profile_counts_only_complete_tagged_positions(
    capsys,
) -> None:
    analyzer.report_prospective_demo_exit_profile(
        [
            {
                "exit_profile": "payoff_early_trail",
                "complete": True,
                "net_pnl": 10.0,
                "net_r": 0.5,
                "risk_usdt": 20.0,
                "start": 1,
            },
            {
                "exit_profile": "payoff_early_trail",
                "complete": False,
                "net_pnl": 100.0,
                "net_r": 5.0,
                "risk_usdt": 20.0,
                "start": 2,
            },
            {
                "exit_profile": "baseline",
                "complete": True,
                "net_pnl": -10.0,
                "net_r": -0.5,
                "risk_usdt": 20.0,
                "start": 3,
            },
        ]
    )

    output = capsys.readouterr().out
    assert "filled_positions=2" in output
    assert "completed_positions=1" in output
    assert "collecting/target:20" in output
    assert '"expectancy_r": 0.5' in output


def test_wilson_interval_is_wide_for_small_samples_and_contains_rate() -> None:
    interval = analyzer.wilson_interval(25, 32)

    assert interval is not None
    assert interval[0] < 100 * 25 / 32 < interval[1]
    assert interval[0] < 72.5 < interval[1]
    assert analyzer.wilson_interval(0, 0) is None


def test_direction_risk_sensitivity_scales_long_pnl_and_risk_only() -> None:
    scenarios = analyzer.direction_risk_sensitivity(
        [
            {"start": 1, "side": "LONG", "net_pnl": -40.0, "risk_usdt": 100.0},
            {"start": 2, "side": "SHORT", "net_pnl": 30.0, "risk_usdt": 100.0},
        ],
        long_multipliers=(1.0, 0.25, 0.0),
        bootstrap_iterations=500,
        seed=42,
    )

    assert [
        {
            key: value
            for key, value in scenario.items()
            if key != "iid_bootstrap_95ci_pct"
        }
        for scenario in scenarios
    ] == [
        {
            "long_risk_multiplier": 1.0,
            "observed_positions": 2,
            "net_pnl_usdt": -10.0,
            "initial_risk_usdt": 200.0,
            "risk_weighted_return_pct": -5.0,
        },
        {
            "long_risk_multiplier": 0.25,
            "observed_positions": 2,
            "net_pnl_usdt": 20.0,
            "initial_risk_usdt": 125.0,
            "risk_weighted_return_pct": 16.0,
        },
        {
            "long_risk_multiplier": 0.0,
            "observed_positions": 2,
            "net_pnl_usdt": 30.0,
            "initial_risk_usdt": 100.0,
            "risk_weighted_return_pct": 30.0,
        },
    ]
    assert (
        scenarios[1]["iid_bootstrap_95ci_pct"]
        == analyzer.direction_risk_sensitivity(
            [
                {"side": "LONG", "net_pnl": -40.0, "risk_usdt": 100.0},
                {"side": "SHORT", "net_pnl": 30.0, "risk_usdt": 100.0},
            ],
            long_multipliers=(1.0, 0.25, 0.0),
            bootstrap_iterations=500,
            seed=42,
        )[1]["iid_bootstrap_95ci_pct"]
    )
    interval = scenarios[1]["iid_bootstrap_95ci_pct"]
    assert interval is not None and interval[0] <= 16 <= interval[1]
    assert scenarios[2]["iid_bootstrap_95ci_pct"] == (30.0, 30.0)


def test_direction_risk_sensitivity_returns_undefined_when_scaled_risk_is_zero() -> (
    None
):
    scenario = analyzer.direction_risk_sensitivity(
        [{"side": "LONG", "net_pnl": -40.0, "risk_usdt": 100.0}],
        long_multipliers=(0.0,),
        bootstrap_iterations=10,
    )[0]

    assert scenario["risk_weighted_return_pct"] is None
    assert scenario["iid_bootstrap_95ci_pct"] is None


def test_direction_risk_sensitivity_rejects_increase_above_baseline() -> None:
    try:
        analyzer.direction_risk_sensitivity([], long_multipliers=(1.1,))
    except ValueError as exc:
        assert "between 0 and 1" in str(exc)
    else:
        raise AssertionError("expected an invalid long risk multiplier to fail")


def test_prospective_long_risk_keeps_only_postfreeze_reconciled_positions() -> None:
    cutoff = int(
        datetime.fromisoformat(analyzer.LONG_RISK_SHADOW_FROZEN_AFTER).timestamp()
        * 1000
    )
    positions = [
        {"start": cutoff, "complete": True, "risk_per_trade_pct": "1"},
        {"start": cutoff + 1, "complete": False, "risk_per_trade_pct": "1"},
        {"start": cutoff + 2, "complete": True, "risk_per_trade_pct": "1"},
        {"start": cutoff + 3, "complete": True, "risk_per_trade_pct": "0.25"},
    ]

    assert analyzer.prospective_long_risk_positions(positions) == [positions[-2]]


def test_prospective_long_risk_curve_uses_its_own_freeze_time() -> None:
    cutoff = int(
        datetime.fromisoformat(analyzer.LONG_RISK_CURVE_FROZEN_AFTER).timestamp() * 1000
    )
    positions = [
        {"start": cutoff, "complete": True, "risk_per_trade_pct": "1"},
        {"start": cutoff + 1, "complete": False, "risk_per_trade_pct": "1"},
        {"start": cutoff + 2, "complete": True, "risk_per_trade_pct": "1"},
        {"start": cutoff + 3, "complete": True, "risk_per_trade_pct": "0.25"},
    ]

    assert analyzer.prospective_long_risk_curve_positions(positions) == [positions[-2]]


def test_live_long_risk_cohort_excludes_paper_and_historical_positions() -> None:
    live_cutoff = int(
        datetime.fromisoformat(analyzer.LIVE_LONG_RISK_STARTED_AT).timestamp() * 1000
    )
    freeze_cutoff = int(
        datetime.fromisoformat(analyzer.LONG_RISK_SHADOW_FROZEN_AFTER).timestamp()
        * 1000
    )
    positions = [
        {
            "start": freeze_cutoff + 1,
            "complete": True,
            "side": "LONG",
            "risk_per_trade_pct": "1",
        },
        {
            "start": live_cutoff - 1,
            "complete": True,
            "side": "LONG",
            "risk_per_trade_pct": "0.10",
        },
        {
            "start": live_cutoff + 1,
            "complete": True,
            "side": "LONG",
            "risk_per_trade_pct": "0.10",
        },
        {
            "start": live_cutoff + 2,
            "complete": True,
            "side": "LONG",
            "risk_per_trade_pct": "0.25",
        },
        {
            "start": live_cutoff + 3,
            "complete": True,
            "side": "SHORT",
            "risk_per_trade_pct": "1",
        },
        {
            "start": live_cutoff + 4,
            "complete": False,
            "side": "LONG",
            "risk_per_trade_pct": "0.25",
        },
    ]

    assert analyzer.prospective_long_risk_positions(positions) == [
        positions[0],
        positions[4],
    ]
    assert analyzer.prospective_live_long_risk_positions(positions) == [positions[2]]


def test_live_long_risk_report_identifies_actual_cohort_and_control(capsys) -> None:
    cutoff = int(
        datetime.fromisoformat(analyzer.LIVE_LONG_RISK_STARTED_AT).timestamp() * 1000
    )
    position = {
        "start": cutoff + 1,
        "complete": True,
        "side": "LONG",
        "risk_per_trade_pct": "0.10",
        "net_pnl": 5.0,
        "net_r": 0.2,
        "risk_usdt": 25.0,
        "planned_max_loss_usdt": 25.0,
    }
    analyzer.report_prospective_live_long_risk([position])
    output = capsys.readouterr().out

    assert "prospective_live_long_risk_long_cases=1" in output
    assert "prospective_live_long_risk_short_controls=0" in output
    assert "prospective_live_long_risk_status=collecting/long_target:20" in output
    assert '"expectancy_r": 0.2' in output


def test_analyzer_help_lists_live_experiment_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["analyze_forensic_pnl.py", "--help"])
    try:
        analyzer.main()
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("help should exit successfully")

    output = capsys.readouterr().out
    assert "--prospective-live-long-risk" in output
    assert "actual completed Demo LONGs" in output


def test_long_risk_curve_reports_all_prespecified_multipliers(capsys) -> None:
    analyzer.report_prospective_long_risk_curve_shadow(
        [
            {"start": 1, "side": "LONG", "net_pnl": -40.0, "risk_usdt": 100.0},
            {"start": 2, "side": "SHORT", "net_pnl": 30.0, "risk_usdt": 100.0},
        ]
    )

    output = capsys.readouterr().out

    assert "multipliers=0.00,0.25,0.50,1.00" in output
    assert '"long_risk_multiplier": 0.25, "net_pnl_usdt": 20.0' in output
    assert '"risk_weighted_return_pct": 16.0' in output
    assert '"long_risk_multiplier": 0.0, "net_pnl_usdt": 30.0' in output
    assert "prospective_long_risk_curve_long_cases=1" in output
    assert "prospective_long_risk_curve_status=collecting/long_target:20" in output


def test_long_risk_curve_review_target_counts_affected_long_trades(capsys) -> None:
    positions = [
        {
            "start": index,
            "side": "LONG" if index < 10 else "SHORT",
            "net_pnl": 0.0,
            "risk_usdt": 100.0,
        }
        for index in range(20)
    ]
    analyzer.report_prospective_long_risk_curve_shadow(positions)
    output = capsys.readouterr().out

    assert "prospective_long_risk_curve_cases=20" in output
    assert "prospective_long_risk_curve_long_cases=10" in output
    assert "prospective_long_risk_curve_status=collecting/long_target:20" in output

    long_only = [
        {"start": index, "side": "LONG", "net_pnl": 0.0, "risk_usdt": 100.0}
        for index in range(20)
    ]
    analyzer.report_prospective_long_risk_curve_shadow(long_only)
    output = capsys.readouterr().out
    assert (
        "prospective_long_risk_curve_status=ready_for_review/long_target:20" in output
    )


def test_paired_long_risk_delta_reports_concentration_and_block_interval() -> None:
    positions = [
        {"start": 1, "side": "LONG", "net_pnl": -100.0},
        {"start": 2, "side": "LONG", "net_pnl": -50.0},
        {"start": 3, "side": "LONG", "net_pnl": 40.0},
        {"start": 4, "side": "SHORT", "net_pnl": -25.0},
    ]

    summary = analyzer._paired_long_risk_delta_summary(
        positions,
        0.25,
        iterations=200,
        block_size=4,
    )

    assert summary["delta_net_pnl_usdt"] == 82.5
    assert summary["positive_delta_positions"] == 2
    assert summary["negative_delta_positions"] == 1
    assert summary["top_three_positive_delta_usdt"] == 112.5
    assert summary["delta_ex_top_three_positive_usdt"] == -30.0
    assert summary["circular_block_4_bootstrap_95ci_usdt"] == (82.5, 82.5)


def test_source_side_health_uses_only_prior_closed_trades() -> None:
    positions = [
        {
            "start": 10,
            "closed_at": 100,
            "complete": True,
            "source_channel_id": 7,
            "side": "LONG",
            "net_r": -0.2,
        },
        {
            "start": 110,
            "closed_at": 200,
            "complete": True,
            "source_channel_id": 7,
            "side": "LONG",
            "net_r": -0.2,
        },
        {
            "start": 210,
            "closed_at": 500,
            "complete": True,
            "source_channel_id": 7,
            "side": "LONG",
            "net_r": -0.2,
        },
        {
            "start": 300,
            "closed_at": 600,
            "complete": True,
            "source_channel_id": 7,
            "side": "LONG",
            "net_r": 0.5,
        },
        {
            "start": 510,
            "closed_at": 700,
            "complete": True,
            "source_channel_id": 7,
            "side": "LONG",
            "net_r": 0.1,
        },
    ]

    allowed, suppressed = analyzer.source_side_health_decisions(positions)

    assert positions[3] in allowed
    assert positions[4] in suppressed


def test_source_side_health_allows_unknown_sources_and_incomplete_history() -> None:
    positions = [
        {
            "start": 10,
            "closed_at": 100,
            "complete": True,
            "source_channel_id": 7,
            "side": "SHORT",
            "net_r": -1.0,
        },
        {
            "start": 110,
            "closed_at": 200,
            "complete": True,
            "source_channel_id": 7,
            "side": "SHORT",
            "net_r": -1.0,
        },
        {
            "start": 210,
            "closed_at": 300,
            "complete": True,
            "source_channel_id": None,
            "side": "SHORT",
            "net_r": -1.0,
        },
    ]

    allowed, suppressed = analyzer.source_side_health_decisions(positions)

    assert allowed == positions
    assert suppressed == []


def test_prospective_source_side_health_keeps_only_postfreeze_positions() -> None:
    cutoff = int(
        datetime.fromisoformat(
            analyzer.SOURCE_SIDE_HEALTH_SHADOW_FROZEN_AFTER
        ).timestamp()
        * 1000
    )
    historical = [
        {
            "start": cutoff - 300,
            "closed_at": cutoff - 200,
            "complete": True,
            "source_channel_id": 7,
            "side": "LONG",
            "net_r": -0.5,
        },
        {
            "start": cutoff - 190,
            "closed_at": cutoff - 100,
            "complete": True,
            "source_channel_id": 7,
            "side": "LONG",
            "net_r": -0.5,
        },
        {
            "start": cutoff - 90,
            "closed_at": cutoff - 10,
            "complete": True,
            "source_channel_id": 7,
            "side": "LONG",
            "net_r": -0.5,
        },
    ]
    future = {
        "start": cutoff + 1,
        "closed_at": cutoff + 2,
        "complete": True,
        "source_channel_id": 7,
        "side": "LONG",
        "net_r": -0.1,
    }

    allowed, suppressed = analyzer.prospective_source_side_health_positions(
        [*historical, future]
    )

    assert allowed == []
    assert suppressed == [future]


def test_scale_long_risk_changes_only_long_dollar_pnl_and_risk() -> None:
    positions = [
        {
            "side": "LONG",
            "net_pnl": -40.0,
            "risk_usdt": 100.0,
            "planned_max_loss_usdt": 100.0,
            "net_r": -0.4,
        },
        {
            "side": "SHORT",
            "net_pnl": 30.0,
            "risk_usdt": 100.0,
            "planned_max_loss_usdt": 100.0,
            "net_r": 0.3,
        },
    ]

    scaled = analyzer.scale_long_risk(positions, 0.25)

    assert scaled[0]["net_pnl"] == -10.0
    assert scaled[0]["risk_usdt"] == 25.0
    assert scaled[0]["planned_max_loss_usdt"] == 25.0
    assert scaled[0]["net_r"] == -0.4
    assert scaled[1] == positions[1]
    assert positions[0]["net_pnl"] == -40.0


def test_portfolio_cap_scales_new_overlapping_risk_and_releases_at_close() -> None:
    positions = [
        {
            "complete": True,
            "start": 1,
            "closed_at": 5,
            "net_pnl": -10.0,
            "risk_usdt": 10.0,
            "planned_max_loss_usdt": 10.0,
        },
        {
            "complete": True,
            "start": 2,
            "closed_at": 4,
            "net_pnl": 6.0,
            "risk_usdt": 10.0,
            "planned_max_loss_usdt": 10.0,
        },
        {
            "complete": True,
            "start": 5,
            "closed_at": 7,
            "net_pnl": 4.0,
            "risk_usdt": 10.0,
            "planned_max_loss_usdt": 10.0,
        },
    ]

    [result] = analyzer.portfolio_risk_cap_sensitivity(
        positions,
        cap_multiples=(1.5,),
        bootstrap_iterations=100,
        seed=23,
    )

    assert result["cap_usdt"] == 15.0
    assert result["net_pnl_usdt"] == -3.0
    assert result["baseline_net_pnl_usdt"] == 0.0
    assert result["delta_usdt"] == -3.0
    assert result["scaled_positions"] == 1
    assert result["splits"]["train"]["scaled_positions"] == 0
    assert result["splits"]["validation"]["scaled_positions"] == 1
    assert result["splits"]["holdout"]["scaled_positions"] == 0


def test_portfolio_cap_requires_positive_caps_and_bootstrap_count() -> None:
    for kwargs in (
        {"cap_multiples": (0.0,)},
        {"bootstrap_iterations": 0},
    ):
        try:
            analyzer.portfolio_risk_cap_sensitivity([], **kwargs)
        except ValueError:
            continue
        raise AssertionError("expected invalid portfolio-cap parameters to fail")


def test_bootstrap_performance_intervals_are_deterministic_and_include_metrics() -> (
    None
):
    positions = [
        {"start": index, "net_pnl": pnl, "net_r": risk_return}
        for index, (pnl, risk_return) in enumerate(
            [(10.0, 0.2), (15.0, 0.3), (-20.0, -0.4), (-25.0, -0.5)]
        )
    ]

    first = analyzer.bootstrap_performance_intervals(
        positions, iterations=200, block_size=2, seed=17
    )
    second = analyzer.bootstrap_performance_intervals(
        positions, iterations=200, block_size=2, seed=17
    )

    assert first == second
    assert set(first) == {"iid", "circular_block_2"}
    assert set(first["iid"]) == {
        "mean_net_r",
        "mean_net_pnl_usdt",
        "avg_win_loss_ratio",
    }
    assert all(interval is not None for interval in first["iid"].values())


def test_bootstrap_performance_intervals_return_none_for_empty_sample() -> None:
    intervals = analyzer.bootstrap_performance_intervals([])

    assert intervals["iid"]["mean_net_r"] is None
    assert intervals["circular_block_4"]["mean_net_pnl_usdt"] is None


def test_bootstrap_performance_intervals_reject_invalid_parameters() -> None:
    for parameters in ({"iterations": 0}, {"block_size": 0}):
        try:
            analyzer.bootstrap_performance_intervals([], **parameters)
        except ValueError:
            continue
        raise AssertionError("expected invalid bootstrap parameters to fail")
