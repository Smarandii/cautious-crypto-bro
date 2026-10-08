"""Compare frozen entry/exit and LONG-risk candidates on reconciled trades only."""

from __future__ import annotations

import argparse
import json
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


def summary(results: list[replay.Result]) -> dict[str, float | int]:
    """Add payoff and break-even metrics to the shared replay summary."""
    metrics = replay.summarize_results(results)
    average_win = metrics["avg_win_r"]
    average_loss = abs(metrics["avg_loss_r"])
    metrics["avg_win_loss_ratio_r"] = (
        average_win / average_loss if average_loss else float("inf")
    )
    metrics["breakeven_win_rate"] = (
        average_loss / (average_win + average_loss)
        if average_win + average_loss
        else 0.0
    )
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

    for name, selected in partitions.items():
        current = [
            replay.replay(case, replay.current_policy(), fee_rate, use_events=True)
            for case in selected
        ]
        live_demo_exit = [
            replay.replay(
                case,
                replay.live_demo_payoff_exit_candidate(),
                fee_rate,
                use_events=True,
            )
            for case in selected
        ]
        live_demo_exit_reduced_e3 = [
            replay.replay(
                case,
                replay.live_demo_payoff_exit_reduced_e3_candidate(),
                fee_rate,
                use_events=True,
            )
            for case in selected
        ]
        reduced_e3_exit = [
            replay.replay(
                case,
                replay.late_target_reduced_e3_candidate(),
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
            ("live_demo_payoff_exit_exact", live_demo_exit),
            ("live_demo_exit_plus_reduced_e3", live_demo_exit_reduced_e3),
            ("reduced_e3_research_candidate", reduced_e3_exit),
        ):
            print(
                f"metrics={name}/{label}",
                json.dumps(summary(results), sort_keys=True),
            )
        for label, comparator, candidate_results in (
            ("live_demo_payoff_exit_vs_current", current, live_demo_exit),
            (
                "live_demo_exit_plus_reduced_e3_vs_current",
                current,
                live_demo_exit_reduced_e3,
            ),
            (
                "live_demo_exit_plus_reduced_e3_vs_live_demo_exit",
                live_demo_exit,
                live_demo_exit_reduced_e3,
            ),
            ("reduced_e3_candidate_vs_current", current, reduced_e3_exit),
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
