"""Compare saved standard and mirror plans on the same one-minute price path."""

from __future__ import annotations

import argparse
import json
import statistics
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from replay_strategy_v2 import (
    Candidate,
    Result,
    paired_difference_summary,
    replay,
    rows,
)

from cautious_crypto_bro.domain import ExecutionOrderType, ExecutionPlan, Side
from cautious_crypto_bro.mirror_replay import MirrorBar, replay_mirror

MINUTE_MS = 60_000


def standard_replay(
    plan: ExecutionPlan,
    candles: list[dict],
    fee: Decimal,
    funding: dict[int, Decimal],
) -> Result | None:
    primary = plan.orders[0]
    sign = 1 if plan.side is Side.LONG else -1
    reference = primary.reference_price
    for fill_index, candle in enumerate(candles):
        opening = Decimal(candle["open"])
        if primary.order_type is ExecutionOrderType.MARKET:
            entry = opening
        elif sign * (opening - reference) <= 0:
            entry = opening
        elif Decimal(candle["low"]) <= reference <= Decimal(candle["high"]):
            # An intrabar entry leaves the ordering of touches unknown.
            return None
        else:
            continue
        if sign * (entry - plan.stop_loss) <= 0:
            return None
        entry_candles = candles[fill_index:]
        break
    else:
        return Result(candles[0]["startTime"], 0.0, 0.0, False, entry_legs_filled=0)

    policy = plan.policy.strategy_v2
    distance = abs(reference - plan.stop_loss)
    candidate = Candidate(
        weights=(
            float(policy.primary_entry_risk_pct / 100),
            float(policy.secondary_entry_risk_pct / 100),
            float(policy.tertiary_entry_risk_pct / 100),
        ),
        depths=(
            float(abs(plan.orders[1].reference_price - reference) / distance),
            float(abs(plan.orders[2].reference_price - reference) / distance),
        ),
        trail_at=float(policy.trailing_activation_r),
        trail_by=float(policy.trailing_distance_r),
        take_profit_rs=(
            float(policy.first_take_profit_r),
            float(policy.second_take_profit_r),
            float(policy.third_take_profit_r),
        ),
        take_profit_pcts=(
            float(policy.first_take_profit_pct),
            float(policy.second_take_profit_pct),
            float(policy.third_take_profit_pct),
        ),
    )
    case = {
        "side": plan.side.value,
        "start": entry_candles[0]["startTime"],
        "entry": float(entry),
        "stop": float(plan.stop_loss),
        "trader_tp": float(plan.trader_take_profit)
        if plan.trader_take_profit
        else None,
        "candles": [
            {
                "time": c["startTime"],
                **{k: float(c[k]) for k in ("high", "low", "close")},
            }
            for c in entry_candles
        ],
        "events": [
            {
                "kind": "FUNDING",
                "time": c["startTime"],
                "rate": float(-sign * funding[c["startTime"]]),
                "price": float(c["open"]),
            }
            for c in entry_candles[1:]
            if c["startTime"] in funding
        ],
    }
    return replay(case, candidate, float(fee), use_events=True)


def summarize(values: list[Result]) -> dict:
    wins = [r.net_r for r in values if r.net_r > 0]
    losses = [r.net_r for r in values if r.net_r < 0]
    win = statistics.mean(wins) if wins else None
    loss = statistics.mean(losses) if losses else None
    return {
        "expectancy_r": statistics.mean(r.net_r for r in values) if values else None,
        "average_win_r": win,
        "average_loss_r": loss,
        "payoff_ratio": win / -loss if win is not None and loss is not None else None,
    }


def report(bundle: Path, fee: Decimal, slippage: Decimal, horizon: int) -> dict:
    excluded: Counter[str] = Counter()
    pairs: dict[str, list[tuple[Result, Result]]] = defaultdict(list)
    with zipfile.ZipFile(bundle) as archive:
        member = "analysis/mirror_comparisons.jsonl"
        members = [name for name in archive.namelist() if name.endswith(f"/{member}")]
        if len(members) != 1:
            raise ValueError(
                "Expected one mirror comparison file in the forensic bundle"
            )
        root = members[0].removesuffix(f"/{member}")
        comparisons = rows(archive, root, member)
        market = {}
        funding_by_symbol = {}
        for case in sorted(
            comparisons, key=lambda item: datetime.fromisoformat(item["planned_at"])
        ):
            plan_data = case["standard_plan"]
            if plan_data is None or case["mirror_plan"] is None:
                excluded["missing_plan"] += 1
                continue
            standard = ExecutionPlan.model_validate(plan_data)
            if standard.symbol not in market:
                try:
                    market[standard.symbol] = {
                        c["startTime"]: c
                        for c in rows(
                            archive, root, f"market_1m/{standard.symbol}.jsonl"
                        )
                    }
                except KeyError:
                    market[standard.symbol] = {}
                try:
                    funding_by_symbol[standard.symbol] = {
                        int(row["fundingRateTimestamp"]): Decimal(row["fundingRate"])
                        for row in rows(
                            archive, root, f"market_funding/{standard.symbol}.jsonl"
                        )
                    }
                except KeyError:
                    funding_by_symbol[standard.symbol] = None
            try:
                start_ms = int(
                    datetime.fromisoformat(case["planned_at"]).timestamp() * 1_000
                )
                start = (start_ms // MINUTE_MS + 1) * MINUTE_MS
                candles = [
                    market[standard.symbol][start + i * MINUTE_MS]
                    for i in range(horizon)
                ]
            except KeyError:
                excluded["missing_candles"] += 1
                continue
            funding = funding_by_symbol[standard.symbol]
            if funding is None:
                excluded["missing_funding_history"] += 1
                continue
            mirror_plan = ExecutionPlan.model_validate(case["mirror_plan"])
            mirror = replay_mirror(
                standard.side,
                mirror_plan.orders[0].reference_price,
                mirror_plan.take_profit,
                mirror_plan.stop_loss,
                [
                    MirrorBar(
                        Decimal(c["open"]),
                        Decimal(c["high"]),
                        Decimal(c["low"]),
                        funding.get(c["startTime"], Decimal(0)),
                    )
                    for c in candles
                ],
                market_entry=mirror_plan.orders[0].order_type
                is ExecutionOrderType.MARKET,
                entry_fee=fee,
                exit_fee=fee,
                stop_slippage=slippage,
                horizon_close=Decimal(candles[-1]["close"]),
            )
            if mirror.net_r is None and mirror.status != "unfilled":
                excluded[f"mirror_{mirror.status}"] += 1
                continue
            baseline = standard_replay(standard, candles, fee, funding)
            if baseline is None:
                excluded["standard_entry_unresolved"] += 1
                continue
            pairs[standard.side.value].append(
                (
                    baseline,
                    Result(
                        start,
                        float(mirror.net_r or Decimal(0)),
                        0.0,
                        mirror.status == "stop",
                        entry_legs_filled=int(mirror.status != "unfilled"),
                        marked_at_horizon=mirror.status == "open",
                    ),
                )
            )
    return {
        "candidates": len(comparisons),
        "excluded": dict(excluded),
        "scope": (
            "Confirmed no-fills contribute zero R. Open positions are valued at the "
            "final close with exit fees. Standard V2 reconstructs its ladder from the primary fill. "
            "Fees included. Funding uses the settlement minute's opening price "
            "and skips entry bars. Same-minute settlements and exits depend on "
            "the simulators' event ordering. Source follow-ups omitted."
        ),
        "horizon_minutes": horizon,
        "mirror_stop_slippage": str(slippage),
        "by_source_side": {
            side.value: _side_report(pairs[side.value]) for side in Side
        },
    }


def _side_report(pairs: list[tuple[Result, Result]]) -> dict:
    cutoff = len(pairs) * 7 // 10
    partitions = {"all": pairs, "latest_30_percent": pairs[cutoff:]}
    result = {}
    for name, group in partitions.items():
        standard = [pair[0] for pair in group]
        mirror = [pair[1] for pair in group]
        resolved = [
            (a, b)
            for a, b in group
            if a.entry_legs_filled
            and b.entry_legs_filled
            and not a.marked_at_horizon
            and not b.marked_at_horizon
        ]
        result[name] = {
            "standard": summarize(standard),
            "mirror": summarize(mirror),
            "paired_delta": paired_difference_summary(standard, mirror),
            "resolved_only": {
                "standard": summarize([a for a, _ in resolved]),
                "mirror": summarize([b for _, b in resolved]),
                "paired_delta": paired_difference_summary(
                    [a for a, _ in resolved], [b for _, b in resolved]
                ),
            },
            "standard_unfilled": sum(r.entry_legs_filled == 0 for r in standard),
            "mirror_unfilled": sum(r.entry_legs_filled == 0 for r in mirror),
            "standard_marked_at_horizon": sum(r.marked_at_horizon for r in standard),
            "mirror_marked_at_horizon": sum(r.marked_at_horizon for r in mirror),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--fee", type=Decimal, required=True)
    parser.add_argument(
        "--mirror-stop-slippage",
        type=Decimal,
        action="append",
        help="Adverse fraction of the mirror stop fill price. Repeat to compare scenarios.",
    )
    parser.add_argument("--horizon-minutes", type=int, default=1440)
    args = parser.parse_args()
    if args.horizon_minutes <= 0:
        parser.error("--horizon-minutes must be positive")
    slippages = args.mirror_stop_slippage or [Decimal(0)]
    for name, rate in [
        ("--fee", args.fee),
        *(("--mirror-stop-slippage", rate) for rate in slippages),
    ]:
        if not rate.is_finite() or not 0 <= rate < 1:
            parser.error(f"{name} must be a fraction in [0, 1)")
    reports = [
        report(args.bundle, args.fee, rate, args.horizon_minutes)
        for rate in dict.fromkeys(slippages)
    ]
    print(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2))


if __name__ == "__main__":
    main()
