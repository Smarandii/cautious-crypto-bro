"""Compare frozen exit and entry-allocation candidates on reconciled trades."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import replay_strategy_v2 as replay


def chronological_splits(cases: list[dict]) -> dict[str, list[dict]]:
    """Return the established 50/20/30 chronological train/validation/holdout."""
    ordered = sorted(cases, key=lambda case: case["start"])
    validation_start = int(len(ordered) * 0.50)
    holdout_start = int(len(ordered) * 0.70)
    return {
        "train": ordered[:validation_start],
        "validation": ordered[validation_start:holdout_start],
        "holdout": ordered[holdout_start:],
    }


def summary(results: list[replay.Result]) -> dict[str, float | int | None]:
    """Add payoff and break-even metrics to the shared replay summary."""
    metrics = replay.summarize_results(results)
    average_win = metrics["avg_win_r"]
    average_loss = (
        abs(metrics["avg_loss_r"]) if metrics["avg_loss_r"] is not None else None
    )

    if average_win is None and average_loss is None:
        metrics["avg_win_loss_ratio_r"] = None
        metrics["breakeven_win_rate"] = None
    elif average_loss is None:
        metrics["avg_win_loss_ratio_r"] = float("inf")
        metrics["breakeven_win_rate"] = 0.0
    elif average_win is None:
        metrics["avg_win_loss_ratio_r"] = 0.0
        metrics["breakeven_win_rate"] = 1.0
    else:
        metrics["avg_win_loss_ratio_r"] = average_win / average_loss
        metrics["breakeven_win_rate"] = average_loss / (average_win + average_loss)
    return metrics


def compare(bundle: Path) -> None:
    """Print post-hoc paired results; does not change live execution policy."""
    fee_rate, cases, _, _ = replay.load_cases(bundle)
    cases = replay.reconciled_position_cases(
        cases,
        replay.completed_position_starts(bundle),
    )
    partitions = chronological_splits(cases)
    print(
        "note=retrospective research only; candidates were selected after viewing "
        "this sample; changing nominal LONG risk does not change normalized R; "
        "see analyze_forensic_pnl.py for dollar-level sizing sensitivity"
    )
    print("split_rule=chronological 50/20/30; inputs are fully reconciled positions")
    print(f"complete_cases={len(cases)}")

    payoff_challenger = replay.live_demo_payoff_exit_candidate()
    live_demo_early_trail = replay.live_demo_early_trail_candidate()
    live_demo_early_trail_reduced_e3 = replace(
        live_demo_early_trail,
        weights=(0.70, 0.25, 0.05),
    )
    reduced_e3_research = replay.late_target_reduced_e3_candidate()

    for name, selected in partitions.items():
        current = [
            replay.replay(case, replay.current_policy(), fee_rate, use_events=True)
            for case in selected
        ]
        payoff_challenger_results = [
            replay.replay(
                case,
                payoff_challenger,
                fee_rate,
                use_events=True,
            )
            for case in selected
        ]
        live_demo_early_trail_results = [
            replay.replay(
                case,
                live_demo_early_trail,
                fee_rate,
                use_events=True,
            )
            for case in selected
        ]
        live_demo_early_trail_reduced_e3_results = [
            replay.replay(
                case,
                live_demo_early_trail_reduced_e3,
                fee_rate,
                use_events=True,
            )
            for case in selected
        ]
        reduced_e3_research_results = [
            replay.replay(
                case,
                reduced_e3_research,
                fee_rate,
                use_events=True,
            )
            for case in selected
        ]
        print(
            f"split={name}/n={len(selected)}/longs="
            f"{sum(c['side'] == 'LONG' for c in selected)}"
        )
        for label, results in (
            ("current", current),
            ("payoff_challenger_040", payoff_challenger_results),
            ("live_demo_early_trail_exact", live_demo_early_trail_results),
            (
                "live_demo_early_trail_reduced_e3",
                live_demo_early_trail_reduced_e3_results,
            ),
            ("reduced_e3_research_candidate", reduced_e3_research_results),
        ):
            print(
                f"metrics={name}/{label}",
                json.dumps(summary(results), sort_keys=True),
            )
        for label, comparator, candidate_results in (
            (
                "payoff_challenger_040_vs_current",
                current,
                payoff_challenger_results,
            ),
            (
                "live_demo_early_trail_vs_current",
                current,
                live_demo_early_trail_results,
            ),
            (
                "payoff_challenger_040_vs_live_demo_early_trail_exact",
                live_demo_early_trail_results,
                payoff_challenger_results,
            ),
            (
                "live_demo_early_trail_reduced_e3_vs_current",
                current,
                live_demo_early_trail_reduced_e3_results,
            ),
            (
                "live_demo_early_trail_reduced_e3_vs_live_demo_early_trail",
                live_demo_early_trail_results,
                live_demo_early_trail_reduced_e3_results,
            ),
            (
                "reduced_e3_candidate_vs_current",
                current,
                reduced_e3_research_results,
            ),
        ):
            print(
                f"paired={name}/{label}",
                json.dumps(
                    replay.paired_difference_summary(comparator, candidate_results),
                    sort_keys=True,
                ),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    compare(args.bundle)


if __name__ == "__main__":
    main()
