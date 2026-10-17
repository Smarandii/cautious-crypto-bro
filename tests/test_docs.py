"""The landing page makes factual claims about this repository.

A stale claim on a public page is a small lie, and this project is built
on the idea that the code owns the facts. The checks below are cheap and
selection-independent: they read the repository's own configuration
rather than relying on session state.

The advertised test count is deliberately NOT checked here. Pinning it
exactly needs `pytest --collect-only` in a subprocess, measured at 11s,
and `session.testscollected` is only meaningful on a full-suite run, so
either option is too slow or too fragile for every invocation. The count
is maintained by hand.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "docs" / "index.html"
DASHBOARD_SCRIPT = REPO / "docs" / "script.js"
DASHBOARD_DATA = REPO / "docs" / "data.json"
DASHBOARD_DATA_JS = REPO / "docs" / "data.js"


def _landing_page() -> str:
    return INDEX.read_text(encoding="utf-8")


def _flat_page() -> str:
    """Whitespace-collapsed, so a phrase is not broken by HTML line wrapping."""
    return re.sub(r"\s+", " ", _landing_page()).lower()


def _pyproject() -> str:
    return (REPO / "pyproject.toml").read_text(encoding="utf-8")


def _dashboard_script() -> str:
    return DASHBOARD_SCRIPT.read_text(encoding="utf-8")


def test_landing_page_has_a_test_count_chip() -> None:
    """Not the value, just that the claim is present and well formed.

    Without this a future edit could silently drop the claim, and the
    reason it is not asserted exactly would be lost.
    """
    page = _landing_page()

    assert re.search(r'<span class="chip">[\d,]+ tests</span>', page), (
        "landing page no longer has a well-formed test-count chip"
    )


def test_landing_page_complexity_ceiling_matches_pyproject() -> None:
    assert "cyclomatic complexity ceiling of 10" in _landing_page()

    assert "max-complexity = 10" in _pyproject(), (
        "landing page advertises a complexity ceiling of 10; "
        "pyproject.toml no longer sets it"
    )


def test_landing_page_auto_mode_default_matches_env_example() -> None:
    """The page says AUTO_APPROVAL_MODE ships disabled. It must."""
    assert "AUTO_APPROVAL_MODE" in _landing_page()

    env_example = (REPO / ".env.example").read_text(encoding="utf-8")

    assert "AUTO_APPROVAL_MODE=disabled" in env_example, (
        "landing page says AUTO_APPROVAL_MODE ships as disabled; "
        ".env.example no longer says so"
    )


def test_landing_page_license_matches_pyproject() -> None:
    assert "GPL-3.0-only" in _landing_page()

    assert 'license = "GPL-3.0-only"' in _pyproject(), (
        "landing page claims GPL-3.0-only; pyproject.toml declares otherwise"
    )


def test_landing_page_codeql_query_suite_matches_workflow() -> None:
    assert "security-extended" in _landing_page()

    codeql = (REPO / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")

    assert "queries: security-extended" in codeql, (
        "landing page claims CodeQL security-extended; codeql.yml changed"
    )


@pytest.mark.parametrize(
    "needle",
    [
        "parked as <code>uncertain",
        "a restart never retries it",
        "quarantined rather than resent",
        "cannot share a net position",
        "bybit demo only",
        "no performance claims",
    ],
)
def test_landing_page_keeps_its_safety_claims(needle: str) -> None:
    """These are the promises the page makes. Do not quietly drop one."""
    assert needle in _flat_page(), f"landing page no longer states: {needle}"


def test_dashboard_separates_whole_positions_from_account_pnl_rows() -> None:
    script = _dashboard_script()

    assert "renderStrategyPnl(data.strategy_pnl);" in script
    assert "data.account_pnl_records" in script
    assert "account P&amp;L rows · not whole trades" in script
    assert "Not inferred from partial closed-PnL rows." in script
    assert "performance_bootstrap_95ci" in script
    assert "block-bootstrap 95% intervals" in script
    assert "intent_outcomes" in script
    assert "safety stops" in script
    assert "break_even_avg_win_usdt_at_observed_counts" in script
    assert "holding the other outcome constant" in script
    assert "sizing/fill mix therefore matters" in script
    assert "account_open_positions" in script
    assert "unrealized P&amp;L · not realized strategy P&amp;L" in script
    assert "mark-to-stop estimate" in script
    assert "portfolio_stop_risk" in script
    assert "combined_stop_risk_usdt" in script
    assert "open_entry_order_details" in script
    assert "cache synced ${accountSync}" in script
    assert "renderSideCohorts(data.strategy_pnl?.by_side)" in script
    assert "Break-even WR" in script
    assert "not a sizing signal" in script
    assert "signed_funding_usdt" in script
    assert "avg_win_to_loss_ratio_r" in script
    assert "break_even_win_rate_r_pct" in script
    assert "renderFillFollowthrough(data.fill_followthrough)" in script
    assert "not realized trade P&amp;L" in script


def test_expectancy_formula_uses_signed_average_loss() -> None:
    assert "(win rate × avg win r) + (loss rate × signed avg loss r)" in _flat_page()


def test_dashboard_data_matches_renderer_contract() -> None:
    data = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    script_data = DASHBOARD_DATA_JS.read_text(encoding="utf-8")
    prefix = "window.DASHBOARD_DATA = "

    assert script_data.startswith(prefix)
    assert script_data.rstrip().endswith(";")
    assert json.loads(script_data[len(prefix) :].rstrip()[:-1]) == data

    strategy_pnl = data.get("strategy_pnl")
    assert strategy_pnl is not None
    assert strategy_pnl["available"] is True
    assert strategy_pnl["avg_win_usdt"] is not None
    assert strategy_pnl["avg_loss_usdt"] is not None
    assert strategy_pnl["break_even_win_rate_pct"] is not None
    fill_followthrough = data["fill_followthrough"]
    assert fill_followthrough["available"] is True
    assert fill_followthrough["filled_case_count"] >= 1
    assert set(fill_followthrough["by_horizon_minutes"]) == {"60", "240"}
    assert set(fill_followthrough["by_horizon_minutes"]["60"]) == {"LONG", "SHORT"}
    open_risk = data["account_open_positions"]["portfolio_stop_risk"]
    assert open_risk["snapshot_available"] is True
    assert open_risk["risk_bounded"] is True
    assert open_risk["combined_stop_risk_usdt"] is not None
    assert isinstance(open_risk["cap_exceeded"], bool)
    experiment = data["risk"]["demo_long_experiment"]
    assert experiment["active"] is True
    assert experiment["multiplier"] == 0.10
    assert experiment["completed_position_count"] <= experiment["filled_position_count"]
    assert (
        experiment["completed_position_count"]
        == experiment["performance"]["position_count"]
    )
    exit_experiment = data["risk"]["demo_exit_experiment"]
    assert exit_experiment["profile"] in {
        "baseline",
        "payoff_challenger",
        "payoff_early_trail",
        "payoff_early_tight_trail",
        "payoff_early_tight_trail_long_015",
        "payoff_early_tight_trail_long_ab_015",
        "payoff_early_tight_trail_long_ab_020_control",
    }
    assert exit_experiment["review_target_completed_positions"] == 20
    assert "renderLongRiskExperiment(data.risk?.demo_long_experiment)" in (
        _dashboard_script()
    )
    assert "renderDemoExitExperiment(data.risk?.demo_exit_experiment)" in (
        _dashboard_script()
    )
    assert "Randomized LONG exits" in _dashboard_script()
