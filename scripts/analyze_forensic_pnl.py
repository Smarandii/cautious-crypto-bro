"""Reconcile Bybit closed PnL and funding to Strategy V2 positions."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Retain historical analyzer imports while consumers use the shared package.
from cautious_crypto_bro.forensic_pnl import (  # noqa: E402,F401
    BASELINE_RISK_PER_TRADE_PCT,
    DEMO_ENTRY_TTL_REVIEW_CASES,
    DEMO_EXIT_PROFILE_REVIEW_CASES,
    DEMO_LONG_EXIT_AB_CONTROL_PROFILE,
    DEMO_LONG_EXIT_AB_TREATMENT_PROFILE,
    DEMO_PAYOFF_EXIT_PROFILE,
    LIVE_LONG_RISK_EXIT_PROFILE,
    LIVE_LONG_RISK_MULTIPLIER,
    LIVE_LONG_RISK_STARTED_AT,
    LONG_FULL_RISK_STARTED_AT,
    LONG_RISK_CURVE_FROZEN_AFTER,
    LONG_RISK_CURVE_MULTIPLIERS,
    LONG_RISK_SHADOW_FROZEN_AFTER,
    LONG_RISK_SHADOW_MULTIPLIER,
    PROSPECTIVE_SHADOW_REVIEW_CASES,
    SOURCE_SIDE_HEALTH_SHADOW_FROZEN_AFTER,
    SOURCE_SIDE_HEALTH_WINDOW,
    _percentile_95_interval,
    _performance_metrics,
    bootstrap_performance_intervals,
    concentration_summary,
    cost_summary,
    exit_sizing_cohorts,
    jsonl,
    position_side,
    read_open_entry_orders_snapshot,
    read_open_positions,
    read_open_positions_snapshot,
    reconcile,
    summarize,
    wilson_interval,
)


def direction_risk_sensitivity(
    positions: list[dict],
    long_multipliers: tuple[float, ...] = (1.0, 0.5, 0.4, 0.25, 0.0),
    *,
    bootstrap_iterations: int = 5_000,
    seed: int = 20261007,
) -> list[dict[str, float | int | tuple[float, float] | None]]:
    """Estimate linear position-size counterfactuals; this is not a trade replay."""
    if bootstrap_iterations < 1:
        raise ValueError("Bootstrap iterations must be positive")
    scenarios = []
    for index, multiplier in enumerate(long_multipliers):
        if not 0 <= multiplier <= 1:
            raise ValueError("Long risk multiplier must be between 0 and 1")
        scaled_pnl = sum(
            position["net_pnl"] * (multiplier if position["side"] == "LONG" else 1.0)
            for position in positions
        )
        scaled_risk = sum(
            position["risk_usdt"] * (multiplier if position["side"] == "LONG" else 1.0)
            for position in positions
        )
        scenarios.append(
            {
                "long_risk_multiplier": multiplier,
                "observed_positions": len(positions),
                "net_pnl_usdt": scaled_pnl,
                "initial_risk_usdt": scaled_risk,
                "risk_weighted_return_pct": (
                    100 * scaled_pnl / scaled_risk if scaled_risk else None
                ),
                "iid_bootstrap_95ci_pct": _bootstrap_risk_return_interval(
                    positions,
                    multiplier,
                    iterations=bootstrap_iterations,
                    seed=seed + index,
                ),
            }
        )
    return scenarios


def prospective_long_risk_positions(
    positions: list[dict],
    *,
    frozen_after: str = LONG_RISK_SHADOW_FROZEN_AFTER,
) -> list[dict]:
    """Keep baseline-sized cases for the offline 25% LONG counterfactual."""
    return _baseline_sized_positions_after(positions, frozen_after=frozen_after)


def prospective_long_risk_curve_positions(
    positions: list[dict],
    *,
    frozen_after: str = LONG_RISK_CURVE_FROZEN_AFTER,
) -> list[dict]:
    """Keep baseline-sized cases for the offline sizing curve."""
    return _baseline_sized_positions_after(positions, frozen_after=frozen_after)


def prospective_live_long_risk_positions(
    positions: list[dict],
    *,
    started_at: str = LIVE_LONG_RISK_STARTED_AT,
    multiplier: float = LIVE_LONG_RISK_MULTIPLIER,
    baseline_risk_pct: float = BASELINE_RISK_PER_TRADE_PCT,
) -> list[dict]:
    """Select completed LONGs actually planned at the live experimental risk."""
    expected_risk_pct = baseline_risk_pct * multiplier
    return [
        position
        for position in _complete_reconciled_positions_after(
            positions,
            frozen_after=started_at,
        )
        if position.get("side") == "LONG"
        and _risk_pct_matches(position.get("risk_per_trade_pct"), expected_risk_pct)
    ]


def _baseline_sized_positions_after(
    positions: list[dict],
    *,
    frozen_after: str,
) -> list[dict]:
    """Exclude live-scaled plans from paper rescaling to prevent double scaling."""
    return [
        position
        for position in _complete_reconciled_positions_after(
            positions,
            frozen_after=frozen_after,
        )
        if _risk_pct_matches(
            position.get("risk_per_trade_pct"),
            BASELINE_RISK_PER_TRADE_PCT,
        )
    ]


def _risk_pct_matches(value: object, expected: float) -> bool:
    try:
        return abs(float(value) - expected) <= 1e-9
    except (TypeError, ValueError):
        return False


def _complete_reconciled_positions_after(
    positions: list[dict],
    *,
    frozen_after: str,
) -> list[dict]:
    """Filter to size-reconciled positions opened strictly after a freeze."""
    cutoff = datetime.fromisoformat(frozen_after).astimezone(UTC)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    return [
        position
        for position in positions
        if position.get("complete") and position["start"] > cutoff_ms
    ]


def report_prospective_live_long_risk(positions: list[dict]) -> None:
    """Report reduced-risk LONGs within their frozen exit-profile cohort."""
    live_longs = [
        position
        for position in prospective_live_long_risk_positions(positions)
        if position.get("exit_profile") == LIVE_LONG_RISK_EXIT_PROFILE
    ]
    cutoff = datetime.fromisoformat(LIVE_LONG_RISK_STARTED_AT).astimezone(UTC)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    short_controls = [
        position
        for position in positions
        if position.get("complete")
        and position.get("side") == "SHORT"
        and position.get("start", 0) > cutoff_ms
        and position.get("exit_profile") == LIVE_LONG_RISK_EXIT_PROFILE
        and _risk_pct_matches(
            position.get("risk_per_trade_pct"),
            BASELINE_RISK_PER_TRADE_PCT,
        )
    ]
    print(
        "prospective_live_long_risk_note=actual Demo LONG plans at reduced risk; "
        "compare per-R expectancy and payoff with contemporaneous baseline-sized "
        "SHORT controls; not a randomized comparison"
    )
    print(f"prospective_live_long_risk_started_at={LIVE_LONG_RISK_STARTED_AT}")
    print(f"prospective_live_long_risk_multiplier={LIVE_LONG_RISK_MULTIPLIER:.2f}")
    print(f"prospective_live_long_risk_exit_profile={LIVE_LONG_RISK_EXIT_PROFILE}")
    print(f"prospective_live_long_risk_long_cases={len(live_longs)}")
    print(f"prospective_live_long_risk_short_controls={len(short_controls)}")
    status = (
        "ready_for_review"
        if len(live_longs) >= PROSPECTIVE_SHADOW_REVIEW_CASES
        else "collecting"
    )
    print(
        "prospective_live_long_risk_status="
        f"{status}/long_target:{PROSPECTIVE_SHADOW_REVIEW_CASES}"
    )
    print(
        "prospective_live_long_risk_metrics=LONG",
        json.dumps(summarize(live_longs), sort_keys=True)
        if live_longs
        else "unavailable/no_cases",
    )
    print(
        "prospective_live_long_risk_metrics=SHORT_CONTROL",
        json.dumps(summarize(short_controls), sort_keys=True)
        if short_controls
        else "unavailable/no_cases",
    )
    print(
        "prospective_live_long_risk_costs=LONG",
        json.dumps(cost_summary(live_longs), sort_keys=True),
    )
    print(
        "prospective_live_long_risk_costs=SHORT_CONTROL",
        json.dumps(cost_summary(short_controls), sort_keys=True),
    )
    print(
        "prospective_live_long_risk_bootstrap_95ci=LONG",
        json.dumps(bootstrap_performance_intervals(live_longs), sort_keys=True),
    )
    print(
        "prospective_live_long_risk_bootstrap_95ci=SHORT_CONTROL",
        json.dumps(bootstrap_performance_intervals(short_controls), sort_keys=True),
    )


def report_prospective_demo_exit_profile(
    positions: list[dict],
    *,
    profile: str = DEMO_PAYOFF_EXIT_PROFILE,
) -> None:
    """Report realized results for the tagged Demo payoff-exit cohort."""
    filled = [
        position for position in positions if position.get("exit_profile") == profile
    ]
    print(f"prospective_demo_exit_profile_sizing_cutover={LONG_FULL_RISK_STARTED_AT}")
    for cohort, rows in exit_sizing_cohorts(filled).items():
        print(f"prospective_demo_exit_profile_sizing_cohort={cohort}")
        _report_exit_cohort(rows, profile=profile, reviewable=cohort != "unknown")


def _report_exit_cohort(filled: list[dict], *, profile: str, reviewable: bool) -> None:
    complete = [position for position in filled if position.get("complete")]
    status = (
        "ready_for_review"
        if reviewable and len(complete) >= DEMO_EXIT_PROFILE_REVIEW_CASES
        else "collecting"
    )
    print(f"prospective_demo_exit_profile={profile}")
    print(f"prospective_demo_exit_profile_filled_positions={len(filled)}")
    print(f"prospective_demo_exit_profile_completed_positions={len(complete)}")
    print(
        "prospective_demo_exit_profile_status="
        f"{status}/target:{DEMO_EXIT_PROFILE_REVIEW_CASES}"
    )
    print(
        "prospective_demo_exit_profile_metrics=",
        json.dumps(summarize(complete), sort_keys=True)
        if complete
        else "unavailable/no_cases",
    )
    print(
        "prospective_demo_exit_profile_bootstrap_95ci=",
        json.dumps(bootstrap_performance_intervals(complete), sort_keys=True),
    )
    print(
        "prospective_demo_exit_profile_concentration=",
        json.dumps(concentration_summary(complete), sort_keys=True),
    )
    if not complete:
        return

    for side in ("LONG", "SHORT"):
        selected = [position for position in complete if position["side"] == side]
        if selected:
            _report_cohort_breakdown(
                "prospective_demo_exit_profile",
                f"side_{side}",
                selected,
            )

    source_ids = sorted(
        {
            str(position["source_channel_id"])
            for position in complete
            if position.get("source_channel_id") is not None
        }
    )
    for source_id in source_ids:
        for side in ("LONG", "SHORT"):
            selected = [
                position
                for position in complete
                if str(position.get("source_channel_id")) == source_id
                and position["side"] == side
            ]
            if selected:
                _report_cohort_breakdown(
                    "prospective_demo_exit_profile",
                    f"source_side_{source_id}_{side}",
                    selected,
                )


def report_prospective_demo_entry_order_ttl(
    positions: list[dict],
    *,
    ttl_minutes: int,
) -> None:
    """Report fully reconciled positions from one persisted entry-TTL cohort."""
    if ttl_minutes <= 0:
        raise ValueError("Prospective Demo entry-order TTL must be positive")
    filled = [
        position
        for position in positions
        if position.get("entry_order_ttl_minutes", 0) == ttl_minutes
    ]
    complete = [position for position in filled if position.get("complete")]
    status = (
        "ready_for_review"
        if len(complete) >= DEMO_ENTRY_TTL_REVIEW_CASES
        else "collecting"
    )
    print(f"prospective_demo_entry_order_ttl_minutes={ttl_minutes}")
    print(f"prospective_demo_entry_order_ttl_filled_positions={len(filled)}")
    print(f"prospective_demo_entry_order_ttl_completed_positions={len(complete)}")
    print(
        "prospective_demo_entry_order_ttl_status="
        f"{status}/target:{DEMO_ENTRY_TTL_REVIEW_CASES}"
    )
    print(
        "prospective_demo_entry_order_ttl_metrics=",
        json.dumps(summarize(complete), sort_keys=True)
        if complete
        else "unavailable/no_cases",
    )
    print(
        "prospective_demo_entry_order_ttl_bootstrap_95ci=",
        json.dumps(bootstrap_performance_intervals(complete), sort_keys=True),
    )
    if not complete:
        return

    for side in ("LONG", "SHORT"):
        selected = [position for position in complete if position["side"] == side]
        if selected:
            _report_cohort_breakdown(
                "prospective_demo_entry_order_ttl",
                f"side_{side}",
                selected,
            )


def _report_cohort_breakdown(
    namespace: str,
    label: str,
    positions: list[dict],
) -> None:
    prefix = f"{namespace}_{label}"
    print(f"{prefix}_metrics=", json.dumps(summarize(positions), sort_keys=True))
    print(f"{prefix}_costs=", json.dumps(cost_summary(positions), sort_keys=True))
    print(
        f"{prefix}_bootstrap_95ci=",
        json.dumps(bootstrap_performance_intervals(positions), sort_keys=True),
    )
    print(
        f"{prefix}_concentration=",
        json.dumps(concentration_summary(positions), sort_keys=True),
    )


def source_side_health_decisions(
    positions: list[dict],
    *,
    window: int = SOURCE_SIDE_HEALTH_WINDOW,
) -> tuple[list[dict], list[dict]]:
    """Paper-gate a source/side after its last N closed trades sum to <= 0R.

    Uses only complete positions whose actual close timestamp precedes the
    candidate entry. Suppressed signals remain in the paper history so later
    decisions can continue to be evaluated without executing them.
    """
    if window < 1:
        raise ValueError("Source-side health window must be positive")
    complete = sorted(
        (position for position in positions if position.get("complete")),
        key=lambda position: position["start"],
    )
    allowed = []
    suppressed = []
    for position in complete:
        source = position.get("source_channel_id")
        if source is None:
            allowed.append(position)
            continue
        history = sorted(
            (
                prior
                for prior in complete
                if prior is not position
                and prior.get("source_channel_id") == source
                and prior.get("side") == position.get("side")
                and prior.get("closed_at", 0) > 0
                and prior["closed_at"] < position["start"]
            ),
            key=lambda prior: prior["closed_at"],
        )
        recent = history[-window:]
        if len(recent) == window and sum(item["net_r"] for item in recent) <= 0:
            suppressed.append(position)
        else:
            allowed.append(position)
    return allowed, suppressed


def prospective_source_side_health_positions(
    positions: list[dict],
    *,
    frozen_after: str = SOURCE_SIDE_HEALTH_SHADOW_FROZEN_AFTER,
) -> tuple[list[dict], list[dict]]:
    """Return post-freeze paper-allowed and paper-suppressed complete positions."""
    cutoff = int(
        datetime.fromisoformat(frozen_after).astimezone(UTC).timestamp() * 1000
    )
    allowed, suppressed = source_side_health_decisions(positions)
    return (
        [position for position in allowed if position["start"] > cutoff],
        [position for position in suppressed if position["start"] > cutoff],
    )


def _bootstrap_delta_interval(
    positions: list[dict],
    suppressed: list[dict],
    *,
    iterations: int = 5_000,
    seed: int = 20261007,
) -> tuple[float, float] | None:
    if not positions:
        return None
    suppressed_ids = {id(position) for position in suppressed}
    deltas = [
        -position["net_pnl"] if id(position) in suppressed_ids else 0.0
        for position in positions
    ]
    rng = random.Random(seed)
    totals = sorted(sum(rng.choices(deltas, k=len(deltas))) for _ in range(iterations))
    last = len(totals) - 1
    return totals[int(0.025 * last)], totals[int(0.975 * last)]


def report_prospective_source_side_health_shadow(
    positions: list[dict],
) -> None:
    """Report an offline participation filter without changing live orders."""
    allowed, suppressed = prospective_source_side_health_positions(positions)
    eligible = allowed + suppressed
    print(
        "prospective_source_side_health_note=paper-only; suppress a source+side "
        f"after its last {SOURCE_SIDE_HEALTH_WINDOW} fully closed baseline signals "
        "sum to <=0R; unknown sources remain allowed; suppressed signals remain "
        "in paper history; actual orders and live policy are unchanged"
    )
    print(
        "prospective_source_side_health_frozen_after="
        f"{SOURCE_SIDE_HEALTH_SHADOW_FROZEN_AFTER}"
    )
    print(f"prospective_source_side_health_cases={len(eligible)}")
    status = (
        "ready_for_review"
        if len(eligible) >= PROSPECTIVE_SHADOW_REVIEW_CASES
        else "collecting"
    )
    print(
        "prospective_source_side_health_status="
        f"{status}/target:{PROSPECTIVE_SHADOW_REVIEW_CASES}"
    )
    if not eligible:
        return
    suppressed_ids = {id(position) for position in suppressed}
    kept = [position for position in eligible if id(position) not in suppressed_ids]
    print(
        "prospective_source_side_health_baseline=",
        json.dumps(summarize(eligible), sort_keys=True),
    )
    print(
        "prospective_source_side_health_filtered=",
        json.dumps(summarize(kept), sort_keys=True),
    )
    print(f"prospective_source_side_health_suppressed={len(suppressed)}")
    print(
        "prospective_source_side_health_suppressed_actual_net_usdt="
        f"{sum(position['net_pnl'] for position in suppressed):.6f}"
    )
    print(
        "prospective_source_side_health_paired_delta_iid_95ci_usdt=",
        json.dumps(_bootstrap_delta_interval(eligible, suppressed)),
    )


def scale_long_risk(positions: list[dict], multiplier: float) -> list[dict]:
    """Linearly scale realized PnL and risk for long positions only."""
    if not 0 <= multiplier <= 1:
        raise ValueError("Long risk multiplier must be between 0 and 1")
    scaled = []
    for position in positions:
        factor = multiplier if position["side"] == "LONG" else 1.0
        scaled.append(
            {
                **position,
                "net_pnl": position["net_pnl"] * factor,
                "risk_usdt": position["risk_usdt"] * factor,
                "planned_max_loss_usdt": (
                    position["planned_max_loss_usdt"] * factor
                    if position.get("planned_max_loss_usdt") is not None
                    else None
                ),
            }
        )
    return scaled


def apply_current_demo_long_sizing(
    positions: list[dict],
    multiplier: float = LIVE_LONG_RISK_MULTIPLIER,
) -> list[dict]:
    """Scale only baseline-risk LONGs; preserve cases already traded at reduced risk."""
    if not 0 <= multiplier <= 1:
        raise ValueError("Long risk multiplier must be between 0 and 1")
    scaled = []
    for position in positions:
        is_baseline_long = position.get("side") == "LONG" and _risk_pct_matches(
            position.get("risk_per_trade_pct"),
            BASELINE_RISK_PER_TRADE_PCT,
        )
        factor = multiplier if is_baseline_long else 1.0
        scaled.append(
            {
                **position,
                "net_pnl": position["net_pnl"] * factor,
                "risk_usdt": position["risk_usdt"] * factor,
                "planned_max_loss_usdt": (
                    position["planned_max_loss_usdt"] * factor
                    if position.get("planned_max_loss_usdt") is not None
                    else None
                ),
            }
        )
    return scaled


def report_retrospective_current_demo_sizing(positions: list[dict]) -> None:
    """Compare the current LONG sizing rule without double-scaling live cohorts."""
    complete = [position for position in positions if position.get("complete")]
    ordered = sorted(complete, key=lambda position: position["start"])
    scaled = apply_current_demo_long_sizing(ordered)
    deltas = [
        candidate["net_pnl"] - baseline["net_pnl"]
        for baseline, candidate in zip(ordered, scaled, strict=True)
    ]
    positive_deltas = sorted((delta for delta in deltas if delta > 0), reverse=True)
    rng = random.Random(20261008)
    iid_totals = []
    block_totals = []
    for _ in range(10_000):
        iid_totals.append(sum(rng.choices(deltas, k=len(deltas))))
        sample = []
        while len(sample) < len(deltas):
            start = rng.randrange(len(deltas))
            sample.extend(deltas[(start + offset) % len(deltas)] for offset in range(4))
        block_totals.append(sum(sample[: len(deltas)]))
    split = int(len(ordered) * 0.70)
    print(
        "retrospective_demo_sizing_note=linear counterfactual; only 1.0%-risk LONGs "
        f"scaled to {LIVE_LONG_RISK_MULTIPLIER:.2f}; actual 0.10%/0.25% LONGs and "
        "all SHORTs preserved; assumes PnL, fees and risk scale with size; ignores "
        "fill/minimum-size/slippage changes; not proof of profitability"
    )
    print(
        "retrospective_demo_sizing_counts="
        f"complete:{len(ordered)} baseline_longs_scaled:"
        f"{sum(position.get('side') == 'LONG' and _risk_pct_matches(position.get('risk_per_trade_pct'), BASELINE_RISK_PER_TRADE_PCT) for position in ordered)} "
        f"live_0.10_longs_preserved:{sum(position.get('side') == 'LONG' and _risk_pct_matches(position.get('risk_per_trade_pct'), BASELINE_RISK_PER_TRADE_PCT * LIVE_LONG_RISK_MULTIPLIER) for position in ordered)}"
    )
    print(
        "retrospective_demo_sizing_metrics=current",
        json.dumps(summarize(ordered), sort_keys=True),
    )
    print(
        "retrospective_demo_sizing_metrics=counterfactual",
        json.dumps(summarize(scaled), sort_keys=True),
    )
    print(
        "retrospective_demo_sizing_paired_delta=",
        json.dumps(
            {
                "delta_net_pnl_usdt": sum(deltas),
                "delta_ex_top_three_positive_usdt": sum(deltas)
                - sum(positive_deltas[:3]),
                "iid_bootstrap_95ci_usdt": _percentile_95_interval(iid_totals),
                "circular_block_4_bootstrap_95ci_usdt": _percentile_95_interval(
                    block_totals
                ),
                "early70_delta_usdt": sum(deltas[:split]),
                "latest30_delta_usdt": sum(deltas[split:]),
            },
            sort_keys=True,
        ),
    )


def portfolio_risk_cap_sensitivity(
    positions: list[dict],
    cap_multiples: tuple[float, ...] = (3.0, 5.0),
    *,
    bootstrap_iterations: int = 10_000,
    seed: int = 20261008,
) -> list[dict]:
    """Replay linear sizing caps on overlapping initial stop risk, for research."""
    if bootstrap_iterations < 1:
        raise ValueError("Bootstrap iterations must be positive")
    if any(cap <= 0 for cap in cap_multiples):
        raise ValueError("Portfolio cap multiples must be positive")

    ordered = sorted(
        (position for position in positions if position.get("complete") is True),
        key=lambda position: position["start"],
    )
    planned_risks = [
        position["planned_max_loss_usdt"]
        for position in ordered
        if position.get("planned_max_loss_usdt") is not None
        and position["planned_max_loss_usdt"] > 0
    ]
    if not ordered or not planned_risks:
        return []

    unit_risk = statistics.mean(planned_risks)
    split_train_end = int(len(ordered) * 0.5)
    split_validation_end = int(len(ordered) * 0.7)
    reports = []
    for cap_index, cap_multiple in enumerate(cap_multiples):
        cap_usdt = unit_risk * cap_multiple
        active_risk: list[tuple[int, float]] = []
        scaled = []
        for position in ordered:
            active_risk = [
                (closed_at, risk)
                for closed_at, risk in active_risk
                if closed_at > position["start"]
            ]
            open_risk = sum(risk for _, risk in active_risk)
            risk = position["risk_usdt"]
            available = max(0.0, cap_usdt - open_risk)
            factor = min(1.0, available / risk) if risk > 0 else 0.0
            scaled.append(
                {
                    "net_pnl": position["net_pnl"] * factor,
                    "delta": position["net_pnl"] * (factor - 1.0),
                    "factor": factor,
                    "risk": risk * factor,
                }
            )
            active_risk.append((position["closed_at"], risk * factor))

        deltas = [position["delta"] for position in scaled]
        positive_deltas = sorted((delta for delta in deltas if delta > 0), reverse=True)
        rng = random.Random(seed + cap_index)
        iid_means = []
        block_means = []
        outcomes = [position["net_pnl"] for position in scaled]
        for _ in range(bootstrap_iterations):
            iid_means.append(statistics.mean(rng.choices(outcomes, k=len(outcomes))))
            block = []
            while len(block) < len(outcomes):
                start = rng.randrange(len(outcomes))
                block.extend(
                    outcomes[(start + offset) % len(outcomes)] for offset in range(4)
                )
            block_means.append(statistics.mean(block[: len(outcomes)]))

        def period_summary(
            start: int,
            end: int,
            current_positions: list[dict],
            original_positions: list[dict],
        ) -> dict[str, float | int]:
            current = current_positions[start:end]
            original = original_positions[start:end]
            return {
                "positions": len(current),
                "net_pnl_usdt": sum(position["net_pnl"] for position in current),
                "delta_usdt": sum(position["delta"] for position in current),
                "scaled_positions": sum(
                    position["factor"] < 0.999999 for position in current
                ),
                "baseline_net_pnl_usdt": sum(
                    position["net_pnl"] for position in original
                ),
            }

        reports.append(
            {
                "cap_multiple_of_mean_planned_risk": cap_multiple,
                "cap_usdt": cap_usdt,
                "positions": len(ordered),
                "net_pnl_usdt": sum(position["net_pnl"] for position in scaled),
                "baseline_net_pnl_usdt": sum(
                    position["net_pnl"] for position in ordered
                ),
                "delta_usdt": sum(deltas),
                "scaled_positions": sum(
                    position["factor"] < 0.999999 for position in scaled
                ),
                "top_three_positive_delta_usdt": sum(positive_deltas[:3]),
                "delta_ex_top_three_usdt": sum(deltas) - sum(positive_deltas[:3]),
                "iid_mean_pnl_95ci_usdt": _percentile_95_interval(iid_means),
                "circular_block_4_mean_pnl_95ci_usdt": _percentile_95_interval(
                    block_means
                ),
                "splits": {
                    "train": period_summary(0, split_train_end, scaled, ordered),
                    "validation": period_summary(
                        split_train_end, split_validation_end, scaled, ordered
                    ),
                    "holdout": period_summary(
                        split_validation_end, len(ordered), scaled, ordered
                    ),
                },
            }
        )
    return reports


def apply_portfolio_stop_risk_cap(
    positions: list[dict],
    cap_usdt: float,
    *,
    risk_basis: str = "actual",
) -> list[dict]:
    """Scale complete positions to a fixed concurrent initial-stop-risk cap."""
    if cap_usdt <= 0:
        raise ValueError("Portfolio stop-risk cap must be positive")
    if risk_basis not in {"actual", "planned"}:
        raise ValueError("Risk basis must be 'actual' or 'planned'")

    ordered = sorted(
        (position for position in positions if position.get("complete") is True),
        key=lambda position: position["start"],
    )
    active_risk: list[tuple[int, float]] = []
    capped = []
    for position in ordered:
        active_risk = [
            (closed_at, risk)
            for closed_at, risk in active_risk
            if closed_at > position["start"]
        ]
        actual_risk = position["risk_usdt"]
        risk = (
            actual_risk
            if risk_basis == "actual"
            else position.get("planned_max_loss_usdt")
        )
        if risk is None or risk < 0:
            raise ValueError(f"Missing or invalid {risk_basis} risk for position")
        available = max(0.0, cap_usdt - sum(amount for _, amount in active_risk))
        factor = min(1.0, available / risk) if risk > 0 else 0.0
        capped.append(
            {
                **position,
                "net_pnl": position["net_pnl"] * factor,
                "risk_usdt": actual_risk * factor,
                "planned_max_loss_usdt": (
                    position["planned_max_loss_usdt"] * factor
                    if position.get("planned_max_loss_usdt") is not None
                    else None
                ),
                "net_r": position["net_r"] if factor > 0 else 0.0,
                "portfolio_cap_factor": factor,
                "portfolio_cap_risk_basis": risk_basis,
            }
        )
        active_risk.append((position["closed_at"], risk * factor))
    return capped


def report_retrospective_live_sizing_with_cap(
    positions: list[dict],
    cap_usdt: float,
    *,
    risk_basis: str = "actual",
    bootstrap_iterations: int = 10_000,
    seed: int = 20261008,
) -> None:
    """Report the joint LONG-sizing and concurrent portfolio-cap counterfactual."""
    if bootstrap_iterations < 1:
        raise ValueError("Bootstrap iterations must be positive")
    complete = sorted(
        (position for position in positions if position.get("complete") is True),
        key=lambda position: position["start"],
    )
    current_sizing = apply_current_demo_long_sizing(complete)
    combined = apply_portfolio_stop_risk_cap(
        current_sizing,
        cap_usdt,
        risk_basis=risk_basis,
    )
    paired_deltas = [
        candidate["net_pnl"] - baseline["net_pnl"]
        for baseline, candidate in zip(complete, combined, strict=True)
    ]
    positive_deltas = sorted(
        (delta for delta in paired_deltas if delta > 0), reverse=True
    )
    rng = random.Random(seed)
    iid_means = []
    block_means = []
    for _ in range(bootstrap_iterations):
        iid_means.append(
            statistics.mean(rng.choices(paired_deltas, k=len(paired_deltas)))
        )
        block = []
        while len(block) < len(paired_deltas):
            start = rng.randrange(len(paired_deltas))
            block.extend(
                paired_deltas[(start + offset) % len(paired_deltas)]
                for offset in range(4)
            )
        block_means.append(statistics.mean(block[: len(paired_deltas)]))

    split_train_end = int(len(complete) * 0.5)
    split_validation_end = int(len(complete) * 0.7)
    print(
        "retrospective_live_sizing_cap_note=linear counterfactual; only 1.0%-risk "
        f"LONGs scaled to {LIVE_LONG_RISK_MULTIPLIER:.2f}; existing reduced-risk "
        "LONG cohorts and SHORTs preserved; fixed cap applied to overlapping "
        f"complete-position stop risk basis={risk_basis}; planned basis reserves "
        "full entry risk to closure; assumes PnL "
        "and fees scale linearly; ignores changed fills, minimums, slippage, and "
        "market impact; not proof of profitability"
    )
    print(f"retrospective_live_sizing_cap_usdt={cap_usdt:.2f}")
    print(
        "retrospective_live_sizing_cap_counts="
        f"complete:{len(complete)} "
        f"cap_partially_scaled:{sum(0 < item['portfolio_cap_factor'] < 0.999999 for item in combined)} "
        f"cap_skipped:{sum(item['portfolio_cap_factor'] == 0 for item in combined)}"
    )
    print(
        "retrospective_live_sizing_before_cap="
        + json.dumps(
            {
                "net_pnl_usdt": sum(item["net_pnl"] for item in current_sizing),
                "baseline_net_pnl_usdt": sum(item["net_pnl"] for item in complete),
            },
            sort_keys=True,
        )
    )
    print(
        "retrospective_live_sizing_metrics=actual",
        json.dumps(summarize(complete), sort_keys=True),
    )
    print(
        "retrospective_live_sizing_metrics=combined",
        json.dumps(summarize(combined), sort_keys=True),
    )
    print(
        "retrospective_live_sizing_bootstrap=combined",
        json.dumps(
            bootstrap_performance_intervals(
                combined,
                iterations=bootstrap_iterations,
                block_size=4,
                seed=seed,
            ),
            sort_keys=True,
        ),
    )
    print(
        "retrospective_live_sizing_with_cap="
        + json.dumps(
            {
                "net_pnl_usdt": sum(item["net_pnl"] for item in combined),
                "delta_vs_actual_usdt": sum(paired_deltas),
                "delta_ex_top_three_positive_usdt": sum(paired_deltas)
                - sum(positive_deltas[:3]),
                "iid_mean_delta_95ci_usdt": _percentile_95_interval(iid_means),
                "circular_block_4_mean_delta_95ci_usdt": _percentile_95_interval(
                    block_means
                ),
            },
            sort_keys=True,
        )
    )
    for name, start, end in (
        ("train", 0, split_train_end),
        ("validation", split_train_end, split_validation_end),
        ("holdout", split_validation_end, len(complete)),
    ):
        print(
            f"retrospective_live_sizing_cap_split={name}/"
            + json.dumps(
                {
                    "positions": end - start,
                    "baseline_net_pnl_usdt": sum(
                        item["net_pnl"] for item in complete[start:end]
                    ),
                    "baseline_net_r": sum(
                        item["net_r"] for item in complete[start:end]
                    ),
                    "net_pnl_usdt": sum(
                        item["net_pnl"] for item in combined[start:end]
                    ),
                    "net_r": sum(item["net_r"] for item in combined[start:end]),
                    "delta_usdt": sum(paired_deltas[start:end]),
                    "delta_net_r": sum(
                        combined[index]["net_r"] - complete[index]["net_r"]
                        for index in range(start, end)
                    ),
                    "delta_ex_top_three_positive_usdt": sum(paired_deltas[start:end])
                    - sum(
                        sorted(
                            (delta for delta in paired_deltas[start:end] if delta > 0),
                            reverse=True,
                        )[:3]
                    ),
                },
                sort_keys=True,
            )
        )


def report_prospective_long_risk_shadow(positions: list[dict]) -> None:
    """Report a shadow-only directional sizing counterfactual."""
    print(
        "prospective_long_risk_note=offline linear scaling only; LONG risk is "
        f"multiplied by {LONG_RISK_SHADOW_MULTIPLIER:.2f}, SHORT risk unchanged; "
        "actual orders and policy are unchanged; assumes PnL, fees, and risk scale "
        "with quantity and ignores fill, minimum-size, slippage, and market impact"
    )
    print(f"prospective_long_risk_frozen_after={LONG_RISK_SHADOW_FROZEN_AFTER}")
    print(f"prospective_long_risk_cases={len(positions)}")
    long_cases = [position for position in positions if position["side"] == "LONG"]
    print(f"prospective_long_risk_long_cases={len(long_cases)}")
    status = (
        "ready_for_review"
        if len(long_cases) >= PROSPECTIVE_SHADOW_REVIEW_CASES
        else "collecting"
    )
    print(
        "prospective_long_risk_status="
        f"{status}/long_target:{PROSPECTIVE_SHADOW_REVIEW_CASES}"
    )
    if not positions:
        return

    baseline = summarize(positions)
    candidate_positions = scale_long_risk(
        positions,
        LONG_RISK_SHADOW_MULTIPLIER,
    )
    candidate = summarize(candidate_positions)
    confidence = direction_risk_sensitivity(
        positions,
        long_multipliers=(LONG_RISK_SHADOW_MULTIPLIER,),
    )[0]
    print("prospective_long_risk_metrics=current", json.dumps(baseline, sort_keys=True))
    print(
        "prospective_long_risk_metrics=long_risk_scaled",
        json.dumps(candidate, sort_keys=True),
    )
    print(
        "prospective_long_risk_return_interval=long_risk_scaled",
        json.dumps(confidence, sort_keys=True),
    )
    print(
        "prospective_long_risk_paired_delta",
        json.dumps(
            _paired_long_risk_delta_summary(positions, LONG_RISK_SHADOW_MULTIPLIER),
            sort_keys=True,
        ),
    )


def report_prospective_long_risk_curve_shadow(positions: list[dict]) -> None:
    """Report a frozen paper-only sensitivity across LONG risk multipliers."""
    print(
        "prospective_long_risk_curve_note=paper-only linear scaling; SHORT risk "
        "is unchanged; actual orders and policy are unchanged; assumes PnL, fees, "
        "and risk scale with quantity and ignores changed fills, minimum sizes, "
        "slippage, and market impact"
    )
    print(f"prospective_long_risk_curve_frozen_after={LONG_RISK_CURVE_FROZEN_AFTER}")
    print(f"prospective_long_risk_curve_cases={len(positions)}")
    long_cases = [position for position in positions if position["side"] == "LONG"]
    print(f"prospective_long_risk_curve_long_cases={len(long_cases)}")
    status = (
        "ready_for_review"
        if len(long_cases) >= PROSPECTIVE_SHADOW_REVIEW_CASES
        else "collecting"
    )
    print(
        "prospective_long_risk_curve_status="
        f"{status}/long_target:{PROSPECTIVE_SHADOW_REVIEW_CASES}"
    )
    print(
        "prospective_long_risk_curve_multipliers="
        + ",".join(f"{value:.2f}" for value in LONG_RISK_CURVE_MULTIPLIERS)
    )
    if not positions:
        return

    for scenario in direction_risk_sensitivity(
        positions,
        long_multipliers=LONG_RISK_CURVE_MULTIPLIERS,
    ):
        multiplier = scenario["long_risk_multiplier"]
        print(
            "prospective_long_risk_curve_metrics=",
            json.dumps(scenario, sort_keys=True),
        )
        print(
            "prospective_long_risk_curve_paired_delta=",
            json.dumps(
                _paired_long_risk_delta_summary(positions, multiplier),
                sort_keys=True,
            ),
        )


def _paired_long_risk_delta_summary(
    positions: list[dict],
    multiplier: float,
    *,
    iterations: int = 10_000,
    block_size: int = 4,
    seed: int = 20261008,
) -> dict[str, float | int | tuple[float, float]]:
    """Bootstrap paired dollar change from scaling LONG PnL only."""
    if not 0 <= multiplier <= 1:
        raise ValueError("Long risk multiplier must be between 0 and 1")
    if iterations < 1 or block_size < 1:
        raise ValueError("Bootstrap iterations and block size must be positive")
    ordered = sorted(positions, key=lambda position: position["start"])
    deltas = [
        position["net_pnl"] * (multiplier - 1) if position["side"] == "LONG" else 0.0
        for position in ordered
    ]
    rng = random.Random(seed)
    iid_totals = []
    block_totals = []
    for _ in range(iterations):
        iid_totals.append(sum(rng.choices(deltas, k=len(deltas))))
        sampled = []
        while len(sampled) < len(deltas):
            start = rng.randrange(len(deltas))
            sampled.extend(
                deltas[(start + offset) % len(deltas)] for offset in range(block_size)
            )
        block_totals.append(sum(sampled[: len(deltas)]))
    positive_deltas = sorted(
        (delta for delta in deltas if delta > 0),
        reverse=True,
    )
    top_three = sum(positive_deltas[:3])
    total = sum(deltas)
    return {
        "positions": len(ordered),
        "long_positions": sum(position["side"] == "LONG" for position in ordered),
        "multiplier": multiplier,
        "delta_net_pnl_usdt": total,
        "delta_per_position_usdt": total / len(ordered) if ordered else 0.0,
        "positive_delta_positions": sum(delta > 0 for delta in deltas),
        "negative_delta_positions": sum(delta < 0 for delta in deltas),
        "top_three_positive_delta_usdt": top_three,
        "delta_ex_top_three_positive_usdt": total - top_three,
        "iid_bootstrap_95ci_usdt": _percentile_95_interval(iid_totals) or (0.0, 0.0),
        "circular_block_4_bootstrap_95ci_usdt": _percentile_95_interval(block_totals)
        or (0.0, 0.0),
    }


def _bootstrap_risk_return_interval(
    positions: list[dict],
    long_multiplier: float,
    *,
    iterations: int,
    seed: int,
) -> tuple[float, float] | None:
    if not positions:
        return None
    outcomes = [
        (
            position["net_pnl"]
            * (long_multiplier if position["side"] == "LONG" else 1.0),
            position["risk_usdt"]
            * (long_multiplier if position["side"] == "LONG" else 1.0),
        )
        for position in positions
    ]
    rng = random.Random(seed)
    returns = []
    for _ in range(iterations):
        sample = rng.choices(outcomes, k=len(outcomes))
        total_risk = sum(risk for _, risk in sample)
        if total_risk > 0:
            returns.append(100 * sum(pnl for pnl, _ in sample) / total_risk)
    if not returns:
        return None
    returns.sort()
    last = len(returns) - 1
    return returns[int(0.025 * last)], returns[int(0.975 * last)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--prospective-long-risk-shadow",
        action="store_true",
        help=(
            "Evaluate LONG risk at 25%% and SHORT risk unchanged only on fully "
            "reconciled positions opened after the fixed freeze time."
        ),
    )
    parser.add_argument(
        "--prospective-long-risk-curve-shadow",
        action="store_true",
        help=(
            "Compare 0%%, 25%%, 50%%, and 100%% LONG risk on fully reconciled "
            "positions opened after the sizing-curve freeze; no live changes."
        ),
    )
    parser.add_argument(
        "--prospective-live-long-risk",
        action="store_true",
        help=(
            "Report actual completed Demo LONGs at the live reduced-risk setting, "
            "with contemporaneous baseline-sized SHORT controls."
        ),
    )
    parser.add_argument(
        "--retrospective-current-demo-sizing",
        action="store_true",
        help=(
            "Compare the current 0.10x LONG sizing with completed history, scaling "
            "only baseline-sized LONGs and preserving already-scaled cohorts."
        ),
    )
    parser.add_argument(
        "--retrospective-live-sizing-cap",
        action="store_true",
        help=(
            "Apply live LONG sizing, then the fixed open stop-risk cap, to complete "
            "history as one retrospective linear counterfactual."
        ),
    )
    parser.add_argument(
        "--portfolio-stop-risk-cap-usdt",
        type=float,
        nargs="+",
        default=float(os.environ.get("DEMO_PORTFOLIO_STOP_RISK_CAP_USDT", "340")),
        help=(
            "One or more fixed concurrent stop-risk caps for the combined "
            "counterfactual."
        ),
    )
    parser.add_argument(
        "--portfolio-cap-risk-basis",
        choices=("actual", "planned"),
        default="actual",
        help=(
            "Use realized filled-position risk or frozen planned max loss as "
            "concurrent cap exposure."
        ),
    )
    parser.add_argument(
        "--prospective-demo-exit-profile",
        action="store_true",
        help=(
            "Report fully reconciled Demo positions whose persisted policy is "
            "tagged with the selected Demo payoff-exit profile."
        ),
    )
    parser.add_argument(
        "--demo-exit-profile",
        choices=(
            "payoff_early_trail",
            "payoff_early_tight_trail",
            "payoff_early_tight_trail_long_015",
            DEMO_LONG_EXIT_AB_TREATMENT_PROFILE,
            DEMO_LONG_EXIT_AB_CONTROL_PROFILE,
        ),
        default=DEMO_PAYOFF_EXIT_PROFILE,
        help="Profile tag selected by --prospective-demo-exit-profile.",
    )
    parser.add_argument(
        "--prospective-demo-entry-order-ttl-minutes",
        type=int,
        help=(
            "Report fully reconciled Demo positions whose persisted plan uses "
            "this entry-order TTL in minutes."
        ),
    )
    parser.add_argument(
        "--prospective-source-side-health-shadow",
        action="store_true",
        help=(
            "Paper-gate a source/direction after its previous three completed "
            "signals sum to nonpositive R, only for post-freeze positions."
        ),
    )
    args = parser.parse_args()

    positions = reconcile(args.bundle)
    complete = [position for position in positions if position["complete"]]
    if args.retrospective_current_demo_sizing:
        report_retrospective_current_demo_sizing(complete)
        return
    if args.retrospective_live_sizing_cap:
        for cap_usdt in (
            args.portfolio_stop_risk_cap_usdt
            if isinstance(args.portfolio_stop_risk_cap_usdt, list)
            else [args.portfolio_stop_risk_cap_usdt]
        ):
            report_retrospective_live_sizing_with_cap(
                complete,
                cap_usdt,
                risk_basis=args.portfolio_cap_risk_basis,
            )
        return
    if args.prospective_source_side_health_shadow:
        report_prospective_source_side_health_shadow(positions)
        return
    if args.prospective_long_risk_shadow:
        report_prospective_long_risk_shadow(
            prospective_long_risk_positions(positions),
        )
        return
    if args.prospective_long_risk_curve_shadow:
        report_prospective_long_risk_curve_shadow(
            prospective_long_risk_curve_positions(positions),
        )
        return
    if args.prospective_live_long_risk:
        report_prospective_live_long_risk(positions)
    if args.prospective_demo_exit_profile:
        report_prospective_demo_exit_profile(
            positions,
            profile=args.demo_exit_profile,
        )
    if args.prospective_demo_entry_order_ttl_minutes is not None:
        report_prospective_demo_entry_order_ttl(
            positions,
            ttl_minutes=args.prospective_demo_entry_order_ttl_minutes,
        )
    if (
        args.prospective_live_long_risk
        or args.prospective_demo_exit_profile
        or args.prospective_demo_entry_order_ttl_minutes is not None
    ):
        return

    print(f"positions={len(positions)} complete_size_reconciled={len(complete)}")
    print(f"unreconciled={len(positions) - len(complete)}")
    print("cost_decomposition=ALL", json.dumps(cost_summary(complete), sort_keys=True))
    for label, selected in (
        ("NET_WINNERS", [p for p in complete if p["net_pnl"] > 0]),
        ("NET_LOSERS", [p for p in complete if p["net_pnl"] < 0]),
    ):
        print(
            f"cost_decomposition={label}",
            json.dumps(cost_summary(selected), sort_keys=True),
        )
    chronological = sorted(complete, key=lambda position: position["start"])
    cutoff = (
        chronological[int(len(chronological) * 0.70)]["start"]
        if len(chronological) >= 2
        else None
    )
    print("channel_holdout_note=posthoc discovery; latest 30% is descriptive only")
    print(
        "direction_risk_sensitivity_note=retrospective linear size scaling only; "
        "assumes PnL and fees scale with quantity and ignores fill, slippage, "
        "minimum-size, and market-impact changes; IID bootstrap intervals assume "
        "independent trades and ignore time clustering; not a policy recommendation"
    )
    if cutoff is not None:
        periods = (
            (
                "early70",
                [position for position in chronological if position["start"] < cutoff],
            ),
            (
                "latest30",
                [position for position in chronological if position["start"] >= cutoff],
            ),
        )
        for period, selected in periods:
            print(
                f"chronological={period}",
                json.dumps(summarize(selected), sort_keys=True),
            )
        for period, selected in periods:
            for scenario in direction_risk_sensitivity(selected):
                print(
                    "direction_risk_sensitivity="
                    f"{period}/long_mult={scenario['long_risk_multiplier']:.2f}",
                    json.dumps(scenario, sort_keys=True),
                )
    for side in ("LONG", "SHORT"):
        cohort = [position for position in complete if position["side"] == side]
        print(side, json.dumps(summarize(cohort), sort_keys=True))
    for side in ("LONG", "SHORT"):
        for pattern in sorted(
            {
                position["fill_pattern"]
                for position in complete
                if position["side"] == side
            }
        ):
            cohort = [
                position
                for position in complete
                if position["side"] == side and position["fill_pattern"] == pattern
            ]
            print(
                f"side_fills={side}/{pattern}",
                json.dumps(summarize(cohort), sort_keys=True),
            )
    for field in ("source_channel_id", "entry_type", "risk_per_trade_pct"):
        values = sorted(
            {
                str(position[field])
                for position in complete
                if position.get(field) is not None
            }
        )
        for value in values:
            cohort = [
                position for position in complete if str(position.get(field)) == value
            ]
            print(
                f"{field}={value}",
                json.dumps(summarize(cohort), sort_keys=True),
            )
            if field == "source_channel_id":
                for side in ("LONG", "SHORT"):
                    selected = [
                        position for position in cohort if position["side"] == side
                    ]
                    if selected:
                        print(
                            f"channel_side={value}/{side}",
                            json.dumps(summarize(selected), sort_keys=True),
                        )
            if field == "source_channel_id" and cutoff is not None:
                for period, selected in (
                    (
                        "early70",
                        [position for position in cohort if position["start"] < cutoff],
                    ),
                    (
                        "latest30",
                        [
                            position
                            for position in cohort
                            if position["start"] >= cutoff
                        ],
                    ),
                ):
                    print(
                        f"channel_period={value}/{period}",
                        json.dumps(summarize(selected), sort_keys=True),
                    )
    print("ALL", json.dumps(summarize(complete), sort_keys=True))
    print(
        "performance_bootstrap_95ci",
        json.dumps(bootstrap_performance_intervals(complete), sort_keys=True),
    )
    print(
        "portfolio_cap_model_note=fixed cap multiple of mean planned stop risk; "
        "new overlapping risk scales to remaining capacity, released at close; "
        "PnL and fees scale linearly, fills and slippage do not change"
    )
    for result in portfolio_risk_cap_sensitivity(complete):
        print("portfolio_risk_cap_sensitivity", json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
