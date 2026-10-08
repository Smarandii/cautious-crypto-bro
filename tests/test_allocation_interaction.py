"""Test the research-only LONG allocation interaction helper."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
spec = importlib.util.spec_from_file_location(
    "allocation_interaction",
    Path(__file__).parents[1] / "scripts" / "compare_allocation_interaction.py",
)
assert spec is not None and spec.loader is not None
interaction = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = interaction
spec.loader.exec_module(interaction)


def test_chronological_splits_are_50_20_30() -> None:
    cases = [{"start": start} for start in range(10)]

    splits = interaction.chronological_splits(cases)

    assert [len(splits[name]) for name in ("train", "validation", "holdout")] == [
        5,
        2,
        3,
    ]
    assert splits["train"][-1]["start"] < splits["validation"][0]["start"]
    assert splits["validation"][-1]["start"] < splits["holdout"][0]["start"]


def test_summary_derives_ratio_and_break_even_win_rate() -> None:
    results = [
        interaction.replay.Result(start=1, net_r=0.5, mfe_r=1, stopped=False),
        interaction.replay.Result(start=2, net_r=-1, mfe_r=0.2, stopped=True),
    ]

    summary = interaction.summary(results)

    assert summary["avg_win_loss_ratio_r"] == 0.5
    assert summary["breakeven_win_rate"] == pytest.approx(2 / 3)


def test_paired_comparison_uses_the_labeled_candidate_as_comparator(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases = [{"start": start, "side": "LONG"} for start in range(10)]
    candidates = {
        (0.60, 0.25, 0.15, 0.30): 0.0,
        (0.60, 0.25, 0.15, 0.10): 1.0,
        (0.70, 0.25, 0.05, 0.10): 2.0,
        (0.70, 0.25, 0.05, 0.30): 3.0,
    }

    monkeypatch.setattr(
        interaction.replay, "load_cases", lambda _bundle: (0.0, cases, [], [])
    )
    monkeypatch.setattr(
        interaction.replay,
        "completed_position_starts",
        lambda _bundle: set(range(10)),
    )

    def fake_replay(case, candidate, _fee_rate, *, use_events):
        assert use_events is True
        key = (*candidate.weights, candidate.trail_by)
        return interaction.replay.Result(
            start=case["start"],
            net_r=candidates[key],
            mfe_r=1.0,
            stopped=False,
        )

    monkeypatch.setattr(interaction.replay, "replay", fake_replay)
    interaction.compare(Path("unused.zip"))

    line = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(
            "paired=train/live_demo_exit_plus_reduced_e3_vs_live_demo_exit "
        )
    )
    result = json.loads(line.split(" ", 1)[1])

    assert result["delta_net_r"] == 5.0
