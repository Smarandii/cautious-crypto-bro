"""Reconcile Bybit closed PnL and funding to Strategy V2 positions."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

LONG_RISK_SHADOW_FROZEN_AFTER = "2026-10-07T16:40:50+00:00"
LONG_RISK_SHADOW_MULTIPLIER = 0.25
LONG_RISK_CURVE_FROZEN_AFTER = "2026-10-07T19:50:18+00:00"
LONG_RISK_CURVE_MULTIPLIERS = (0.0, 0.25, 0.5, 1.0)
LIVE_LONG_RISK_STARTED_AT = "2026-10-08T08:49:47+00:00"
LIVE_LONG_RISK_MULTIPLIER = 0.10
BASELINE_RISK_PER_TRADE_PCT = 1.0
SOURCE_SIDE_HEALTH_SHADOW_FROZEN_AFTER = "2026-10-07T17:42:41+00:00"
SOURCE_SIDE_HEALTH_WINDOW = 3
PROSPECTIVE_SHADOW_REVIEW_CASES = 20
DEMO_EXIT_PROFILE_REVIEW_CASES = 20
DEMO_PAYOFF_EXIT_PROFILE = "payoff_early_trail"


def jsonl(archive: zipfile.ZipFile, root: str, name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in archive.read(f"{root}/{name}").decode().splitlines()
        if line.strip()
    ]


def read_open_positions_snapshot(path: Path) -> tuple[list[dict], str | None]:
    """Read open positions and their capture time when present in an archive."""
    with zipfile.ZipFile(path) as archive:
        if not archive.namelist():
            return [], None
        root = archive.namelist()[0].split("/", 1)[0]
        name = f"{root}/bybit/open_positions.jsonl"
        members = set(archive.namelist())
        positions = (
            jsonl(archive, root, "bybit/open_positions.jsonl")
            if name in members
            else []
        )
        metadata_name = f"{root}/metadata.json"
        metadata = (
            json.loads(archive.read(metadata_name)) if metadata_name in members else {}
        )
        return positions, metadata.get("open_positions_as_of")


def read_open_positions(path: Path) -> list[dict]:
    """Read account-level open positions, or an empty list for older bundles."""
    return read_open_positions_snapshot(path)[0]


def position_side(closing_side: str) -> str:
    return "LONG" if closing_side == "Sell" else "SHORT"


def wilson_interval(wins: int, trials: int) -> tuple[float, float] | None:
    """Return an approximate 95% Wilson interval for a binomial win rate."""
    if trials == 0:
        return None
    z = 1.96
    rate = wins / trials
    denominator = 1 + z**2 / trials
    center = (rate + z**2 / (2 * trials)) / denominator
    margin = (
        z * (rate * (1 - rate) / trials + z**2 / (4 * trials**2)) ** 0.5 / denominator
    )
    return (
        100 * max(0.0, center - margin),
        100 * min(1.0, center + margin),
    )


def reconcile(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        root = archive.namelist()[0].split("/", 1)[0]
        lineage = jsonl(archive, root, "analysis/trade_lineage.jsonl")
        executions = jsonl(archive, root, "bybit/executions.jsonl")
        closed_pnl = jsonl(archive, root, "bybit/closed_pnl.jsonl")
        settlements = jsonl(archive, root, "bybit/transaction_log.jsonl")

    executions_by_order = defaultdict(list)
    for execution in executions:
        if execution.get("execType") == "Trade":
            executions_by_order[str(execution.get("orderId") or "")].append(execution)

    positions = []
    for source in lineage:
        for item in source["intents"]:
            intent = item["intent"]
            metadata = item.get("metadata") or {}
            entry_fills = [
                fill
                for order_id in item.get("entry_order_ids", [])
                for fill in executions_by_order.get(str(order_id), [])
                if fill.get("execType") == "Trade"
                and float(fill.get("closedSize") or 0) == 0
            ]
            if not entry_fills:
                continue
            filled_legs = sorted(
                {
                    str(fill.get("orderLinkId") or "").rsplit("-", 1)[-1].upper()
                    for fill in entry_fills
                    if str(fill.get("orderLinkId") or "").rsplit("-", 1)[-1].upper()
                    in {"E1", "E2", "E3"}
                }
            )
            positions.append(
                {
                    "intent_id": item.get("intent_id", ""),
                    "symbol": intent["symbol"].upper(),
                    "side": intent["side"],
                    "source_channel_id": metadata.get("source_channel_id"),
                    "confidence": metadata.get("confidence"),
                    "entry_type": metadata.get("entry_type"),
                    "risk_per_trade_pct": metadata.get("risk_per_trade_pct"),
                    "exit_profile": metadata.get("exit_profile", "baseline"),
                    "planned_max_loss_usdt": (
                        float(metadata["planned_max_loss_usdt"])
                        if metadata.get("planned_max_loss_usdt") is not None
                        else None
                    ),
                    "fill_pattern": "+".join(filled_legs),
                    "stop": float(intent["stop_loss"]),
                    "start": min(int(fill["execTime"]) for fill in entry_fills),
                    "entry_qty": sum(float(fill["execQty"]) for fill in entry_fills),
                    "risk_usdt": sum(
                        float(fill["execQty"])
                        * abs(float(fill["execPrice"]) - float(intent["stop_loss"]))
                        for fill in entry_fills
                    ),
                    "closed_qty": 0.0,
                    "closed_at": 0,
                    "closed_pnl": 0.0,
                    "fees": 0.0,
                    "funding": 0.0,
                    "pnl_rows": 0,
                    "ambiguous_rows": 0,
                }
            )

    positions.sort(key=lambda item: item["start"])
    by_symbol = defaultdict(list)
    for position in positions:
        by_symbol[position["symbol"]].append(position)

    for symbol_positions in by_symbol.values():
        for index, position in enumerate(symbol_positions):
            position["end"] = (
                symbol_positions[index + 1]["start"]
                if index + 1 < len(symbol_positions)
                else float("inf")
            )

    for row in closed_pnl:
        symbol = str(row.get("symbol") or "").upper()
        side = position_side(str(row.get("side") or ""))
        timestamp = int(row.get("updatedTime") or row.get("createdTime") or 0)
        matches = [
            position
            for position in by_symbol.get(symbol, [])
            if position["side"] == side
            and position["start"] <= timestamp < position["end"]
        ]
        if len(matches) != 1:
            if len(matches) > 1:
                for position in matches:
                    position["ambiguous_rows"] += 1
            continue
        position = matches[0]
        position["closed_qty"] += float(row.get("closedSize") or row.get("qty") or 0)
        position["closed_at"] = max(position["closed_at"], timestamp)
        position["closed_pnl"] += float(row.get("closedPnl") or 0)
        position["fees"] += float(row.get("openFee") or 0) + float(
            row.get("closeFee") or 0
        )
        position["pnl_rows"] += 1

    for row in settlements:
        if str(row.get("type", "")).upper() != "SETTLEMENT":
            continue
        symbol = str(row.get("symbol") or "").upper()
        timestamp = int(row.get("transactionTime") or 0)
        matches = [
            position
            for position in by_symbol.get(symbol, [])
            if position["start"] <= timestamp < position["end"]
        ]
        if len(matches) == 1:
            matches[0]["funding"] += float(row.get("funding") or 0)

    for position in positions:
        position["closed_fraction"] = (
            position["closed_qty"] / position["entry_qty"]
            if position["entry_qty"] > 0
            else 0.0
        )
        position["complete"] = (
            position["closed_fraction"] >= 0.99
            and position["ambiguous_rows"] == 0
            and position["pnl_rows"] > 0
        )
        # Bybit Closed P&L already nets opening/closing fees and funding.
        # Keep the transaction-log funding sum for reconciliation diagnostics,
        # but do not add it to Closed P&L a second time.
        position["net_pnl"] = position["closed_pnl"]
        # Bybit's closed P&L = gross price P&L - fees + signed funding.
        # This derived figure is diagnostic; only closed_pnl is realized net.
        position["gross_pnl"] = (
            position["net_pnl"] + position["fees"] - position["funding"]
        )
        position["net_r"] = (
            position["net_pnl"] / position["risk_usdt"]
            if position["risk_usdt"] > 0
            else 0.0
        )
    return positions


def cost_summary(positions: list[dict]) -> dict[str, float | int | None]:
    """Summarize fees and the implied gross price P&L for reconciled positions."""
    net = sum(position["net_pnl"] for position in positions)
    gross = sum(position["gross_pnl"] for position in positions)
    fees = sum(position["fees"] for position in positions)
    funding = sum(position["funding"] for position in positions)
    net_winners = [position for position in positions if position["net_pnl"] > 0]
    avg_win = (
        statistics.mean(position["net_pnl"] for position in net_winners)
        if net_winners
        else None
    )
    avg_winner_fee = (
        statistics.mean(position["fees"] for position in net_winners)
        if net_winners
        else None
    )
    return {
        "positions": len(positions),
        "gross_price_pnl_usdt": gross,
        "fees_usdt": fees,
        "signed_funding_usdt": funding,
        "closed_pnl_net_usdt": net,
        "fee_share_of_abs_gross_pct": 100 * fees / abs(gross) if gross else None,
        "avg_net_winner_usdt": avg_win,
        "avg_fee_per_net_winner_usdt": avg_winner_fee,
        "winner_fee_pct_of_avg_net_win": (
            100 * avg_winner_fee / avg_win
            if avg_winner_fee is not None and avg_win
            else None
        ),
    }


def summarize(
    positions: list[dict],
) -> dict[str, float | int | tuple[float, float] | None]:
    wins = [position["net_pnl"] for position in positions if position["net_pnl"] > 0]
    losses = [position["net_pnl"] for position in positions if position["net_pnl"] < 0]
    win_rs = [position["net_r"] for position in positions if position["net_r"] > 0]
    loss_rs = [position["net_r"] for position in positions if position["net_r"] < 0]
    win_risks = [
        position["risk_usdt"] for position in positions if position["net_pnl"] > 0
    ]
    loss_risks = [
        position["risk_usdt"] for position in positions if position["net_pnl"] < 0
    ]
    gross_loss = abs(sum(losses))
    gross_loss_r = abs(sum(loss_rs))
    total_risk = sum(position["risk_usdt"] for position in positions)
    average_win = statistics.mean(wins) if wins else None
    average_loss = statistics.mean(losses) if losses else None
    average_win_r = statistics.mean(win_rs) if win_rs else None
    average_loss_r = statistics.mean(loss_rs) if loss_rs else None
    win_rate_interval = wilson_interval(len(wins), len(positions))
    return {
        "positions": len(positions),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": 100 * len(wins) / len(positions) if positions else None,
        "win_rate_95ci_pct": win_rate_interval,
        "net_pnl_usdt": sum(position["net_pnl"] for position in positions),
        "avg_win_usdt": average_win,
        "avg_loss_usdt": average_loss,
        "avg_win_loss_ratio": (
            average_win / abs(average_loss)
            if average_win is not None and average_loss is not None
            else None
        ),
        "breakeven_win_rate_pct": (
            100 * abs(average_loss) / (average_win + abs(average_loss))
            if average_win is not None and average_loss is not None
            else None
        ),
        "profit_factor": (sum(wins) / gross_loss if gross_loss else float("inf")),
        "expectancy_usdt": (
            statistics.mean(position["net_pnl"] for position in positions)
            if positions
            else None
        ),
        "total_initial_risk_usdt": total_risk,
        "avg_initial_risk_win_usdt": (
            statistics.mean(win_risks) if win_risks else None
        ),
        "avg_initial_risk_loss_usdt": (
            statistics.mean(loss_risks) if loss_risks else None
        ),
        "avg_planned_max_loss_usdt": (
            statistics.mean(
                [
                    position["planned_max_loss_usdt"]
                    for position in positions
                    if position.get("planned_max_loss_usdt") is not None
                ]
            )
            if any(
                position.get("planned_max_loss_usdt") is not None
                for position in positions
            )
            else None
        ),
        "risk_weighted_return_pct": (
            100 * sum(position["net_pnl"] for position in positions) / total_risk
            if total_risk > 0
            else None
        ),
        "net_r": sum(position["net_r"] for position in positions),
        "expectancy_r": (
            statistics.mean(position["net_r"] for position in positions)
            if positions
            else None
        ),
        "avg_win_r": statistics.mean(win_rs) if win_rs else None,
        "avg_loss_r": statistics.mean(loss_rs) if loss_rs else None,
        "avg_win_loss_ratio_r": (
            average_win_r / abs(average_loss_r)
            if average_win_r is not None and average_loss_r is not None
            else None
        ),
        "breakeven_win_rate_r_pct": (
            100 * abs(average_loss_r) / (average_win_r + abs(average_loss_r))
            if average_win_r is not None and average_loss_r is not None
            else None
        ),
        "profit_factor_r": (
            sum(win_rs) / gross_loss_r if gross_loss_r else float("inf")
        ),
    }


def bootstrap_performance_intervals(
    positions: list[dict],
    *,
    iterations: int = 10_000,
    block_size: int = 4,
    seed: int = 741_903,
) -> dict[str, dict[str, tuple[float, float] | None]]:
    """Bootstrap payoff and expectancy intervals with IID and circular blocks."""
    if iterations < 1 or block_size < 1:
        raise ValueError("Bootstrap iterations and block size must be positive")

    metric_names = ("mean_net_r", "mean_net_pnl_usdt", "avg_win_loss_ratio")
    method_names = ("iid", f"circular_block_{block_size}")
    if not positions:
        return {method: dict.fromkeys(metric_names) for method in method_names}

    ordered = sorted(positions, key=lambda position: position["start"])
    rng = random.Random(seed)
    samples = {method: [] for method in method_names}
    for _ in range(iterations):
        iid_sample = rng.choices(ordered, k=len(ordered))
        samples["iid"].append(_performance_metrics(iid_sample))

        block_sample = []
        while len(block_sample) < len(ordered):
            start = rng.randrange(len(ordered))
            block_sample.extend(
                ordered[(start + offset) % len(ordered)] for offset in range(block_size)
            )
        samples[method_names[1]].append(
            _performance_metrics(block_sample[: len(ordered)])
        )

    return {
        method: {
            name: _percentile_95_interval([sample[index] for sample in method_samples])
            for index, name in enumerate(metric_names)
        }
        for method, method_samples in samples.items()
    }


def _performance_metrics(positions: list[dict]) -> tuple[float, float, float | None]:
    winners = [position["net_pnl"] for position in positions if position["net_pnl"] > 0]
    losers = [position["net_pnl"] for position in positions if position["net_pnl"] < 0]
    payoff_ratio = (
        statistics.mean(winners) / abs(statistics.mean(losers))
        if winners and losers
        else None
    )
    return (
        statistics.mean(position["net_r"] for position in positions),
        statistics.mean(position["net_pnl"] for position in positions),
        payoff_ratio,
    )


def _percentile_95_interval(
    values: list[float | None],
) -> tuple[float, float] | None:
    defined = sorted(value for value in values if value is not None)
    if not defined:
        return None
    last = len(defined) - 1
    return defined[int(0.025 * last)], defined[int(0.975 * last)]


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
    """Report actual Demo LONG results at the configured reduced-risk size."""
    live_longs = prospective_live_long_risk_positions(positions)
    cutoff = datetime.fromisoformat(LIVE_LONG_RISK_STARTED_AT).astimezone(UTC)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    short_controls = [
        position
        for position in positions
        if position.get("complete")
        and position.get("side") == "SHORT"
        and position.get("start", 0) > cutoff_ms
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
        "prospective_live_long_risk_bootstrap_95ci=LONG",
        json.dumps(bootstrap_performance_intervals(live_longs), sort_keys=True),
    )


def report_prospective_demo_exit_profile(positions: list[dict]) -> None:
    """Report realized results for the tagged Demo payoff-exit cohort."""
    filled = [
        position
        for position in positions
        if position.get("exit_profile") == DEMO_PAYOFF_EXIT_PROFILE
    ]
    complete = [position for position in filled if position.get("complete")]
    status = (
        "ready_for_review"
        if len(complete) >= DEMO_EXIT_PROFILE_REVIEW_CASES
        else "collecting"
    )
    print(f"prospective_demo_exit_profile={DEMO_PAYOFF_EXIT_PROFILE}")
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
        "--prospective-demo-exit-profile",
        action="store_true",
        help=(
            "Report fully reconciled Demo positions whose persisted policy is "
            "tagged with the active Demo payoff-exit profile."
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
        return
    if args.prospective_demo_exit_profile:
        report_prospective_demo_exit_profile(positions)
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
