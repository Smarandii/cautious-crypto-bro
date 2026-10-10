"""Test post-entry directional follow-through diagnostics."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import analyze_signal_followthrough as analysis


def test_fixed_horizon_returns_are_risk_normalized_and_directional() -> None:
    cases = [
        {
            "start": 0,
            "side": "LONG",
            "first_fill_entry": 100.0,
            "entry": 80.0,
            "stop": 90.0,
            "candles": [{"time": 0, "close": 110.0}],
        },
        {
            "start": 0,
            "side": "SHORT",
            "first_fill_entry": 100.0,
            "entry": 120.0,
            "stop": 110.0,
            "candles": [{"time": 0, "close": 95.0}],
        },
    ]

    returns = analysis.fixed_horizon_returns(cases, horizon_minutes=1)

    assert returns == {"LONG": [1.0], "SHORT": [0.5]}


def test_fixed_horizon_returns_omit_cases_without_future_candles() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "first_fill_entry": 100.0,
        "stop": 90.0,
        "candles": [],
    }

    assert analysis.fixed_horizon_returns([case], horizon_minutes=1) == {
        "LONG": [],
        "SHORT": [],
    }


def test_delayed_entry_uses_later_close_and_new_stop_risk() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "stop": 90.0,
        "candles": [
            {"time": 0, "close": 100.0, "high": 101.0, "low": 99.0},
            {"time": 60_000, "close": 110.0, "high": 111.0, "low": 109.0},
        ],
    }

    returns = analysis.delayed_entry_returns(
        [case],
        delay_minutes=1,
        horizon_minutes=1,
    )

    assert returns == {"LONG": [1.0], "SHORT": []}


def test_delayed_entry_skips_a_case_if_stop_is_touched_while_waiting() -> None:
    case = {
        "start": 0,
        "side": "LONG",
        "stop": 90.0,
        "candles": [
            {"time": 0, "close": 95.0, "high": 101.0, "low": 89.0},
            {"time": 60_000, "close": 100.0, "high": 101.0, "low": 99.0},
        ],
    }

    returns = analysis.delayed_entry_returns(
        [case],
        delay_minutes=1,
        horizon_minutes=1,
    )

    assert returns == {"LONG": [], "SHORT": []}


def test_pre_signal_momentum_uses_only_closed_candles_and_is_directional() -> None:
    cases = [
        {
            "plan_created_at": "1970-01-01T00:03:00Z",
            "side": "LONG",
            "first_fill_entry": 100.0,
            "stop": 90.0,
            "candles": [
                {"time": 0, "close": 95.0},
                {"time": 60_000, "close": 100.0},
                {"time": 120_000, "close": 110.0},
                # This candle closes after the signal and must not be used.
                {"time": 180_000, "close": 130.0},
            ],
        },
        {
            "plan_created_at": "1970-01-01T00:03:00Z",
            "side": "SHORT",
            "first_fill_entry": 100.0,
            "stop": 110.0,
            "candles": [
                {"time": 0, "close": 105.0},
                {"time": 60_000, "close": 100.0},
                {"time": 120_000, "close": 90.0},
                {"time": 180_000, "close": 70.0},
            ],
        },
    ]

    returns = analysis.pre_signal_momentum_returns(cases, horizon_minutes=1)

    assert returns == {"LONG": [1.0], "SHORT": [1.0]}


def test_pre_signal_momentum_omits_cases_without_lookback() -> None:
    case = {
        "plan_created_at": "1970-01-01T00:02:00Z",
        "side": "LONG",
        "first_fill_entry": 100.0,
        "stop": 90.0,
        "candles": [{"time": 60_000, "close": 101.0}],
    }

    assert analysis.pre_signal_momentum_returns([case], horizon_minutes=1) == {
        "LONG": [],
        "SHORT": [],
    }


def test_pre_signal_momentum_can_use_raw_archive_lookback() -> None:
    case = {
        "plan_created_at": "1970-01-01T00:03:00Z",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "first_fill_entry": 100.0,
        "stop": 90.0,
        # Replay candles intentionally begin after the first fill.
        "candles": [{"time": 180_000, "close": 120.0}],
    }
    raw_candles = {
        "BTCUSDT": [
            {"time": 0, "close": 90.0},
            {"time": 60_000, "close": 95.0},
            {"time": 120_000, "close": 100.0},
            {"time": 180_000, "close": 120.0},
        ]
    }

    returns = analysis.pre_signal_momentum_returns(
        [case],
        horizon_minutes=1,
        market_candles_by_symbol=raw_candles,
    )

    assert returns == {"LONG": [0.5], "SHORT": []}


def test_realized_momentum_pairs_only_complete_positions_by_intent_id() -> None:
    cases = [
        {
            "intent_id": "complete",
            "plan_created_at": "1970-01-01T00:03:00Z",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "first_fill_entry": 100.0,
            "stop": 90.0,
            "candles": [],
        },
        {
            "intent_id": "incomplete",
            "plan_created_at": "1970-01-01T00:03:00Z",
            "symbol": "BTCUSDT",
            "side": "SHORT",
            "first_fill_entry": 100.0,
            "stop": 110.0,
            "candles": [],
        },
    ]
    positions = [
        {
            "intent_id": "complete",
            "complete": True,
            "start": 1,
            "side": "LONG",
            "net_r": -0.75,
        },
        {
            "intent_id": "incomplete",
            "complete": False,
            "start": 2,
            "side": "SHORT",
            "net_r": 2.0,
        },
    ]
    market_candles = {
        "BTCUSDT": [
            {"time": 0, "close": 90.0},
            {"time": 60_000, "close": 95.0},
            {"time": 120_000, "close": 100.0},
            # Signal is at 180 seconds; this candle closes afterward.
            {"time": 180_000, "close": 140.0},
        ]
    }

    paired = analysis.realized_momentum_filter_rows(
        cases,
        positions,
        market_candles,
        horizon_minutes=1,
    )

    assert paired == [
        {
            "intent_id": "complete",
            "start": 1,
            "side": "LONG",
            "momentum_r": 0.5,
            "net_r": -0.75,
            "plan_created_at": "1970-01-01T00:03:00Z",
            "exit_profile": None,
        }
    ]


def test_prospective_momentum_rows_only_include_new_complete_positions() -> None:
    cases = [
        {
            "intent_id": "old",
            "plan_created_at": "1970-01-01T00:02:00Z",
            "symbol": "OLDUSDT",
            "side": "LONG",
            "first_fill_entry": 100.0,
            "stop": 90.0,
            "exit_profile": "profile-a",
            "candles": [],
        },
        {
            "intent_id": "new",
            "plan_created_at": "1970-01-01T00:03:00Z",
            "symbol": "NEWUSDT",
            "side": "LONG",
            "first_fill_entry": 100.0,
            "stop": 90.0,
            "exit_profile": "profile-b",
            "candles": [],
        },
        {
            "intent_id": "open",
            "plan_created_at": "1970-01-01T00:04:00Z",
            "symbol": "OPENUSDT",
            "side": "LONG",
            "first_fill_entry": 100.0,
            "stop": 90.0,
            "exit_profile": "profile-b",
            "candles": [],
        },
    ]
    positions = [
        {
            "intent_id": "old",
            "complete": True,
            "start": 1,
            "side": "LONG",
            "net_r": -0.5,
        },
        {
            "intent_id": "new",
            "complete": True,
            "start": 2,
            "side": "LONG",
            "net_r": 0.4,
        },
        {
            "intent_id": "open",
            "complete": False,
            "start": 3,
            "side": "LONG",
            "net_r": 0.8,
        },
    ]
    market_candles = {
        "OLDUSDT": [
            {"time": 0, "close": 95.0},
            {"time": 60_000, "close": 100.0},
        ],
        "NEWUSDT": [
            {"time": 0, "close": 90.0},
            {"time": 60_000, "close": 95.0},
            {"time": 120_000, "close": 100.0},
        ],
        "OPENUSDT": [
            {"time": 0, "close": 90.0},
            {"time": 60_000, "close": 95.0},
            {"time": 120_000, "close": 100.0},
            {"time": 180_000, "close": 105.0},
        ],
    }

    paired = analysis.prospective_realized_momentum_filter_rows(
        cases,
        positions,
        market_candles,
        horizon_minutes=1,
        created_after=datetime(1970, 1, 1, 0, 2, 30, tzinfo=UTC),
    )

    assert [row["intent_id"] for row in paired] == ["new"]
    assert paired[0]["exit_profile"] == "profile-b"
    assert paired[0]["net_r"] == 0.4


def test_empty_horizon_summary_reports_unavailable_metrics() -> None:
    summary = analysis.summarize_returns([], seed=7, iterations=20)

    assert summary == {
        "count": 0,
        "positive_pct": None,
        "mean_r": None,
        "median_r": None,
        "iid95": None,
        "block4_95": None,
    }


def test_fill_followthrough_summary_reports_per_side_horizons(monkeypatch) -> None:
    cases = [{"side": "LONG"}, {"side": "SHORT"}]
    monkeypatch.setattr(
        analysis.replay,
        "load_cases",
        lambda _bundle: ([], cases, [], []),
    )

    def returns_for_horizon(_cases, *, horizon_minutes):
        if horizon_minutes == 60:
            return {"LONG": [-0.5, 0.25], "SHORT": [0.1]}
        return {"LONG": [-0.75], "SHORT": [0.2, -0.1]}

    monkeypatch.setattr(analysis, "fixed_horizon_returns", returns_for_horizon)

    summary = analysis.fill_followthrough_summary(Path("forensic.zip"))

    assert summary["available"] is True
    assert summary["filled_case_count"] == 2
    assert summary["by_horizon_minutes"]["60"]["LONG"]["count"] == 2
    assert summary["by_horizon_minutes"]["60"]["LONG"]["mean_r"] == -0.125
    assert summary["by_horizon_minutes"]["60"]["SHORT"]["count"] == 1
    assert summary["by_horizon_minutes"]["240"]["SHORT"]["count"] == 2
    assert "not realized trade PnL" in summary["note"]


def test_fill_followthrough_summary_is_unavailable_without_fills(monkeypatch) -> None:
    monkeypatch.setattr(analysis.replay, "load_cases", lambda _bundle: ([], [], [], []))

    summary = analysis.fill_followthrough_summary(Path("forensic.zip"))

    assert summary["available"] is False
    assert summary["filled_case_count"] == 0
    assert summary["by_horizon_minutes"] == {}


def test_prospective_long_cohort_excludes_old_risk_and_other_profiles() -> None:
    common = {
        "side": "LONG",
        "risk_per_trade_pct": 0.10,
        "exit_profile": "payoff_early_trail",
    }
    cases = [
        {
            **common,
            "plan_created_at": "2026-10-08T08:49:47+00:00",
            "symbol": "ENAUSDT",
        },
        {
            **common,
            "plan_created_at": "2026-10-08T08:49:46+00:00",
            "symbol": "OLD",
        },
        {
            **common,
            "plan_created_at": "2026-10-08T09:00:00+00:00",
            "risk_per_trade_pct": 0.25,
            "symbol": "PRIOR_RISK",
        },
        {
            **common,
            "plan_created_at": "2026-10-08T09:00:00+00:00",
            "exit_profile": "baseline",
            "symbol": "OTHER_PROFILE",
        },
        {
            **common,
            "plan_created_at": "2026-10-08T09:00:00+00:00",
            "side": "SHORT",
            "symbol": "SHORT",
        },
    ]

    assert [case["symbol"] for case in analysis.prospective_long_cohort(cases)] == [
        "ENAUSDT"
    ]


def test_bundle_analysis_does_not_require_a_position_to_be_closed(
    monkeypatch,
    capsys,
) -> None:
    cases = [
        {
            "start": 0,
            "plan_created_at": "1970-01-01T00:00:00Z",
            "side": "LONG",
            "first_fill_entry": 100.0,
            "stop": 90.0,
            "candles": [{"time": 0, "close": 101.0}],
        }
    ]
    monkeypatch.setattr(
        analysis.replay,
        "load_cases",
        lambda _bundle: (0.0, cases, 0, 0),
    )
    monkeypatch.setattr(analysis, "load_market_candles", lambda _bundle: {})
    monkeypatch.setattr(analysis.forensic_pnl, "reconcile", lambda _bundle: [])
    monkeypatch.setattr(analysis, "HORIZONS_MINUTES", (1,))

    def fail_if_completeness_is_checked(_bundle) -> None:
        raise AssertionError("directional movement must include open cases")

    monkeypatch.setattr(
        analysis.replay,
        "completed_position_starts",
        fail_if_completeness_is_checked,
    )

    analysis.analyze_bundle(Path("unused.zip"))

    output = capsys.readouterr().out
    assert "filled_cases=1" in output
    assert "horizon_min=1 side=LONG n=1" in output
