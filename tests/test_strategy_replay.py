"""Guard the replay's walk-forward metrics and candidate coverage."""

import importlib.util
import io
import json
import sys
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cautious_crypto_bro.domain import StrategyV2Policy

spec = importlib.util.spec_from_file_location(
    "strategy_replay",
    Path(__file__).parents[1] / "scripts" / "replay_strategy_v2.py",
)
assert spec is not None and spec.loader is not None
replay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = replay
spec.loader.exec_module(replay)


def test_summary_uses_closed_r_results_and_chronological_drawdown() -> None:
    results = [
        replay.Result(start=1, net_r=1.0, mfe_r=1.0, stopped=False),
        replay.Result(start=2, net_r=-1.5, mfe_r=0.2, stopped=True),
        replay.Result(start=3, net_r=0.5, mfe_r=0.7, stopped=False),
    ]

    summary = replay.summarize_results(results)

    assert summary["net_r"] == 0.0
    assert summary["expectancy_r"] == 0.0
    assert summary["win_rate"] == 2 / 3
    assert summary["avg_win_r"] == 0.75
    assert summary["avg_loss_r"] == -1.5
    assert summary["profit_factor"] == 1.0
    assert summary["max_drawdown_r"] == 1.5


def test_summarize_by_side_keeps_directional_samples_separate() -> None:
    cases = [
        {"side": "LONG"},
        {"side": "SHORT"},
        {"side": "SHORT"},
    ]
    results = [
        replay.Result(start=1, net_r=-1.0, mfe_r=0.0, stopped=True),
        replay.Result(start=2, net_r=0.5, mfe_r=0.5, stopped=False),
        replay.Result(start=3, net_r=1.5, mfe_r=1.5, stopped=False),
    ]

    summary = replay.summarize_by_side(cases, results)

    assert summary["LONG"]["count"] == 1
    assert summary["LONG"]["net_r"] == -1.0
    assert summary["SHORT"]["count"] == 2
    assert summary["SHORT"]["net_r"] == 2.0


def test_candidate_grid_matches_production_policy_constraints() -> None:
    candidates = list(replay.candidates())

    assert candidates
    assert all(candidate.weights[2] > 0 for candidate in candidates)
    assert all(
        Decimal(str(candidate.trail_by)) < Decimal(str(candidate.trail_at))
        and Decimal(str(candidate.trail_at)) - Decimal(str(candidate.trail_by))
        >= Decimal("0.05")
        for candidate in candidates
    )
    assert any(
        candidate.trail_at == 0.5 and candidate.trail_by == 0.45
        for candidate in candidates
    )
    for candidate in candidates:
        StrategyV2Policy(
            primary_entry_risk_pct=Decimal(str(candidate.weights[0] * 100)),
            secondary_entry_risk_pct=Decimal(str(candidate.weights[1] * 100)),
            tertiary_entry_risk_pct=Decimal(str(candidate.weights[2] * 100)),
            secondary_entry_depth_r=Decimal(str(candidate.depths[0])),
            tertiary_entry_depth_r=Decimal(str(candidate.depths[1])),
            first_take_profit_r=Decimal(str(candidate.take_profit_rs[0])),
            second_take_profit_r=Decimal(str(candidate.take_profit_rs[1])),
            third_take_profit_r=Decimal(str(candidate.take_profit_rs[2])),
            first_take_profit_pct=Decimal(str(candidate.take_profit_pcts[0])),
            second_take_profit_pct=Decimal(str(candidate.take_profit_pcts[1])),
            third_take_profit_pct=Decimal(str(candidate.take_profit_pcts[2])),
            runner_pct=Decimal(str(100 - sum(candidate.take_profit_pcts))),
            trailing_activation_r=Decimal(str(candidate.trail_at)),
            trailing_distance_r=Decimal(str(candidate.trail_by)),
        )
    assert (0.70, 0.20, 0.10) in {candidate.weights for candidate in candidates}
    assert (0.80, 0.15, 0.05) in {candidate.weights for candidate in candidates}
    assert (0.75, 0.20, 0.05) in {candidate.weights for candidate in candidates}
    assert all(
        candidate.weights[2] == 0 for candidate in replay.no_e3_counterfactuals()
    )
    assert replay.current_policy().weights == (0.60, 0.25, 0.15)


def test_frozen_reduced_e3_late_target_challenger_is_policy_valid() -> None:
    candidate = replay.late_target_reduced_e3_candidate()

    policy = StrategyV2Policy(
        primary_entry_risk_pct=Decimal(str(candidate.weights[0] * 100)),
        secondary_entry_risk_pct=Decimal(str(candidate.weights[1] * 100)),
        tertiary_entry_risk_pct=Decimal(str(candidate.weights[2] * 100)),
        secondary_entry_depth_r=Decimal(str(candidate.depths[0])),
        tertiary_entry_depth_r=Decimal(str(candidate.depths[1])),
        first_take_profit_r=Decimal(str(candidate.take_profit_rs[0])),
        second_take_profit_r=Decimal(str(candidate.take_profit_rs[1])),
        third_take_profit_r=Decimal(str(candidate.take_profit_rs[2])),
        first_take_profit_pct=Decimal(str(candidate.take_profit_pcts[0])),
        second_take_profit_pct=Decimal(str(candidate.take_profit_pcts[1])),
        third_take_profit_pct=Decimal(str(candidate.take_profit_pcts[2])),
        runner_pct=Decimal(str(100 - sum(candidate.take_profit_pcts))),
        trailing_activation_r=Decimal(str(candidate.trail_at)),
        trailing_distance_r=Decimal(str(candidate.trail_by)),
    )

    assert policy.tertiary_entry_risk_pct == Decimal("5.0")
    assert policy.runner_pct == Decimal("40.0")


def test_frozen_exit_only_candidate_preserves_current_entry_allocation() -> None:
    candidate = replay.exit_only_candidate()

    assert candidate.weights == replay.current_policy().weights
    assert candidate.depths == replay.current_policy().depths
    assert candidate.trail_at == 0.40
    assert candidate.trail_by == 0.30
    assert candidate.take_profit_rs == (1.0, 2.0, 4.0)
    assert candidate.take_profit_pcts == (15.0, 20.0, 25.0)


def test_live_demo_payoff_candidate_matches_deployed_profile() -> None:
    candidate = replay.live_demo_payoff_exit_candidate()

    assert candidate.weights == (0.60, 0.25, 0.15)
    assert candidate.depths == (0.33, 0.66)
    assert candidate.trail_at == 0.40
    assert candidate.trail_by == 0.10
    assert candidate.take_profit_rs == (1.0, 2.0, 4.0)
    assert candidate.take_profit_pcts == (15.0, 20.0, 25.0)


def test_live_demo_early_trail_candidate_matches_separate_experiment() -> None:
    candidate = replay.live_demo_early_trail_candidate()

    assert candidate.weights == (0.60, 0.25, 0.15)
    assert candidate.depths == (0.33, 0.66)
    assert candidate.trail_at == 0.20
    assert candidate.trail_by == 0.10
    assert candidate.take_profit_rs == (1.0, 2.0, 4.0)
    assert candidate.take_profit_pcts == (15.0, 20.0, 25.0)


def test_live_demo_payoff_candidate_with_reduced_e3_is_policy_valid() -> None:
    candidate = replay.live_demo_payoff_exit_reduced_e3_candidate()

    assert candidate.weights == (0.70, 0.25, 0.05)
    assert candidate.depths == (0.33, 0.66)
    assert candidate.trail_at == 0.40
    assert candidate.trail_by == 0.10
    policy = StrategyV2Policy(
        primary_entry_risk_pct=Decimal(str(candidate.weights[0] * 100)),
        secondary_entry_risk_pct=Decimal(str(candidate.weights[1] * 100)),
        tertiary_entry_risk_pct=Decimal(str(candidate.weights[2] * 100)),
        secondary_entry_depth_r=Decimal(str(candidate.depths[0])),
        tertiary_entry_depth_r=Decimal(str(candidate.depths[1])),
        trailing_activation_r=Decimal(str(candidate.trail_at)),
        trailing_distance_r=Decimal(str(candidate.trail_by)),
    )

    assert policy.tertiary_entry_risk_pct == Decimal("5.0")
    assert candidate.take_profit_rs == (1.0, 2.0, 4.0)
    assert candidate.take_profit_pcts == (15.0, 20.0, 25.0)


def test_frozen_early_trail_candidate_changes_only_trailing_geometry() -> None:
    candidate = replay.early_trail_candidate()
    policy = StrategyV2Policy(
        primary_entry_risk_pct=Decimal(str(candidate.weights[0] * 100)),
        secondary_entry_risk_pct=Decimal(str(candidate.weights[1] * 100)),
        tertiary_entry_risk_pct=Decimal(str(candidate.weights[2] * 100)),
        secondary_entry_depth_r=Decimal(str(candidate.depths[0])),
        tertiary_entry_depth_r=Decimal(str(candidate.depths[1])),
        first_take_profit_r=Decimal(str(candidate.take_profit_rs[0])),
        second_take_profit_r=Decimal(str(candidate.take_profit_rs[1])),
        third_take_profit_r=Decimal(str(candidate.take_profit_rs[2])),
        first_take_profit_pct=Decimal(str(candidate.take_profit_pcts[0])),
        second_take_profit_pct=Decimal(str(candidate.take_profit_pcts[1])),
        third_take_profit_pct=Decimal(str(candidate.take_profit_pcts[2])),
        runner_pct=Decimal(str(100 - sum(candidate.take_profit_pcts))),
        trailing_activation_r=Decimal(str(candidate.trail_at)),
        trailing_distance_r=Decimal(str(candidate.trail_by)),
    )

    assert candidate.weights == replay.current_policy().weights
    assert candidate.depths == replay.current_policy().depths
    assert candidate.take_profit_rs == replay.current_policy().take_profit_rs
    assert candidate.take_profit_pcts == replay.current_policy().take_profit_pcts
    assert candidate.trail_at == 0.20
    assert candidate.trail_by == 0.10
    assert policy.minimum_locked_profit_r == Decimal("0.05")


def test_prospective_early_trail_shadow_uses_its_own_freeze_time() -> None:
    cutoff = int(
        datetime.fromisoformat(replay.EARLY_TRAIL_FROZEN_AFTER).timestamp() * 1000
    )
    cases = [{"start": cutoff}, {"start": cutoff + 1}]

    selected = replay.prospective_shadow_cases(
        cases,
        {cutoff, cutoff + 1},
        frozen_after=replay.EARLY_TRAIL_FROZEN_AFTER,
    )

    assert selected == [{"start": cutoff + 1}]


def test_prospective_early_trail_shadow_freezes_only_the_new_exit_profile(
    monkeypatch,
) -> None:
    captured = {}

    def capture_candidate(_cases, _fee_rate, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        replay,
        "report_prospective_candidate_shadow",
        capture_candidate,
    )

    replay.report_prospective_early_trail_shadow([], 0.00055)

    assert captured["frozen_after"] == replay.EARLY_TRAIL_FROZEN_AFTER
    assert captured["challenger"] == replay.early_trail_candidate()


def test_frozen_depth_exit_candidate_has_expected_policy_geometry() -> None:
    candidate = replay.depth_exit_candidate()

    assert candidate.weights == (0.60, 0.25, 0.15)
    assert candidate.depths == (0.25, 0.50)
    assert candidate.trail_at == 0.40
    assert candidate.trail_by == 0.30
    assert candidate.take_profit_rs == (1.0, 2.0, 4.0)
    assert candidate.take_profit_pcts == (15.0, 20.0, 25.0)


def test_prospective_depth_exit_shadow_uses_its_own_freeze_time() -> None:
    cutoff = int(
        datetime.fromisoformat(replay.DEPTH_EXIT_FROZEN_AFTER).timestamp() * 1000
    )
    cases = [{"start": cutoff}, {"start": cutoff + 1}]

    selected = replay.prospective_shadow_cases(
        cases,
        {cutoff, cutoff + 1},
        frozen_after=replay.DEPTH_EXIT_FROZEN_AFTER,
    )

    assert selected == [{"start": cutoff + 1}]


def test_prospective_depth_exit_shadow_freezes_selected_profile(monkeypatch) -> None:
    captured = {}

    def capture_candidate(_cases, _fee_rate, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        replay,
        "report_prospective_candidate_shadow",
        capture_candidate,
    )

    replay.report_prospective_depth_exit_shadow([], 0.00055)

    assert captured["frozen_after"] == replay.DEPTH_EXIT_FROZEN_AFTER
    assert captured["challenger"] == replay.depth_exit_candidate()


def test_prospective_shadow_only_keeps_post_freeze_completed_cases() -> None:
    cutoff = int(
        datetime.fromisoformat(replay.LATE_TARGETS_REDUCED_E3_FROZEN_AFTER).timestamp()
        * 1000
    )
    cases = [
        {"start": cutoff - 1},
        {"start": cutoff + 1},
        {"start": cutoff + 2},
    ]

    selected = replay.prospective_shadow_cases(
        cases,
        {cutoff - 1, cutoff + 1},
    )

    assert selected == [{"start": cutoff + 1}]


def test_prospective_exit_shadow_uses_its_own_freeze_time() -> None:
    cutoff = int(
        datetime.fromisoformat(replay.EXIT_ONLY_FROZEN_AFTER).timestamp() * 1000
    )
    cases = [{"start": cutoff}, {"start": cutoff + 1}]

    selected = replay.prospective_shadow_cases(
        cases,
        {cutoff, cutoff + 1},
        frozen_after=replay.EXIT_ONLY_FROZEN_AFTER,
    )

    assert selected == [{"start": cutoff + 1}]


def test_prospective_stop_grid_shadow_uses_its_own_freeze_time() -> None:
    cutoff = int(
        datetime.fromisoformat(replay.STOP_GRID_FROZEN_AFTER).timestamp() * 1000
    )
    cases = [{"start": cutoff}, {"start": cutoff + 1}]

    selected = replay.prospective_shadow_cases(
        cases,
        {cutoff, cutoff + 1},
        frozen_after=replay.STOP_GRID_FROZEN_AFTER,
    )

    assert selected == [{"start": cutoff + 1}]


def test_prospective_stop_grid_shadow_freezes_two_x_risk_geometry(monkeypatch) -> None:
    captured = {}

    def capture_candidate(_cases, _fee_rate, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        replay,
        "report_prospective_candidate_shadow",
        capture_candidate,
    )

    replay.report_prospective_stop_distance_shadow([], 0.00055)

    assert captured["frozen_after"] == replay.STOP_GRID_FROZEN_AFTER
    assert captured["challenger"] == replay.current_policy()
    assert captured["risk_geometry"] == replay.RiskGeometry(2.0, 2.0)


def test_completed_position_starts_uses_reconciled_closed_size() -> None:
    bundle = io.BytesIO()
    lineage = {
        "intents": [
            {
                "intent_id": "closed",
                "entry_order_ids": ["entry-closed"],
                "metadata": {},
                "intent": {"symbol": "BTCUSDT", "side": "LONG", "stop_loss": "90"},
            },
            {
                "intent_id": "open",
                "entry_order_ids": ["entry-open"],
                "metadata": {},
                "intent": {"symbol": "ETHUSDT", "side": "LONG", "stop_loss": "90"},
            },
        ]
    }
    executions = [
        {
            "execType": "Trade",
            "orderId": "entry-closed",
            "execTime": "1000",
            "execQty": "2",
            "execPrice": "100",
            "closedSize": "0",
            "orderLinkId": "strategy-E1",
        },
        {
            "execType": "Trade",
            "orderId": "entry-open",
            "execTime": "3000",
            "execQty": "2",
            "execPrice": "100",
            "closedSize": "0",
            "orderLinkId": "strategy-E1",
        },
    ]
    closed_pnl = [
        {
            "symbol": "BTCUSDT",
            "side": "Sell",
            "updatedTime": "2000",
            "closedSize": "2",
            "closedPnl": "18",
        }
    ]

    with zipfile.ZipFile(bundle, "w") as archive:
        for name, rows in (
            ("analysis/trade_lineage.jsonl", [lineage]),
            ("bybit/executions.jsonl", executions),
            ("bybit/closed_pnl.jsonl", closed_pnl),
            ("bybit/transaction_log.jsonl", []),
        ):
            archive.writestr(
                f"forensic/{name}",
                "".join(json.dumps(row) + "\n" for row in rows),
            )

    bundle.seek(0)
    assert replay.completed_position_starts(bundle) == {1000}


def test_reconciled_position_filter_excludes_open_or_unmatched_cases() -> None:
    cases = [
        {"start": 1, "symbol": "BTCUSDT"},
        {"start": 2, "symbol": "ETHUSDT"},
        {"start": 3, "symbol": "SOLUSDT"},
    ]

    selected = replay.reconciled_position_cases(cases, {1, 3})

    assert selected == [cases[0], cases[2]]


def test_prospective_shadow_reports_collecting_without_new_closed_cases(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        replay, "load_cases", lambda _path: (0.0005, [], 0.0002, 0.00055)
    )
    monkeypatch.setattr(replay, "completed_position_starts", lambda _path: set())
    monkeypatch.setattr(
        sys, "argv", ["replay_strategy_v2.py", "bundle", "--prospective-shadow"]
    )

    output = io.StringIO()
    with redirect_stdout(output):
        replay.main()

    assert "prospective_shadow_cases=0" in output.getvalue()
    assert "prospective_shadow_status=collecting/target:20" in output.getvalue()


def test_prospective_exit_shadow_reports_collecting_without_new_closed_cases(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        replay, "load_cases", lambda _path: (0.0005, [], 0.0002, 0.00055)
    )
    monkeypatch.setattr(replay, "completed_position_starts", lambda _path: set())
    monkeypatch.setattr(
        sys,
        "argv",
        ["replay_strategy_v2.py", "bundle", "--prospective-exit-shadow"],
    )

    output = io.StringIO()
    with redirect_stdout(output):
        replay.main()

    assert f"prospective_shadow_frozen_after={replay.EXIT_ONLY_FROZEN_AFTER}" in (
        output.getvalue()
    )
    assert "prospective_shadow_cases=0" in output.getvalue()
    assert "prospective_shadow_status=collecting/target:20" in output.getvalue()


def test_paired_difference_summary_reports_deterministic_bootstrap() -> None:
    baseline = [
        replay.Result(start=1, net_r=-1.0, mfe_r=0.0, stopped=True),
        replay.Result(start=2, net_r=0.0, mfe_r=0.0, stopped=False),
    ]
    candidate = [
        replay.Result(start=1, net_r=-0.5, mfe_r=0.0, stopped=True),
        replay.Result(start=2, net_r=1.0, mfe_r=1.0, stopped=False),
    ]

    summary = replay.paired_difference_summary(
        baseline,
        candidate,
        bootstrap_iterations=500,
        seed=9,
    )

    assert summary["count"] == 2
    assert summary["improved_cases"] == 2
    assert summary["delta_net_r"] == 1.5
    interval = summary["iid_bootstrap_95ci_delta_net_r"]
    assert interval is not None and interval[0] <= 1.5 <= interval[1]
    block_interval = summary["circular_block_4_bootstrap_95ci_delta_net_r"]
    assert block_interval is not None
    assert block_interval[0] <= 1.5 <= block_interval[1]
    assert summary == replay.paired_difference_summary(
        baseline,
        candidate,
        bootstrap_iterations=500,
        seed=9,
    )


def test_paired_difference_summary_reports_top_three_gain_concentration() -> None:
    baseline = [
        replay.Result(start=index, net_r=0.0, mfe_r=0.0, stopped=False)
        for index in range(4)
    ]
    candidate = [
        replay.Result(start=0, net_r=2.0, mfe_r=2.0, stopped=False),
        replay.Result(start=1, net_r=1.0, mfe_r=1.0, stopped=False),
        replay.Result(start=2, net_r=0.5, mfe_r=0.5, stopped=False),
        replay.Result(start=3, net_r=-0.25, mfe_r=0.0, stopped=True),
    ]

    summary = replay.paired_difference_summary(
        baseline,
        candidate,
        bootstrap_iterations=100,
    )

    assert summary["top_3_positive_case_delta_r"] == 3.5
    assert summary["delta_net_r_excluding_top_3_positive_cases"] == -0.25


def test_concentration_adjusted_delta_removes_only_largest_positive_cases() -> None:
    top_three_delta, adjusted_delta = replay.concentration_adjusted_delta(
        [2.0, -0.5, 1.0, 0.5, -0.25]
    )

    assert top_three_delta == 3.5
    assert adjusted_delta == -0.75


def test_scale_in_freezes_at_candidate_tp1_or_trailing_activation() -> None:
    case = {
        "start": 1,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 60_000, "high": 106.0, "low": 99.0, "close": 105.0},
            {"time": 120_000, "high": 104.0, "low": 96.0, "close": 101.0},
        ],
        "events": [],
    }
    candidate_with_scale_in = replay.Candidate(
        weights=(0.80, 0.20, 0.00),
        depths=(0.33, 0.66),
        trail_at=1.20,
        trail_by=0.30,
        take_profit_rs=(1.0, 2.0, 3.0),
    )
    candidate_without_scale_in = replay.Candidate(
        weights=(1.00, 0.00, 0.00),
        depths=(0.33, 0.66),
        trail_at=1.20,
        trail_by=0.30,
        take_profit_rs=(1.0, 2.0, 3.0),
    )

    scaled = replay.replay(case, candidate_with_scale_in, 0.0, use_events=False)
    unscaled = replay.replay(case, candidate_without_scale_in, 0.0, use_events=False)

    assert scaled.net_r > unscaled.net_r


def test_e3_loss_cap_tightens_stop_only_after_tertiary_fill() -> None:
    candidate = replay.Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.33, 0.66),
        trail_at=0.50,
        trail_by=0.30,
    )
    long_case = {
        "start": 1,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 60_000, "high": 100.0, "low": 93.4, "close": 94.0},
            {"time": 120_000, "high": 93.0, "low": 89.0, "close": 90.0},
        ],
        "events": [],
    }
    short_case = {
        **long_case,
        "side": "SHORT",
        "stop": 110.0,
        "candles": [
            {"time": 60_000, "high": 106.6, "low": 100.0, "close": 106.0},
            {"time": 120_000, "high": 111.0, "low": 107.0, "close": 110.0},
        ],
    }

    for case in (long_case, short_case):
        uncapped = replay.replay(case, candidate, 0.0, use_events=False)
        capped = replay.replay(
            case,
            candidate,
            0.0,
            use_events=False,
            e3_loss_cap_r=0.50,
        )

        assert uncapped.net_r <= -0.99
        assert capped.net_r >= -0.51


def test_e3_risk_trim_caps_projected_stop_loss_after_same_bar_fill() -> None:
    candidate = replay.current_policy()
    long_case = {
        "start": 1,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 60_000, "high": 100.0, "low": 93.4, "close": 94.0},
            {"time": 120_000, "high": 93.0, "low": 89.0, "close": 90.0},
        ],
        "events": [],
    }
    short_case = {
        **long_case,
        "side": "SHORT",
        "stop": 110.0,
        "candles": [
            {"time": 60_000, "high": 106.6, "low": 100.0, "close": 106.0},
            {"time": 120_000, "high": 111.0, "low": 107.0, "close": 110.0},
        ],
    }

    for case in (long_case, short_case):
        baseline = replay.replay(case, candidate, 0.00055, use_events=False)
        trimmed = replay.replay(
            case,
            candidate,
            0.00055,
            use_events=False,
            e3_risk_trim_cap_r=0.50,
        )

        assert trimmed.entry_legs_filled == 3
        assert trimmed.net_r >= -0.51
        assert trimmed.net_r > baseline.net_r


def test_maker_taker_fee_schedule_prices_scale_ins_as_maker() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 60_000, "high": 100.0, "low": 92.0, "close": 94.0},
            {"time": 120_000, "high": 95.0, "low": 89.0, "close": 90.0},
        ],
        "events": [],
    }
    candidate = replay.current_policy()

    scalar = replay.replay(case, candidate, 0.00055, use_events=False)
    scheduled = replay.replay(
        case,
        candidate,
        0.00055,
        use_events=False,
        maker_fee_rate=0.00020,
        taker_fee_rate=0.00055,
    )

    assert scalar.entry_legs_filled == 3
    assert scheduled.net_r > scalar.net_r


def test_e3_minimum_delay_avoids_early_scale_in_before_stop() -> None:
    candidate = replay.Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.33, 0.66),
        trail_at=0.50,
        trail_by=0.30,
    )
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 60_000, "high": 100.0, "low": 93.0, "close": 94.0},
            {"time": 120_000, "high": 95.0, "low": 89.0, "close": 90.0},
        ],
        "events": [],
    }

    immediate = replay.replay(case, candidate, 0.0, use_events=False)
    delayed = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        e3_min_delay_minutes=3,
    )

    assert immediate.net_r <= -0.99
    assert delayed.net_r > immediate.net_r


def test_close_confirmed_time_stop_exits_stale_loser() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 0, "high": 100.0, "low": 98.0, "close": 99.0},
            {"time": 60_000, "high": 101.0, "low": 95.0, "close": 96.0},
            {"time": 120_000, "high": 103.0, "low": 96.0, "close": 102.0},
        ],
        "events": [],
    }
    candidate = replay.Candidate(
        weights=(1.0, 0.0, 0.0),
        depths=(0.33, 0.66),
        trail_at=2.0,
        trail_by=0.30,
        take_profit_rs=(3.0, 4.0, 5.0),
    )

    held = replay.replay(case, candidate, 0.0, use_events=False)
    time_stopped = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        time_stop=replay.TimeStop(after_minutes=2, max_close_r=-0.30),
    )

    assert held.net_r > 0
    assert time_stopped.net_r == -0.4


def test_time_stop_does_not_exit_when_close_recovers_above_loss_threshold() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 0, "high": 100.0, "low": 98.0, "close": 99.0},
            {"time": 60_000, "high": 101.0, "low": 97.0, "close": 98.0},
            {"time": 120_000, "high": 103.0, "low": 96.0, "close": 102.0},
        ],
        "events": [],
    }
    candidate = replay.Candidate(
        weights=(1.0, 0.0, 0.0),
        depths=(0.33, 0.66),
        trail_at=2.0,
        trail_by=0.30,
        take_profit_rs=(3.0, 4.0, 5.0),
    )

    held = replay.replay(case, candidate, 0.0, use_events=False)
    time_stopped = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        time_stop=replay.TimeStop(after_minutes=2, max_close_r=-0.30),
    )

    assert time_stopped.net_r == held.net_r


def test_time_stop_rejects_nonpositive_age_and_nonfinite_threshold() -> None:
    for after_minutes, max_close_r in ((0, 0.0), (-1, 0.0), (1, float("nan"))):
        try:
            replay.TimeStop(after_minutes, max_close_r)
        except ValueError:
            continue
        raise AssertionError("expected invalid time stop to be rejected")


def test_wider_stop_can_survive_noise_while_preserving_planned_risk() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 0, "high": 100.0, "low": 93.0, "close": 94.0},
            {"time": 60_000, "high": 98.0, "low": 89.0, "close": 91.0},
            {"time": 120_000, "high": 102.0, "low": 90.0, "close": 101.0},
        ],
        "events": [],
    }
    candidate = replay.current_policy()

    original = replay.replay(case, candidate, 0.0, use_events=False)
    unchanged = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        risk_geometry=replay.RiskGeometry(),
    )
    widened = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        risk_geometry=replay.RiskGeometry(
            stop_distance_multiplier=2.0,
            entry_grid_multiplier=1.0,
        ),
    )
    widened_grid = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        risk_geometry=replay.RiskGeometry(2.0, 2.0),
    )
    stopped_after_all_entries = replay.replay(
        {**case, "candles": [{"time": 0, "high": 100.0, "low": 70.0, "close": 75.0}]},
        candidate,
        0.0,
        use_events=False,
        risk_geometry=replay.RiskGeometry(2.0, 2.0),
    )

    assert unchanged == original
    assert original.net_r < 0
    assert widened.net_r > original.net_r
    assert widened_grid.net_r > original.net_r
    assert abs(stopped_after_all_entries.net_r + 1.0) < 1e-12


def test_risk_geometry_multipliers_must_be_positive_and_finite() -> None:
    for stop_multiplier, grid_multiplier in (
        (0.0, 1.0),
        (1.0, -0.5),
        (float("inf"), 1.0),
    ):
        try:
            replay.RiskGeometry(stop_multiplier, grid_multiplier)
        except ValueError:
            continue
        raise AssertionError("expected invalid risk geometry to be rejected")


def test_entry_grid_cannot_cross_scaled_stop() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "candles": [],
        "events": [],
    }

    try:
        replay.replay(
            case,
            replay.current_policy(),
            0.0,
            use_events=False,
            risk_geometry=replay.RiskGeometry(0.75, 2.0),
        )
    except ValueError as exc:
        assert "crossed effective stop" in str(exc)
    else:
        raise AssertionError("expected entry grid crossing the stop to be rejected")


def test_replay_reports_simulated_scale_in_fill_count() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 0, "high": 100.0, "low": 95.0, "close": 96.0},
            {"time": 60_000, "high": 100.0, "low": 96.0, "close": 97.0},
        ],
        "events": [],
    }
    candidate = replay.current_policy()

    current = replay.replay(case, candidate, 0.0, use_events=False)
    wider_stop_only = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        risk_geometry=replay.RiskGeometry(2.0, 1.0),
    )
    wider_stop_and_grid = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        risk_geometry=replay.RiskGeometry(2.0, 2.0),
    )

    assert current.entry_legs_filled == 2
    assert wider_stop_only.entry_legs_filled == 2
    assert wider_stop_and_grid.entry_legs_filled == 1


def test_e3_reclaim_waits_for_a_later_close_through_e2() -> None:
    candidate = replay.Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.33, 0.66),
        trail_at=2.0,
        trail_by=0.30,
        take_profit_rs=(3.0, 4.0, 5.0),
    )
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 60_000, "high": 100.0, "low": 93.0, "close": 94.0},
            {"time": 120_000, "high": 95.0, "low": 89.0, "close": 90.0},
        ],
        "events": [],
    }

    immediate = replay.replay(case, candidate, 0.0, use_events=False)
    confirmation = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        e3_reclaim_e2=True,
    )

    assert immediate.net_r <= -0.99
    assert abs(confirmation.net_r + 0.85) < 1e-12


def test_e3_reclaim_enters_at_confirmation_close_after_touch_bar() -> None:
    candidate = replay.Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.33, 0.66),
        trail_at=2.0,
        trail_by=0.30,
        take_profit_rs=(3.0, 4.0, 5.0),
    )
    no_e3 = replay.Candidate(
        weights=(0.60, 0.25, 0.00),
        depths=(0.33, 0.66),
        trail_at=2.0,
        trail_by=0.30,
        take_profit_rs=(3.0, 4.0, 5.0),
    )
    case = {
        "start": 0,
        "side": "LONG",
        "entry": 100.0,
        "stop": 90.0,
        "trader_tp": None,
        "candles": [
            {"time": 60_000, "high": 100.0, "low": 93.0, "close": 94.0},
            {"time": 120_000, "high": 99.0, "low": 94.0, "close": 97.0},
            {"time": 180_000, "high": 99.0, "low": 95.0, "close": 96.5},
        ],
        "events": [],
    }

    confirmation = replay.replay(
        case,
        candidate,
        0.0,
        use_events=False,
        e3_reclaim_e2=True,
    )
    without_e3 = replay.replay(case, no_e3, 0.0, use_events=False)

    assert confirmation.net_r < without_e3.net_r


def test_e3_minimum_delay_rejects_negative_age() -> None:
    try:
        replay.replay(
            {
                "start": 0,
                "side": "LONG",
                "entry": 100.0,
                "stop": 90.0,
                "candles": [],
                "events": [],
            },
            replay.current_policy(),
            0.0,
            use_events=False,
            e3_min_delay_minutes=-1,
        )
    except ValueError as exc:
        assert "cannot be negative" in str(exc)
    else:
        raise AssertionError("expected negative E3 delay to be rejected")


def test_replay_ranks_on_early_training_not_later_outcomes(monkeypatch) -> None:
    later_loser = replay.Candidate((0.70, 0.20, 0.10), (0.33, 0.66), 0.5, 0.3)
    later_winner = replay.Candidate((0.65, 0.25, 0.10), (0.33, 0.66), 0.5, 0.3)
    cases = [
        {"start": index, "events": [], "side": "LONG" if index % 2 else "SHORT"}
        for index in range(10)
    ]

    def fake_replay(case, candidate, _fee_rate, *, use_events, e3_loss_cap_r=None):
        assert use_events
        assert e3_loss_cap_r is None or e3_loss_cap_r in {0.50, 0.75}
        if candidate == replay.current_policy():
            value = 0.1
        elif candidate == later_loser:
            value = 1.0 if case["start"] < 5 else -10.0
        else:
            value = 0.5 if case["start"] < 5 else 10.0
        return replay.Result(case["start"], value, 0.0, False)

    monkeypatch.setattr(
        replay,
        "load_cases",
        lambda _path: (0.0005, cases, 0.0002, 0.00055),
    )
    monkeypatch.setattr(replay, "candidates", lambda: [later_loser, later_winner])
    monkeypatch.setattr(
        replay,
        "no_e3_counterfactuals",
        lambda: [replay.Candidate((0.80, 0.20, 0.00), (0.33, 0.66), 0.5, 0.3)],
    )
    monkeypatch.setattr(replay, "replay", fake_replay)
    monkeypatch.setattr(
        sys,
        "argv",
        ["replay_strategy_v2.py", "bundle", "--top", "1"],
    )

    output = io.StringIO()
    with redirect_stdout(output):
        replay.main()

    report = output.getvalue()
    assert "chronological_split=train:5/validation:2/holdout:3" in report
    assert "current_policy=train:+0.500R" in report
    assert "weights=(0.7, 0.2, 0.1)" in report
    assert "holdout=-30.000R" in report
