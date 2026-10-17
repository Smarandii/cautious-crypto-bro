"""Measure risk-normalized directional movement after the first actual fill."""

from __future__ import annotations

import json
import random
import statistics
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import analyze_forensic_pnl as forensic_pnl
import replay_strategy_v2 as replay

HORIZONS_MINUTES = (5, 15, 60, 240, 1440)
BOOTSTRAP_ITERATIONS = 2_000
PROSPECTIVE_LONG_COHORT_START = datetime(2026, 10, 8, 8, 49, 47, tzinfo=UTC)
PROSPECTIVE_LONG_RISK_PCT = 0.10
PROSPECTIVE_EXIT_PROFILE = "payoff_early_trail"


def prospective_long_cohort(cases: list[dict]) -> list[dict]:
    """Select only plans created under the current 0.10% LONG risk cohort."""
    cohort = []
    for case in cases:
        created_at = case.get("plan_created_at")
        if not created_at or case.get("side") != "LONG":
            continue
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed_created_at.tzinfo is None:
            parsed_created_at = parsed_created_at.replace(tzinfo=UTC)
        try:
            risk_pct = float(case.get("risk_per_trade_pct"))
        except (TypeError, ValueError):
            continue
        if (
            parsed_created_at.astimezone(UTC) >= PROSPECTIVE_LONG_COHORT_START
            and abs(risk_pct - PROSPECTIVE_LONG_RISK_PCT) <= 1e-9
            and case.get("exit_profile") == PROSPECTIVE_EXIT_PROFILE
        ):
            cohort.append(case)
    return cohort


def fixed_horizon_returns(
    cases: list[dict],
    *,
    horizon_minutes: int,
) -> dict[str, list[float]]:
    """Return signed price moves in initial-stop R at a fixed time horizon."""
    if horizon_minutes <= 0:
        raise ValueError("Horizon must be positive")

    returns = {"LONG": [], "SHORT": []}
    horizon_ms = horizon_minutes * 60_000

    for case in sorted(cases, key=lambda item: item["start"]):
        side = case["side"]
        if side not in returns:
            continue

        target_time = case["start"] + horizon_ms
        candle = next(
            (row for row in case["candles"] if row["time"] + 60_000 >= target_time),
            None,
        )
        if candle is None:
            continue

        entry = case["first_fill_entry"]
        risk_distance = abs(entry - case["stop"])
        if risk_distance <= 0:
            raise ValueError("First-fill price must differ from the initial stop")

        direction = 1 if side == "LONG" else -1
        returns[side].append(direction * (candle["close"] - entry) / risk_distance)

    return returns


def delayed_entry_returns(
    cases: list[dict],
    *,
    delay_minutes: int,
    horizon_minutes: int,
    market_candles_by_symbol: dict[str, list[dict]] | None = None,
) -> dict[str, list[float]]:
    """Measure fixed-horizon R after waiting, skipping cases whose stop was hit."""
    if delay_minutes <= 0 or horizon_minutes <= 0:
        raise ValueError("Entry delay and horizon must be positive")

    returns = {"LONG": [], "SHORT": []}
    delay_ms = delay_minutes * 60_000
    horizon_ms = horizon_minutes * 60_000

    for case in cases:
        side = case["side"]
        if side not in returns:
            continue

        signal_time = case.get("plan_created_at")
        if signal_time:
            parsed = datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            signal_time_ms = int(parsed.timestamp() * 1000)
        else:
            signal_time_ms = int(case["start"])

        candles = sorted(
            (market_candles_by_symbol or {}).get(
                case.get("symbol", ""), case["candles"]
            ),
            key=lambda row: row["time"],
        )
        entry_target = signal_time_ms + delay_ms
        entry_candle = next(
            (
                candle
                for candle in candles
                if candle["time"] >= signal_time_ms
                and candle["time"] + 60_000 >= entry_target
            ),
            None,
        )
        if entry_candle is None:
            continue

        stop = float(case["stop"])
        wait_candles = (
            candle
            for candle in candles
            if signal_time_ms <= candle["time"] <= entry_candle["time"]
        )
        if side == "LONG":
            stop_touched = any(
                float(candle.get("low", candle["close"])) <= stop
                for candle in wait_candles
            )
        else:
            stop_touched = any(
                float(candle.get("high", candle["close"])) >= stop
                for candle in wait_candles
            )
        if stop_touched:
            continue

        exit_target = entry_candle["time"] + horizon_ms
        exit_candle = next(
            (
                candle
                for candle in candles
                if candle["time"] > entry_candle["time"]
                and candle["time"] + 60_000 >= exit_target
            ),
            None,
        )
        if exit_candle is None:
            continue

        entry = float(entry_candle["close"])
        risk_distance = abs(entry - stop)
        if risk_distance <= 0:
            continue
        direction = 1 if side == "LONG" else -1
        returns[side].append(
            direction * (float(exit_candle["close"]) - entry) / risk_distance
        )

    return returns


def pre_signal_momentum_returns(
    cases: list[dict],
    *,
    horizon_minutes: int,
    market_candles_by_symbol: dict[str, list[dict]] | None = None,
) -> dict[str, list[float]]:
    """Measure signed trend before each signal using only fully closed candles."""
    if horizon_minutes <= 0:
        raise ValueError("Horizon must be positive")

    returns = {"LONG": [], "SHORT": []}

    for case in cases:
        value = pre_signal_momentum_r(
            case,
            horizon_minutes=horizon_minutes,
            market_candles_by_symbol=market_candles_by_symbol,
        )
        if value is not None:
            returns[case["side"]].append(value)

    return returns


def pre_signal_momentum_r(
    case: dict,
    *,
    horizon_minutes: int,
    market_candles_by_symbol: dict[str, list[dict]] | None = None,
) -> float | None:
    """Return signed pre-signal momentum using only fully closed candles."""
    if horizon_minutes <= 0:
        raise ValueError("Horizon must be positive")
    side = case.get("side")
    created_at = case.get("plan_created_at")
    if side not in ("LONG", "SHORT") or not created_at:
        return None

    signal_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if signal_time.tzinfo is None:
        signal_time = signal_time.replace(tzinfo=UTC)
    signal_time_ms = int(signal_time.timestamp() * 1000)
    candles = (market_candles_by_symbol or {}).get(
        case.get("symbol", ""), case["candles"]
    )
    closed_candles = sorted(
        (row for row in candles if row["time"] + 60_000 <= signal_time_ms),
        key=lambda row: row["time"],
    )
    if not closed_candles:
        return None

    latest = closed_candles[-1]
    prior = next(
        (
            row
            for row in reversed(closed_candles[:-1])
            if row["time"] <= latest["time"] - horizon_minutes * 60_000
        ),
        None,
    )
    if prior is None:
        return None

    risk_distance = abs(case["first_fill_entry"] - case["stop"])
    if risk_distance <= 0:
        raise ValueError("First-fill price must differ from the initial stop")
    direction = 1 if side == "LONG" else -1
    return direction * (latest["close"] - prior["close"]) / risk_distance


def realized_momentum_filter_rows(
    cases: list[dict],
    positions: list[dict],
    market_candles_by_symbol: dict[str, list[dict]],
    *,
    horizon_minutes: int,
) -> list[dict]:
    """Pair pre-signal momentum with whole-position, reconciled realized R."""
    positions_by_intent = {
        position["intent_id"]: position
        for position in positions
        if position["complete"]
    }
    paired = []
    for case in cases:
        position = positions_by_intent.get(case.get("intent_id"))
        if position is None:
            continue
        momentum_r = pre_signal_momentum_r(
            case,
            horizon_minutes=horizon_minutes,
            market_candles_by_symbol=market_candles_by_symbol,
        )
        if momentum_r is None:
            continue
        paired.append(
            {
                "intent_id": case["intent_id"],
                "start": position["start"],
                "side": position["side"],
                "momentum_r": momentum_r,
                "net_r": position["net_r"],
                "plan_created_at": case["plan_created_at"],
                "exit_profile": case.get("exit_profile"),
            }
        )
    return sorted(paired, key=lambda row: row["start"])


def prospective_realized_momentum_filter_rows(
    cases: list[dict],
    positions: list[dict],
    market_candles_by_symbol: dict[str, list[dict]],
    *,
    horizon_minutes: int,
    created_after: datetime,
) -> list[dict]:
    """Select fully reconciled momentum cases created after a frozen cutoff."""
    cutoff = _parse_utc_datetime(created_after.isoformat())
    paired = realized_momentum_filter_rows(
        cases,
        positions,
        market_candles_by_symbol,
        horizon_minutes=horizon_minutes,
    )
    return [
        row for row in paired if _parse_utc_datetime(row["plan_created_at"]) >= cutoff
    ]


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def report_prospective_momentum_filter(
    cases: list[dict],
    positions: list[dict],
    market_candles_by_symbol: dict[str, list[dict]],
    *,
    created_after: datetime,
) -> None:
    """Score a frozen 60-minute signal filter on later completed positions."""
    cutoff = _parse_utc_datetime(created_after.isoformat())
    rows = prospective_realized_momentum_filter_rows(
        cases,
        positions,
        market_candles_by_symbol,
        horizon_minutes=60,
        created_after=created_after,
    )
    print(
        "prospective_momentum_filter_note=shadow counterfactual only; all actual "
        "positions were taken; score only fully reconciled positions created "
        "after the cutoff; keep profile and side groups separate; retained means "
        "60m pre-signal direction-aligned momentum <= 0; fees and funding are "
        "included in net R; no live policy change"
    )
    print(
        "prospective_momentum_filter_after="
        f"{cutoff.isoformat()} complete_pairs={len(rows)}"
    )
    profiles = sorted({row.get("exit_profile") or "unknown" for row in rows})
    for profile in profiles:
        for side in ("LONG", "SHORT"):
            group = [
                row
                for row in rows
                if (row.get("exit_profile") or "unknown") == profile
                and row["side"] == side
            ]
            if not group:
                continue
            retained = [row for row in group if row["momentum_r"] <= 0]
            skipped = [row for row in group if row["momentum_r"] > 0]
            summaries = {
                label: summarize_returns(
                    [row["net_r"] for row in selected],
                    seed=20261009 + len(selected) + (side == "SHORT"),
                )
                for label, selected in (
                    ("all", group),
                    ("retained", retained),
                    ("skipped", skipped),
                )
            }
            print(
                "prospective_momentum_filter_group="
                f"profile={profile} side={side} "
                f"metrics={json.dumps(summaries, sort_keys=True)}"
            )


def report_realized_momentum_filter(
    bundle: Path,
    cases: list[dict],
    market_candles_by_symbol: dict[str, list[dict]],
) -> None:
    """Evaluate a predeclared zero-threshold filter on reconciled outcomes."""
    positions = forensic_pnl.reconcile(bundle)
    print(
        "realized_momentum_filter_note=research only; paired by intent_id to "
        "fully reconciled whole-position PnL; feature uses only pre-signal closed "
        "candles; candidate skips signals with positive side-aligned momentum; "
        "chronological splits and holdout are already inspected; not prospective "
        "evidence and no live policy change"
    )
    for horizon in (60, 240):
        paired = realized_momentum_filter_rows(
            cases,
            positions,
            market_candles_by_symbol,
            horizon_minutes=horizon,
        )
        first = int(len(paired) * 0.50)
        second = int(len(paired) * 0.70)
        splits = {
            "train": paired[:first],
            "validation": paired[first:second],
            "holdout": paired[second:],
        }
        print(
            f"realized_momentum_filter_horizon={horizon}m "
            f"complete_pairs={len(paired)} split=50/20/30"
        )
        for split, rows in splits.items():
            for side in ("ALL", "LONG", "SHORT"):
                side_rows = [
                    row for row in rows if side == "ALL" or row["side"] == side
                ]
                for rule, selected in (
                    ("all", side_rows),
                    (
                        "skip_aligned_positive",
                        [row for row in side_rows if row["momentum_r"] <= 0],
                    ),
                ):
                    metrics = summarize_returns(
                        [row["net_r"] for row in selected],
                        seed=20261008 + horizon + len(selected),
                    )
                    wins = [row["net_r"] for row in selected if row["net_r"] > 0]
                    losses = [row["net_r"] for row in selected if row["net_r"] < 0]
                    payoff_ratio = (
                        statistics.mean(wins) / abs(statistics.mean(losses))
                        if wins and losses
                        else None
                    )
                    metrics["avg_win_loss_ratio_r"] = payoff_ratio
                    metrics["breakeven_win_rate_pct"] = (
                        100 / (1 + payoff_ratio) if payoff_ratio is not None else None
                    )
                    print(
                        f"realized_momentum_filter={horizon}m/{split}/{side}/{rule} "
                        f"metrics={json.dumps(metrics, sort_keys=True)}"
                    )


def load_market_candles(bundle: Path) -> dict[str, list[dict]]:
    """Load raw archive candles, including the pre-fill lookback omitted by replay."""
    candles_by_symbol = {}
    with zipfile.ZipFile(bundle) as archive:
        root = archive.namelist()[0].split("/", 1)[0]
        for name in archive.namelist():
            prefix = f"{root}/market_1m/"
            if not name.startswith(prefix) or not name.endswith(".jsonl"):
                continue

            symbol = name[len(prefix) : -len(".jsonl")]
            candles_by_symbol[symbol] = [
                {
                    "time": int(row["startTime"]),
                    "close": float(row["close"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                }
                for line in archive.read(name).decode().splitlines()
                if line.strip()
                for row in [json.loads(line)]
            ]

    return candles_by_symbol


def _bootstrap_mean_interval(
    values: list[float],
    *,
    block_size: int,
    iterations: int,
    seed: int,
) -> tuple[float, float] | None:
    if not values:
        return None
    if block_size < 1 or iterations < 1:
        raise ValueError("Block size and bootstrap iterations must be positive")

    rng = random.Random(seed)
    sample_means = []
    sample_size = len(values)

    for _ in range(iterations):
        sample = []
        while len(sample) < sample_size:
            start = rng.randrange(sample_size)
            sample.extend(
                values[(start + offset) % sample_size] for offset in range(block_size)
            )
        sample_means.append(statistics.mean(sample[:sample_size]))

    sample_means.sort()
    lower = sample_means[int(0.025 * iterations)]
    upper = sample_means[min(int(0.975 * iterations), iterations - 1)]
    return lower, upper


def summarize_returns(
    values: list[float],
    *,
    seed: int,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> dict[str, float | int | tuple[float, float] | None]:
    if not values:
        return {
            "count": 0,
            "positive_pct": None,
            "mean_r": None,
            "median_r": None,
            "iid95": None,
            "block4_95": None,
        }

    return {
        "count": len(values),
        "positive_pct": sum(value > 0 for value in values) / len(values),
        "mean_r": statistics.mean(values),
        "median_r": statistics.median(values),
        "iid95": _bootstrap_mean_interval(
            values,
            block_size=1,
            iterations=iterations,
            seed=seed,
        ),
        "block4_95": _bootstrap_mean_interval(
            values,
            block_size=4,
            iterations=iterations,
            seed=seed + 1,
        ),
    }


def fill_followthrough_summary(bundle: Path) -> dict[str, object]:
    """Summarize fixed-horizon directional movement for every filled case."""
    _, cases, _, _ = replay.load_cases(bundle)
    by_horizon_minutes = {}
    for horizon_minutes in (60, 240):
        returns_by_side = fixed_horizon_returns(
            cases,
            horizon_minutes=horizon_minutes,
        )
        by_horizon_minutes[str(horizon_minutes)] = {
            side: summarize_returns(
                returns,
                seed=20261008 + horizon_minutes + (side == "SHORT"),
            )
            for side, returns in returns_by_side.items()
        }

    return {
        "available": bool(cases),
        "filled_case_count": len(cases),
        "basis": "first_fill_to_fixed_horizon_close_in_initial_stop_R",
        "note": (
            "Fixed-horizon mark-to-market in initial-stop R after first actual "
            "fill; not realized trade PnL. Ignores exits, fees, funding, and later "
            "entries. Cases without future candles are omitted. Block-4 intervals "
            "do not control shared market regimes, and horizons overlap."
        ),
        "by_horizon_minutes": by_horizon_minutes if cases else {},
    }


def analyze_bundle(
    bundle: Path,
    *,
    prospective_momentum_filter_after: datetime | None = None,
) -> None:
    _, cases, _, _ = replay.load_cases(bundle)
    market_candles_by_symbol = load_market_candles(bundle)
    report_realized_momentum_filter(bundle, cases, market_candles_by_symbol)
    if prospective_momentum_filter_after is not None:
        report_prospective_momentum_filter(
            cases,
            forensic_pnl.reconcile(bundle),
            market_candles_by_symbol,
            created_after=prospective_momentum_filter_after,
        )

    print(
        "note=exploratory signal follow-through only; uses the first actual fill "
        "and initial stop distance for every filled case, ignores exits/fees/later "
        "entries, and omits cases without a future candle; horizons are not "
        "independent tests"
    )
    print(f"filled_cases={len(cases)}")

    print(
        "delayed_entry_note=counterfactual market entry at the first completed "
        "1m candle after waiting; skip if the original stop was touched while "
        "waiting; fixed-horizon mark-to-market in new-entry stop-R; ignores fees, "
        "slippage, exits, and later fills; not a trade replay"
    )
    for delay in (15, 60):
        for horizon in (60, 240):
            side_returns = delayed_entry_returns(
                cases,
                delay_minutes=delay,
                horizon_minutes=horizon,
                market_candles_by_symbol=market_candles_by_symbol,
            )
            for side in ("LONG", "SHORT"):
                metrics = summarize_returns(
                    side_returns[side],
                    seed=20261008 + delay * 10 + horizon + (side == "SHORT"),
                )
                interval = metrics["block4_95"]
                interval_text = (
                    "n/a"
                    if interval is None
                    else f"[{interval[0]:+.3f},{interval[1]:+.3f}]"
                )
                mean_r = metrics["mean_r"]
                mean_text = "n/a" if mean_r is None else f"{mean_r:+.3f}R"
                print(
                    f"delayed_entry_wait_min={delay} horizon_min={horizon} "
                    f"side={side} n={metrics['count']} mean={mean_text} "
                    f"block4_95={interval_text}"
                )

    cohort = prospective_long_cohort(cases)
    print(
        "prospective_long_cohort="
        f"start={PROSPECTIVE_LONG_COHORT_START.isoformat()} "
        f"risk_pct={PROSPECTIVE_LONG_RISK_PCT:.2f} "
        f"exit_profile={PROSPECTIVE_EXIT_PROFILE} cases={len(cohort)}"
    )

    for horizon in (15, 60, 240):
        side_returns = pre_signal_momentum_returns(
            cases,
            horizon_minutes=horizon,
            market_candles_by_symbol=market_candles_by_symbol,
        )
        for side in ("LONG", "SHORT"):
            metrics = summarize_returns(
                side_returns[side],
                seed=20261008 + horizon + (side == "SHORT") + 1000,
            )
            mean_r = metrics["mean_r"]
            positive_pct = metrics["positive_pct"]
            mean_text = "n/a" if mean_r is None else f"{mean_r:+.3f}R"
            positive_text = "n/a" if positive_pct is None else f"{positive_pct:.1%}"
            print(
                f"pre_signal_horizon_min={horizon} side={side} "
                f"n={metrics['count']} aligned={positive_text} "
                f"mean={mean_text} iid95={metrics['iid95']} "
                f"block4_95={metrics['block4_95']}"
            )

    for horizon in HORIZONS_MINUTES:
        side_returns = fixed_horizon_returns(
            cases,
            horizon_minutes=horizon,
        )
        for side in ("LONG", "SHORT"):
            metrics = summarize_returns(
                side_returns[side],
                seed=20261008 + horizon + (side == "SHORT"),
            )
            iid = metrics["iid95"]
            block = metrics["block4_95"]
            iid_text = "n/a" if iid is None else f"[{iid[0]:+.3f},{iid[1]:+.3f}]"
            block_text = (
                "n/a" if block is None else f"[{block[0]:+.3f},{block[1]:+.3f}]"
            )
            positive_pct = metrics["positive_pct"]
            positive_text = "n/a" if positive_pct is None else f"{positive_pct:.1%}"
            mean_r = metrics["mean_r"]
            mean_text = "n/a" if mean_r is None else f"{mean_r:+.3f}R"
            median_r = metrics["median_r"]
            median_text = "n/a" if median_r is None else f"{median_r:+.3f}R"
            print(
                f"horizon_min={horizon} side={side} n={metrics['count']} "
                f"positive={positive_text} mean={mean_text} median={median_text} "
                f"iid95={iid_text} block4_95={block_text}"
            )

    for horizon in (60, 240):
        values = fixed_horizon_returns(cohort, horizon_minutes=horizon)["LONG"]
        metrics = summarize_returns(values, seed=20261008 + horizon + 17)
        mean_r = metrics["mean_r"]
        block = metrics["block4_95"]
        mean_text = "n/a" if mean_r is None else f"{mean_r:+.3f}R"
        block_text = "n/a" if block is None else f"[{block[0]:+.3f},{block[1]:+.3f}]"
        positive_pct = metrics["positive_pct"]
        positive_text = "n/a" if positive_pct is None else f"{positive_pct:.1%}"
        print(
            f"prospective_long_horizon_min={horizon} n={metrics['count']} "
            f"positive={positive_text} mean={mean_text} block4_95={block_text}"
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--prospective-momentum-filter-after",
        type=_parse_utc_datetime,
        metavar="ISO_TIMESTAMP",
        help=(
            "score the frozen 60-minute momentum filter on fully reconciled "
            "positions created after this UTC timestamp"
        ),
    )
    args = parser.parse_args()
    analyze_bundle(
        args.bundle,
        prospective_momentum_filter_after=args.prospective_momentum_filter_after,
    )


if __name__ == "__main__":
    main()
