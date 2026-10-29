"""Shared forensic archive reconciliation, descriptive statistics and frozen cohorts."""

from __future__ import annotations

import json
import random
import statistics
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

LONG_RISK_SHADOW_FROZEN_AFTER = "2026-10-07T16:40:50+00:00"
LONG_RISK_SHADOW_MULTIPLIER = 0.25
LONG_RISK_CURVE_FROZEN_AFTER = "2026-10-07T19:50:18+00:00"
LONG_RISK_CURVE_MULTIPLIERS = (0.0, 0.25, 0.5, 1.0)
LIVE_LONG_RISK_STARTED_AT = "2026-10-08T08:49:47+00:00"
LIVE_LONG_RISK_MULTIPLIER = 0.10
LIVE_LONG_RISK_EXIT_PROFILE = "payoff_early_trail"
BASELINE_RISK_PER_TRADE_PCT = 1.0
SOURCE_SIDE_HEALTH_SHADOW_FROZEN_AFTER = "2026-10-07T17:42:41+00:00"
SOURCE_SIDE_HEALTH_WINDOW = 3
PROSPECTIVE_SHADOW_REVIEW_CASES = 20
DEMO_EXIT_PROFILE_REVIEW_CASES = 20
DEMO_ENTRY_TTL_REVIEW_CASES = 20
DEMO_PAYOFF_EXIT_PROFILE = "payoff_early_trail"
DEMO_LONG_EXIT_AB_TREATMENT_PROFILE = "payoff_early_tight_trail_long_ab_015"
DEMO_LONG_EXIT_AB_CONTROL_PROFILE = "payoff_early_tight_trail_long_ab_020_control"
# Deployment of v2.2.6/#199; use plan creation, not eventual fill/close time.
LONG_FULL_RISK_STARTED_AT = "2026-10-27T17:06:35+00:00"
# Operator-approved 400 USDT -> 4000 USDT stop-risk cap (v2.2.10/#207), measured
# from the deployed 2.2.10 application container start; plan creation decides.
PORTFOLIO_CAP_4000_STARTED_AT = "2026-10-27T20:11:55+00:00"


PARTICIPATION_ASSIGNMENT_DESCRIPTIONS = {
    "legacy_untagged": (
        "historical untagged assignments; mechanism not frozen; "
        "sample review is descriptive, without causal attribution"
    ),
    "hash_fallback": "stable SHA-256 bucket; momentum unavailable at assignment",
    "momentum_gate": "deterministic momentum gate: positive take, nonpositive skip",
    "unknown": "unknown or inconsistent frozen assignment metadata",
    "mixed": "mixed assignment mechanisms; inspect separate assignment cohorts",
}


def participation_assignment_cohorts(records: list[dict]) -> dict[str, list[dict]]:
    """Partition by frozen provenance, never current settings or later candles."""
    # Zen of Python: "Explicit is better than implicit." Selection mechanisms
    # must not silently supply each other's experimental sample requirements.
    cohorts: dict[str, list[dict]] = {
        name: [] for name in PARTICIPATION_ASSIGNMENT_DESCRIPTIONS if name != "mixed"
    }
    for record in records:
        tag = record.get("long_participation_momentum")
        if tag is None:
            name = "legacy_untagged"
        elif tag == "unavailable":
            name = "hash_fallback"
        elif (tag == "positive" and record.get("arm") == "take") or (
            tag == "nonpositive" and record.get("arm") == "skip"
        ):
            name = "momentum_gate"
        else:
            name = "unknown"
        cohorts[name].append(record)
    return cohorts


def participation_assignment_basis(records: list[dict]) -> str:
    names = [
        name for name, rows in participation_assignment_cohorts(records).items() if rows
    ]
    return names[0] if len(names) == 1 else "mixed" if names else "unknown"


def long_sizing_cohorts(records: list[dict]) -> dict[str, list[dict]]:
    """Keep the verified sizing cutover separate, including its cap-limited plans."""
    boundary = datetime.fromisoformat(LONG_FULL_RISK_STARTED_AT)
    cohorts: dict[str, list[dict]] = {
        "pre_cutover": [],
        "unknown": [],
        "full_baseline_risk": [],
    }
    for record in records:
        try:
            created = datetime.fromisoformat(
                str(record.get("created_at", "")).replace("Z", "+00:00")
            )
            if created.tzinfo is None:
                raise ValueError("Assignment creation time needs a timezone")
            cohort = (
                "full_baseline_risk"
                if created.astimezone(UTC) >= boundary
                else "pre_cutover"
            )
        except (ValueError, TypeError):
            cohort = "unknown"
        cohorts[cohort].append(record)
    return cohorts


def exit_sizing_cohorts(records: list[dict]) -> dict[str, list[dict]]:
    """SHORT sizing was unchanged; never use SHORTs to complete a LONG sample."""
    longs = long_sizing_cohorts([row for row in records if row.get("side") != "SHORT"])
    return {
        "pre_cutover": longs["pre_cutover"],
        "unknown": longs["unknown"],
        "unaffected_shorts": [row for row in records if row.get("side") == "SHORT"],
        "full_baseline_risk": longs["full_baseline_risk"],
    }


def cap_epoch_label(created_at: str) -> str:
    """Label a record with the portfolio stop-risk cap epoch, or `unknown`."""
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("Cap-epoch creation time needs a timezone")
        return (
            "cap_4000"
            if created.astimezone(UTC)
            >= datetime.fromisoformat(PORTFOLIO_CAP_4000_STARTED_AT)
            else "cap_400"
        )
    except (ValueError, TypeError):
        return "unknown"


def cap_epoch_cohorts(records: list[dict]) -> dict[str, list[dict]]:
    """Group planning-side records by the operator cap boundary; never merge."""
    cohorts: dict[str, list[dict]] = {
        "cap_400": [],
        "unknown": [],
        "cap_4000": [],
    }
    for record in records:
        cohorts[cap_epoch_label(record.get("created_at", ""))].append(record)
    return cohorts


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


def read_open_entry_orders_snapshot(
    path: Path,
) -> tuple[list[dict] | None, str | None, int | None]:
    """Read tracked entries and unmatched-entry count from the account snapshot."""
    with zipfile.ZipFile(path) as archive:
        if not archive.namelist():
            return None, None, None
        root = archive.namelist()[0].split("/", 1)[0]
        members = set(archive.namelist())
        name = f"{root}/bybit/open_entry_orders.jsonl"
        metadata_name = f"{root}/metadata.json"
        if name not in members:
            return None, None, None
        metadata = (
            json.loads(archive.read(metadata_name)) if metadata_name in members else {}
        )
        orders_as_of = (
            metadata.get("open_entry_orders_as_of")
            or metadata.get("open_account_orders_as_of")
            or metadata.get("open_positions_as_of")
        )
        unmatched = metadata.get("unmatched_entry_order_count")
        return (
            jsonl(archive, root, "bybit/open_entry_orders.jsonl"),
            orders_as_of,
            int(unmatched) if unmatched is not None else None,
        )


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


def reconcile(path: Path | BinaryIO) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        root = archive.namelist()[0].split("/", 1)[0]
        lineage = jsonl(archive, root, "analysis/trade_lineage.jsonl")
        executions = jsonl(archive, root, "bybit/executions.jsonl")
        closed_pnl = jsonl(archive, root, "bybit/closed_pnl.jsonl")
        settlements = jsonl(archive, root, "bybit/transaction_log.jsonl")
        probe_rows = (
            jsonl(archive, root, "analysis/probe_order_ids.jsonl")
            if f"{root}/analysis/probe_order_ids.jsonl" in archive.namelist()
            else []
        )

    probe_order_ids = {
        str(order_id)
        for row in probe_rows
        for order_id in (row.get("probe_order_ids") or [])
    }
    if not probe_order_ids:
        # Older bundles: only the unambiguous rehearsal prefixes are known.
        probe_order_ids = {
            str(execution.get("orderId") or "")
            for execution in executions
            if execution.get("orderId")
            and str(execution.get("orderLinkId") or "").startswith(
                ("ccb-zone-", "ccb-recover-")
            )
        }

    executions_by_order = defaultdict(list)
    for execution in executions:
        if execution.get("execType") == "Trade":
            executions_by_order[str(execution.get("orderId") or "")].append(execution)

    positions = _entry_positions(lineage, executions_by_order)
    by_symbol = _position_windows(positions)
    owned_order_ids: dict[str, str] = {}
    for source in lineage:
        for item in source["intents"]:
            intent_id = str(item.get("intent_id") or "")
            if not intent_id:
                continue
            for fill in (item.get("bybit") or {}).get("plan_executions") or []:
                order_id = str(fill.get("orderId") or "")
                if order_id:
                    owned_order_ids.setdefault(order_id, intent_id)
    _assign_closed_pnl(by_symbol, closed_pnl, owned_order_ids, probe_order_ids)
    _assign_funding(by_symbol, settlements)
    _finish_positions(positions)
    return positions


def _entry_positions(
    lineage: list[dict], executions_by_order: dict[str, list[dict]]
) -> list[dict]:
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
                    "created_at": item.get("created_at"),
                    "symbol": intent["symbol"].upper(),
                    "side": intent["side"],
                    "source_channel_id": metadata.get("source_channel_id"),
                    "confidence": metadata.get("confidence"),
                    "entry_type": metadata.get("entry_type"),
                    "risk_per_trade_pct": metadata.get("risk_per_trade_pct"),
                    "exit_profile": metadata.get("exit_profile", "baseline"),
                    "long_participation_arm": metadata.get("long_participation_arm"),
                    "entry_order_ttl_minutes": int(
                        metadata.get("entry_order_ttl_minutes") or 0
                    ),
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

    return positions


def _position_windows(positions: list[dict]) -> dict[str, list[dict]]:
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

    return by_symbol


def _assign_closed_pnl(
    by_symbol: dict[str, list[dict]],
    closed_pnl: list[dict],
    owned_order_ids: dict[str, str] | None = None,
    probe_order_ids: frozenset[str] | set[str] | None = None,
) -> None:
    owned_order_ids = owned_order_ids or {}
    probe_order_ids = probe_order_ids or set()
    for row in closed_pnl:
        symbol = str(row.get("symbol") or "").upper()
        side = position_side(str(row.get("side") or ""))
        timestamp = int(row.get("updatedTime") or row.get("createdTime") or 0)
        closer = str(row.get("orderId") or "")
        if closer and closer in probe_order_ids:
            # Untracked probe/rehearsal dust never attributes to an app position.
            continue
        attributed = None
        owner = owned_order_ids.get(closer) if closer else None
        if owner:
            candidates = [
                position
                for position in by_symbol.get(symbol, [])
                if position["intent_id"] == owner and position["side"] == side
            ]
            if len(candidates) == 1:
                attributed = candidates[0]
        if attributed is None:
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
            attributed = matches[0]
        attributed["closed_qty"] += float(row.get("closedSize") or row.get("qty") or 0)
        attributed["closed_at"] = max(attributed["closed_at"], timestamp)
        attributed["closed_pnl"] += float(row.get("closedPnl") or 0)
        attributed["fees"] += float(row.get("openFee") or 0) + float(
            row.get("closeFee") or 0
        )
        attributed["pnl_rows"] += 1


def _assign_funding(by_symbol: dict[str, list[dict]], settlements: list[dict]) -> None:
    for row in settlements:
        if str(row.get("type", "")).upper() != "SETTLEMENT":
            continue
        symbol = str(row.get("symbol") or "").upper()
        timestamp = int(row.get("transactionTime") or 0)
        matches = [
            position
            for position in by_symbol.get(symbol, [])
            if position["start"] <= timestamp < position["end"]
            # A partial close can still accrue funding, including the 1%
            # residual allowed by the report's completion tolerance.
            and (
                position["closed_qty"] < position["entry_qty"]
                or timestamp <= position["closed_at"]
            )
        ]
        if len(matches) == 1:
            matches[0]["funding"] += float(row.get("funding") or 0)


def _finish_positions(positions: list[dict]) -> None:
    for position in positions:
        position["closed_fraction"] = (
            position["closed_qty"] / position["entry_qty"]
            if position["entry_qty"] > 0
            else 0.0
        )
        position["complete"] = (
            position["closed_fraction"] >= 0.99
            # Preserve the underfill allowance, but tolerate only float
            # roundoff above entry fills, never extra closed trades.
            and position["closed_fraction"] <= 1.0 + 1e-9
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


def concentration_summary(positions: list[dict]) -> dict[str, float | int | None]:
    """Show how much realized profit depends on the three largest winners."""
    winners_usdt = sorted(
        (position["net_pnl"] for position in positions if position["net_pnl"] > 0),
        reverse=True,
    )
    winners_r = sorted(
        (position["net_r"] for position in positions if position["net_r"] > 0),
        reverse=True,
    )
    top_three_usdt = sum(winners_usdt[:3])
    top_three_r = sum(winners_r[:3])
    gross_wins_usdt = sum(winners_usdt)
    gross_wins_r = sum(winners_r)

    return {
        "positive_positions": len(winners_usdt),
        "top_three_positive_pnl_usdt": round(top_three_usdt, 8),
        "top_three_share_of_gross_wins_pct": round(
            100 * top_three_usdt / gross_wins_usdt, 4
        )
        if gross_wins_usdt
        else None,
        "net_pnl_ex_top_three_positive_usdt": round(
            sum(position["net_pnl"] for position in positions) - top_three_usdt,
            8,
        ),
        "top_three_positive_r": round(top_three_r, 8),
        "top_three_share_of_gross_winning_r_pct": round(
            100 * top_three_r / gross_wins_r, 4
        )
        if gross_wins_r
        else None,
        "net_r_ex_top_three_positive": round(
            sum(position["net_r"] for position in positions) - top_three_r,
            8,
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

    metric_names = (
        "mean_net_r",
        "mean_net_pnl_usdt",
        "avg_win_loss_ratio",
        "avg_win_loss_ratio_r",
    )
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


def _performance_metrics(
    positions: list[dict],
) -> tuple[float, float, float | None, float | None]:
    winners = [position["net_pnl"] for position in positions if position["net_pnl"] > 0]
    losers = [position["net_pnl"] for position in positions if position["net_pnl"] < 0]
    winner_rs = [position["net_r"] for position in positions if position["net_r"] > 0]
    loser_rs = [position["net_r"] for position in positions if position["net_r"] < 0]
    payoff_ratio = (
        statistics.mean(winners) / abs(statistics.mean(losers))
        if winners and losers
        else None
    )
    payoff_ratio_r = (
        statistics.mean(winner_rs) / abs(statistics.mean(loser_rs))
        if winner_rs and loser_rs
        else None
    )
    return (
        statistics.mean(position["net_r"] for position in positions),
        statistics.mean(position["net_pnl"] for position in positions),
        payoff_ratio,
        payoff_ratio_r,
    )


def _percentile_95_interval(
    values: list[float | None],
) -> tuple[float, float] | None:
    defined = sorted(value for value in values if value is not None)
    if not defined:
        return None
    last = len(defined) - 1
    return defined[int(0.025 * last)], defined[int(0.975 * last)]
