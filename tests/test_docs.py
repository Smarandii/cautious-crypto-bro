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

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "docs" / "index.html"


def _landing_page() -> str:
    return INDEX.read_text(encoding="utf-8")


def _flat_page() -> str:
    """Whitespace-collapsed, so a phrase is not broken by HTML line wrapping."""
    return re.sub(r"\s+", " ", _landing_page()).lower()


def _pyproject() -> str:
    return (REPO / "pyproject.toml").read_text(encoding="utf-8")


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
