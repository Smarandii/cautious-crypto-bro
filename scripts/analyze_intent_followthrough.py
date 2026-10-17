"""Measure signal-time price movement for all NEW intents, not just fills."""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

HORIZONS_MINUTES = (5, 15, 60, 240)


def load_new_intents(database: Path) -> list[dict]:
    """Read every NEW intent, retaining execution status and missing plans."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT i.status, i.created_at, i.payload_json, p.created_at
            FROM intents AS i
            LEFT JOIN execution_plans AS p ON p.intent_id = i.intent_id
            ORDER BY i.created_at
            """
        ).fetchall()
    finally:
        connection.close()

    cases = []
    for status, intent_created_at, payload_json, plan_created_at in rows:
        payload = json.loads(payload_json)
        if payload.get("relation") != "NEW":
            continue
        source = payload.get("source")
        source = source if isinstance(source, dict) else {}
        source_label = (
            source.get("channel_title") or source.get("channel_id") or "unknown"
        )
        created_at = plan_created_at or intent_created_at
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        cases.append(
            {
                "status": status,
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "source": str(source_label),
                "time_ms": int(parsed.timestamp() * 1000),
                "has_plan": plan_created_at is not None,
            }
        )
    return cases


def load_market_candles(bundle: Path) -> dict[str, list[dict]]:
    """Load archived 1m candles keyed by symbol."""
    candles_by_symbol = {}
    with zipfile.ZipFile(bundle) as archive:
        roots = [name.split("/", 1)[0] for name in archive.namelist()]
        if not roots:
            return candles_by_symbol
        root = roots[0]
        prefix = f"{root}/market_1m/"
        for name in archive.namelist():
            if not name.startswith(prefix) or not name.endswith(".jsonl"):
                continue
            symbol = name[len(prefix) : -len(".jsonl")]
            candles_by_symbol[symbol] = [
                {
                    "time": int(row["startTime"]),
                    "close": float(row["close"]),
                }
                for line in archive.read(name).decode().splitlines()
                if line.strip()
                for row in [json.loads(line)]
            ]
    return candles_by_symbol


def signed_horizon_return(
    case: dict,
    candles: list[dict],
    *,
    horizon_minutes: int,
) -> float | None:
    """Return signed percent movement from first closed post-signal candle."""
    if horizon_minutes <= 0:
        raise ValueError("Horizon must be positive")
    if case.get("side") not in ("LONG", "SHORT") or not candles:
        return None

    signal_time = int(case["time_ms"])
    first_full_minute = math.ceil(signal_time / 60_000) * 60_000
    entry_candle = next(
        (row for row in candles if row["time"] >= first_full_minute), None
    )
    if entry_candle is None:
        return None
    target_time = max(
        signal_time + horizon_minutes * 60_000,
        entry_candle["time"] + 60_000,
    )
    exit_candle = next(
        (row for row in candles if row["time"] + 60_000 >= target_time), None
    )
    if exit_candle is None:
        return None

    entry = float(entry_candle["close"])
    if entry <= 0:
        return None
    direction = 1 if case["side"] == "LONG" else -1
    return direction * (float(exit_candle["close"]) / entry - 1)


def benchmark_adjusted_followthrough(
    cases: list[dict],
    candles_by_symbol: dict[str, list[dict]],
    *,
    horizon_minutes: int,
    benchmark_symbol: str = "BTCUSDT",
) -> dict[tuple[str, str], list[dict]]:
    """Pair signal movement with same-window benchmark movement by UTC day."""
    benchmark_candles = candles_by_symbol.get(benchmark_symbol, [])
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for case in cases:
        side = case.get("side")
        symbol = case.get("symbol")
        if side not in ("LONG", "SHORT") or not symbol:
            continue
        asset_return = signed_horizon_return(
            case,
            candles_by_symbol.get(symbol, []),
            horizon_minutes=horizon_minutes,
        )
        benchmark_return = signed_horizon_return(
            case,
            benchmark_candles,
            horizon_minutes=horizon_minutes,
        )
        if asset_return is None or benchmark_return is None:
            continue
        day = datetime.fromtimestamp(case["time_ms"] / 1000, UTC).date().isoformat()
        observation = {
            "day": day,
            "asset_return": asset_return,
            "benchmark_return": benchmark_return,
            "relative_return": asset_return - benchmark_return,
        }
        grouped[(side, case["status"])].append(observation)
        grouped[(side, "ALL")].append(observation)
    return grouped


def day_cluster_interval(
    observations: list[dict],
    *,
    value_key: str = "relative_return",
    iterations: int = 10_000,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Bootstrap observations by UTC day to retain same-day market dependence."""
    clusters: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        clusters[str(observation["day"])].append(float(observation[value_key]))
    days = sorted(clusters)
    if not days:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = []
        for _ in days:
            sample.extend(clusters[rng.choice(days)])
        means.append(statistics.mean(sample))
    means.sort()
    return (
        means[int(0.025 * iterations)],
        means[min(int(0.975 * iterations), iterations - 1)],
    )


def source_side_followthrough(
    cases: list[dict],
    candles_by_symbol: dict[str, list[dict]],
    *,
    horizon_minutes: int,
) -> dict[tuple[str, str, str], dict[str, list[float]]]:
    """Group hypothetical signal movement by source, side, status, and symbol."""
    grouped: dict[tuple[str, str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for case in cases:
        symbol = case.get("symbol")
        value = signed_horizon_return(
            case,
            candles_by_symbol.get(symbol, []),
            horizon_minutes=horizon_minutes,
        )
        if value is None:
            continue
        source = str(case.get("source") or "unknown")
        side = case["side"]
        grouped[(source, side, case["status"])][symbol].append(value)
        grouped[(source, side, "ALL")][symbol].append(value)
    return grouped


def symbol_cluster_interval(
    values_by_symbol: dict[str, list[float]],
    *,
    iterations: int = 2_000,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Bootstrap the mean by resampling symbol clusters with replacement."""
    symbols = sorted(symbol for symbol, values in values_by_symbol.items() if values)
    if not symbols:
        return None
    if iterations < 1:
        raise ValueError("Iterations must be positive")

    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = []
        for _ in symbols:
            sample.extend(values_by_symbol[rng.choice(symbols)])
        means.append(statistics.mean(sample))
    means.sort()
    lower = means[int(0.025 * iterations)]
    upper = means[min(int(0.975 * iterations), iterations - 1)]
    return lower, upper


def purged_chronological_folds(
    cases: list[dict],
    *,
    horizon_minutes: int,
) -> dict[str, list[dict]]:
    """Split signals by time and purge outcomes that cross fold boundaries."""
    if horizon_minutes <= 0:
        raise ValueError("Horizon must be positive")
    ordered = sorted(cases, key=lambda case: case["time_ms"])
    if len(ordered) < 3:
        return {"train": ordered, "validation": [], "holdout": []}

    train_boundary = ordered[len(ordered) * 50 // 100]["time_ms"]
    validation_boundary = ordered[len(ordered) * 70 // 100]["time_ms"]
    horizon_ms = horizon_minutes * 60_000
    return {
        "train": [
            case for case in ordered if case["time_ms"] + horizon_ms <= train_boundary
        ],
        "validation": [
            case
            for case in ordered
            if train_boundary <= case["time_ms"]
            and case["time_ms"] + horizon_ms <= validation_boundary
        ],
        "holdout": [case for case in ordered if case["time_ms"] >= validation_boundary],
    }


def analyze(database: Path, bundle: Path) -> None:
    cases = load_new_intents(database)
    candles_by_symbol = load_market_candles(bundle)
    planned = sum(case["has_plan"] for case in cases)
    print(
        f"new_intents={len(cases)} with_execution_plan={planned} "
        f"without_execution_plan={len(cases) - planned} "
        f"archive_symbols={len(candles_by_symbol)}"
    )
    print(
        "note=hypothetical signed close-to-close percent movement from the first "
        "fully closed 1m candle at/after plan creation; includes failed/skipped "
        "intents; not trade PnL, excludes fees/slippage/exits; cluster bootstrap "
        "resamples symbols, not shared market-time regimes"
    )

    for horizon in HORIZONS_MINUTES:
        grouped: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        missing = defaultdict(int)
        for case in cases:
            side = case.get("side")
            status = case["status"]
            symbol = case.get("symbol")
            candles = candles_by_symbol.get(symbol, [])
            value = signed_horizon_return(case, candles, horizon_minutes=horizon)
            if value is None:
                missing[(side, status)] += 1
                continue
            grouped[(side, "ALL")][symbol].append(value)
            grouped[(side, status)][symbol].append(value)

        print(f"horizon_min={horizon} missing={dict(missing)}")
        for side in ("LONG", "SHORT"):
            for status in ("ALL", "EXECUTED", "FAILED", "SKIPPED"):
                clusters = grouped[(side, status)]
                values = [value for rows in clusters.values() for value in rows]
                if not values:
                    continue
                interval = symbol_cluster_interval(
                    clusters,
                    seed=20261008 + horizon + (side == "SHORT") + (status != "ALL"),
                )
                assert interval is not None
                print(
                    f"side={side} status={status} n={len(values)} "
                    f"symbols={len(clusters)} "
                    f"mean_pct={statistics.mean(values) * 100:+.3f} "
                    f"positive_pct={sum(value > 0 for value in values) / len(values):.1%} "
                    f"cluster95=[{interval[0] * 100:+.3f},{interval[1] * 100:+.3f}]"
                )

    benchmark_groups = benchmark_adjusted_followthrough(
        cases,
        candles_by_symbol,
        horizon_minutes=240,
    )
    print(
        "benchmark_adjusted_note=directional asset close-to-close return minus "
        "same-window BTC directional return; descriptive signal markout, not "
        "trade P&L; day-cluster bootstrap does not remove symbol beta or all "
        "market-regime dependence"
    )
    for side in ("LONG", "SHORT"):
        for status in ("ALL", "EXECUTED", "FAILED", "SKIPPED"):
            observations = benchmark_groups.get((side, status), [])
            if not observations:
                continue
            interval = day_cluster_interval(
                observations,
                seed=20261009
                + len(observations)
                + (side == "SHORT")
                + (status != "ALL"),
            )
            assert interval is not None
            print(
                f"benchmark_adjusted_240m side={side} status={status} "
                f"n={len(observations)} days={len({row['day'] for row in observations})} "
                f"asset_directional_pct={statistics.mean(row['asset_return'] for row in observations) * 100:+.3f} "
                f"btc_directional_pct={statistics.mean(row['benchmark_return'] for row in observations) * 100:+.3f} "
                f"relative_pct={statistics.mean(row['relative_return'] for row in observations) * 100:+.3f} "
                f"day_cluster95=[{interval[0] * 100:+.3f},{interval[1] * 100:+.3f}]"
            )

    print(
        "source_side_note=descriptive breakdown only; small subgroups and shared "
        "market regimes make this unsuitable for selecting a live source filter"
    )
    for horizon in (60, 240):
        grouped = source_side_followthrough(
            cases,
            candles_by_symbol,
            horizon_minutes=horizon,
        )
        for (source, side, status), clusters in sorted(grouped.items()):
            values = [value for rows in clusters.values() for value in rows]
            interval = symbol_cluster_interval(
                clusters,
                seed=20261009 + horizon + (side == "SHORT") + (status != "ALL"),
            )
            assert interval is not None
            print(
                f"source_side_horizon_min={horizon} "
                f"source={json.dumps(source, ensure_ascii=False)} "
                f"side={side} status={status} n={len(values)} "
                f"symbols={len(clusters)} mean_pct={statistics.mean(values) * 100:+.3f} "
                f"cluster95=[{interval[0] * 100:+.3f},{interval[1] * 100:+.3f}]"
            )

    print(
        "source_side_fold_note=chronological 50/20/30 split; purge signals whose "
        "fixed 240m outcome crosses a train/validation boundary"
    )
    for fold, selected_cases in purged_chronological_folds(
        cases,
        horizon_minutes=240,
    ).items():
        grouped = source_side_followthrough(
            selected_cases,
            candles_by_symbol,
            horizon_minutes=240,
        )
        print(f"source_side_fold={fold} intent_cases={len(selected_cases)}")
        for (source, side, status), clusters in sorted(grouped.items()):
            if status != "ALL":
                continue
            values = [value for rows in clusters.values() for value in rows]
            interval = symbol_cluster_interval(clusters)
            assert interval is not None
            print(
                f"source_side_fold_result={fold} "
                f"source={json.dumps(source, ensure_ascii=False)} side={side} "
                f"n={len(values)} symbols={len(clusters)} "
                f"mean_pct={statistics.mean(values) * 100:+.3f} "
                f"cluster95=[{interval[0] * 100:+.3f},{interval[1] * 100:+.3f}]"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    analyze(args.database, args.bundle)


if __name__ == "__main__":
    main()
