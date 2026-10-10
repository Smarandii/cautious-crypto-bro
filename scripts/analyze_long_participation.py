"""Analyze randomized Demo LONG take/skip assignments from a forensic archive."""

from __future__ import annotations

import argparse
import json
import random
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from analyze_forensic_pnl import reconcile, summarize
from analyze_signal_followthrough import load_market_candles

REVIEW_TARGET_PER_ARM = 20
BOOTSTRAP_SAMPLES = 10_000
MOMENTUM_LOOKBACK_MINUTES = 60


def _assignments(bundle: Path) -> list[dict]:
    with zipfile.ZipFile(bundle) as archive:
        name = next(
            (
                item
                for item in archive.namelist()
                if item.endswith("analysis/long_participation_assignments.jsonl")
            ),
            None,
        )
        if name is None:
            return []
        records = [
            json.loads(line)
            for line in archive.read(name).decode("utf-8").splitlines()
            if line
        ]
    return [
        assignment
        for record in records
        for assignment in record.get("assignments", [])
        if assignment.get("arm") in {"take", "skip"}
    ]


def _day_cluster_interval(
    values_by_day: dict[str, list[float]],
    *,
    seed: int,
) -> tuple[float, float] | None:
    if len(values_by_day) < 2:
        return None
    days = sorted(values_by_day)
    rng = random.Random(seed)
    estimates = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sampled_days = [rng.choice(days) for _ in days]
        sample = [value for day in sampled_days for value in values_by_day[day]]
        estimates.append(sum(sample) / len(sample))
    estimates.sort()
    lower = estimates[int(0.025 * BOOTSTRAP_SAMPLES)]
    upper = estimates[int(0.975 * BOOTSTRAP_SAMPLES)]
    return (lower, upper)


def _momentum_stratum(
    assignment: dict,
    market_candles_by_symbol: dict[str, list[dict]],
) -> str | None:
    """Classify an assignment by direction-aligned, closed-candle momentum."""
    side = assignment.get("side")
    symbol = assignment.get("symbol")
    created_at = assignment.get("created_at")
    if side not in {"LONG", "SHORT"} or not symbol or not created_at:
        return None

    try:
        signal_time = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=UTC)
        signal_time_ms = int(signal_time.timestamp() * 1000)
        candles = sorted(
            (
                candle
                for candle in market_candles_by_symbol.get(symbol.upper(), [])
                if int(candle["time"]) + 60_000 <= signal_time_ms
            ),
            key=lambda candle: int(candle["time"]),
        )
        if not candles:
            return None

        latest = candles[-1]
        latest_time = int(latest["time"])
        lookback_ms = MOMENTUM_LOOKBACK_MINUTES * 60_000
        prior = next(
            (
                candle
                for candle in reversed(candles[:-1])
                if int(candle["time"]) <= latest_time - lookback_ms
            ),
            None,
        )
        if prior is None:
            return None

        prior_close = float(prior["close"])
        latest_close = float(latest["close"])
        if prior_close <= 0:
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None

    direction = 1 if side == "LONG" else -1
    aligned_return = direction * (latest_close / prior_close - 1)
    return "keep_nonpositive" if aligned_return <= 0 else "skip_positive"


def _assignments_by_momentum_stratum(
    assignments: list[dict],
    market_candles_by_symbol: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], int]:
    by_stratum: dict[str, list[dict]] = {
        "keep_nonpositive": [],
        "skip_positive": [],
    }
    unclassified = 0
    for assignment in assignments:
        stratum = _momentum_stratum(assignment, market_candles_by_symbol)
        if stratum is None:
            unclassified += 1
        else:
            by_stratum[stratum].append(assignment)
    return by_stratum, unclassified


def _momentum_stratum_metrics(
    assignments: list[dict],
    positions: dict[str, dict],
    *,
    seed: int,
) -> dict[str, object]:
    takes = [row for row in assignments if row.get("arm") == "take"]
    skips = [row for row in assignments if row.get("arm") == "skip"]
    complete_takes = [
        positions[row["intent_id"]] for row in takes if row["intent_id"] in positions
    ]
    failed_takes = [
        row
        for row in takes
        if row.get("status") == "FAILED" and not row.get("bybit_order_ids")
    ]
    unresolved = len(takes) - len(complete_takes) - len(failed_takes)
    invalid_takes = [row for row in takes if row.get("approval_mode") != "AUTO"]
    invalid_skips = [
        row
        for row in skips
        if row.get("status") != "SKIPPED"
        or row.get("approval_mode") != "SKIPPED"
        or row.get("bybit_order_ids")
    ]
    take_values_by_day: dict[str, list[float]] = defaultdict(list)
    take_r_values_by_day: dict[str, list[float]] = defaultdict(list)
    for assignment in takes:
        position = positions.get(assignment["intent_id"])
        failed_before_fill = assignment.get(
            "status"
        ) == "FAILED" and not assignment.get("bybit_order_ids")
        if position is None and not failed_before_fill:
            continue
        created_at = datetime.fromisoformat(
            str(assignment["created_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        pnl = 0.0 if position is None else float(position["net_pnl"])
        net_r = 0.0 if position is None else float(position["net_r"])
        take_values_by_day[created_at.date().isoformat()].append(pnl)
        take_r_values_by_day[created_at.date().isoformat()].append(net_r)

    all_takes_resolved = unresolved == 0 and not invalid_takes and not invalid_skips
    take_mean = (
        sum(sum(values) for values in take_values_by_day.values()) / len(takes)
        if takes and all_takes_resolved
        else None
    )
    take_mean_r = (
        sum(sum(values) for values in take_r_values_by_day.values()) / len(takes)
        if takes and all_takes_resolved
        else None
    )
    return {
        "take_assigned": len(takes),
        "skip_assigned": len(skips),
        "take_completed": len(complete_takes),
        "take_failed_before_fill": len(failed_takes),
        "take_unresolved": unresolved,
        "invalid_take": len(invalid_takes),
        "invalid_skip": len(invalid_skips),
        "take_mean_net_usdt_per_assignment": take_mean,
        "take_day_cluster_95ci_usdt": (
            _day_cluster_interval(take_values_by_day, seed=seed)
            if takes and all_takes_resolved
            else None
        ),
        "take_mean_net_r_per_assignment": take_mean_r,
        "take_day_cluster_95ci_r": (
            _day_cluster_interval(take_r_values_by_day, seed=seed + 1)
            if takes and all_takes_resolved
            else None
        ),
    }


def _report_momentum_strata(
    assignments: list[dict],
    positions: dict[str, dict],
    market_candles_by_symbol: dict[str, list[dict]],
) -> None:
    by_stratum, unclassified = _assignments_by_momentum_stratum(
        assignments,
        market_candles_by_symbol,
    )
    print(
        "long_participation_momentum_note=60m prior direction-aligned return "
        "from fully closed 1m candles; take-arm outcomes are pooled across "
        "randomized exit profiles; strata are descriptive until adequately sampled"
    )
    print(f"long_participation_momentum_unclassified={unclassified}")

    for index, (stratum, rows) in enumerate(by_stratum.items()):
        metrics = _momentum_stratum_metrics(rows, positions, seed=88231 + index)
        take_mean = metrics["take_mean_net_usdt_per_assignment"]
        interval = metrics["take_day_cluster_95ci_usdt"]
        take_mean_r = metrics["take_mean_net_r_per_assignment"]
        interval_r = metrics["take_day_cluster_95ci_r"]
        interval_text = "n/a" if interval is None else json.dumps(interval)
        mean_text = "n/a" if take_mean is None else str(take_mean)
        interval_r_text = "n/a" if interval_r is None else json.dumps(interval_r)
        mean_r_text = "n/a" if take_mean_r is None else str(take_mean_r)
        print(
            f"long_participation_momentum_stratum={stratum} "
            f"take_assigned={metrics['take_assigned']} "
            f"skip_assigned={metrics['skip_assigned']} "
            f"take_completed={metrics['take_completed']} "
            f"take_failed_before_fill={metrics['take_failed_before_fill']} "
            f"take_unresolved={metrics['take_unresolved']} "
            f"take_mean_net_usdt_per_assignment={mean_text} "
            f"take_day_cluster_95ci_usdt={interval_text} "
            f"take_mean_net_r_per_assignment={mean_r_text} "
            f"take_day_cluster_95ci_r={interval_r_text} "
            f"invalid_take={metrics['invalid_take']} "
            f"invalid_skip={metrics['invalid_skip']}"
        )


def report(bundle: Path) -> None:
    assignments = _assignments(bundle)
    positions = {
        position["intent_id"]: position
        for position in reconcile(bundle)
        if position.get("complete")
    }
    if not assignments:
        print("long_participation_status=awaiting_tagged_assignments")
        return

    market_candles_by_symbol = load_market_candles(bundle)

    by_arm = {
        arm: [item for item in assignments if item.get("arm") == arm]
        for arm in ("take", "skip")
    }
    take_values_by_day: dict[str, list[float]] = defaultdict(list)
    complete_take_positions = []
    unresolved_take = 0
    failed_take = sum(
        item.get("status") == "FAILED" and not item.get("bybit_order_ids")
        for item in by_arm["take"]
    )
    for assignment in by_arm["take"]:
        position = positions.get(assignment["intent_id"])
        if position is not None:
            complete_take_positions.append(position)
            value = float(position["net_pnl"])
        elif assignment.get("status") == "FAILED" and not assignment.get(
            "bybit_order_ids"
        ):
            value = 0.0
        else:
            unresolved_take += 1
            continue
        timestamp = datetime.fromisoformat(
            str(assignment["created_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        take_values_by_day[timestamp.date().isoformat()].append(value)

    invalid_skip = [
        item
        for item in by_arm["skip"]
        if item.get("status") != "SKIPPED"
        or item.get("approval_mode") != "SKIPPED"
        or item.get("bybit_order_ids")
    ]
    invalid_take = [
        item for item in by_arm["take"] if item.get("approval_mode") != "AUTO"
    ]
    print("long_participation_assignment_basis=auto-approved Strategy V2 LONG plans")
    print(
        "long_participation_assignment_note=skip contributes zero portfolio PnL; "
        "take PnL is not final until its whole position reconciles; unresolved takes "
        "are excluded from the interim comparison"
    )
    print(f"long_participation_take_assigned={len(by_arm['take'])}")
    print(f"long_participation_take_completed={len(complete_take_positions)}")
    print(f"long_participation_take_failed_before_fill={failed_take}")
    print(f"long_participation_take_unresolved={unresolved_take}")
    print(f"long_participation_skip_assigned={len(by_arm['skip'])}")
    print(f"long_participation_invalid_skip_records={len(invalid_skip)}")
    print(f"long_participation_invalid_take_records={len(invalid_take)}")
    print(
        "long_participation_take_completed_metrics=",
        json.dumps(summarize(complete_take_positions), sort_keys=True),
    )
    _report_momentum_strata(assignments, positions, market_candles_by_symbol)

    ready = (
        unresolved_take == 0
        and not invalid_skip
        and not invalid_take
        and len(by_arm["take"]) >= REVIEW_TARGET_PER_ARM
        and len(by_arm["skip"]) >= REVIEW_TARGET_PER_ARM
    )
    print(
        "long_participation_status="
        f"{'ready_for_review' if ready else 'collecting'}"
        f"/take_target:{REVIEW_TARGET_PER_ARM}"
        f"/skip_target:{REVIEW_TARGET_PER_ARM}"
    )
    if ready:
        interval = _day_cluster_interval(take_values_by_day, seed=88231)
        take_mean = sum(sum(values) for values in take_values_by_day.values()) / len(
            by_arm["take"]
        )
        print(f"long_participation_itt_take_mean_net_usdt_per_assignment={take_mean}")
        print("long_participation_itt_skip_mean_net_usdt_per_assignment=0")
        print(f"long_participation_take_day_cluster_95ci_usdt={interval}")
        print(
            "long_participation_itt_caveat=Demo-only, randomized assignment-level "
            "effect; day-cluster interval, no claim of prospective live profitability"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    report(parser.parse_args().bundle)


if __name__ == "__main__":
    main()
