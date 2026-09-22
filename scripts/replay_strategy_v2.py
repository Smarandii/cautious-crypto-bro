from __future__ import annotations

import argparse
import itertools
import json
import statistics
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Candidate:
    weights: tuple[float, float, float]
    depths: tuple[float, float]
    trail_at: float
    trail_by: float


@dataclass
class Result:
    start: int
    net_r: float
    mfe_r: float
    stopped: bool
    lifecycle_actions: int = 0
    funding_r: float = 0.0

    @property
    def roundtrip_loss(self) -> bool:
        return self.mfe_r >= 0.5 and self.net_r <= 0


def rows(
    archive: zipfile.ZipFile,
    root: str,
    name: str,
):
    raw = archive.read(f"{root}/{name}").decode()

    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def fills(items):
    items = [item for item in items if item.get("execType") == "Trade"]

    qty = sum(float(item["execQty"]) for item in items)

    if qty <= 0:
        raise ValueError("No fill quantity")

    price = (
        sum(float(item["execQty"]) * float(item["execPrice"]) for item in items) / qty
    )

    return (
        min(int(item["execTime"]) for item in items),
        price,
    )


def load_cases(
    path: Path,
):
    with zipfile.ZipFile(path) as archive:
        root = archive.namelist()[0].split("/", 1)[0]

        lineage = rows(
            archive,
            root,
            "analysis/trade_lineage.jsonl",
        )

        executions = rows(
            archive,
            root,
            "bybit/executions.jsonl",
        )

        position_actions = rows(
            archive,
            root,
            "database/position_actions.jsonl",
        )

        transaction_log = rows(
            archive,
            root,
            "bybit/transaction_log.jsonl",
        )

        executions_by_order = {}

        for execution in executions:
            if execution.get("execType") != "Trade":
                continue

            order_id = str(execution.get("orderId") or "")

            if not order_id:
                continue

            executions_by_order.setdefault(
                order_id,
                [],
            ).append(execution)

        fee_rates = [
            abs(float(item["feeRate"]))
            for item in executions
            if (item.get("execType") == "Trade" and float(item.get("feeRate") or 0) > 0)
        ]

        fee_rate = statistics.median(fee_rates) if fee_rates else 0.00055

        raw = []

        for source in lineage:
            for item in source["intents"]:
                actual = item.get(
                    "bybit",
                    {},
                ).get(
                    "executions",
                    [],
                )

                if item.get("status") != "EXECUTED":
                    continue

                if not any(
                    execution.get("execType") == "Trade" for execution in actual
                ):
                    continue

                start, entry = fills(actual)

                raw.append(
                    (
                        item,
                        start,
                        entry,
                    )
                )

        raw.sort(key=lambda value: value[1])

        by_symbol = {}

        for index, (
            item,
            start,
            _,
        ) in enumerate(raw):
            symbol = item["intent"]["symbol"].upper()

            by_symbol.setdefault(
                symbol,
                [],
            ).append(
                (
                    index,
                    start,
                )
            )

        end_times = {}

        for (
            symbol,
            values,
        ) in by_symbol.items():
            for offset, (
                index,
                _,
            ) in enumerate(values):
                end_times[
                    (
                        symbol,
                        index,
                    )
                ] = values[offset + 1][1] if (offset + 1 < len(values)) else None

        cases = []

        for index, (
            item,
            start,
            entry,
        ) in enumerate(raw):
            intent = item["intent"]

            symbol = intent["symbol"].upper()

            end = end_times[
                (
                    symbol,
                    index,
                )
            ]

            candle_rows = rows(
                archive,
                root,
                (f"market_1m/{symbol}.jsonl"),
            )

            # The actual entry may happen
            # halfway through a 1-minute candle.
            # Skip that partial candle so replay
            # never uses price movement that
            # happened before the fill.
            first_full_minute = ((start // 60_000) + 1) * 60_000

            candles = [
                {
                    "time": int(row["startTime"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
                for row in candle_rows
                if (
                    int(row["startTime"]) >= first_full_minute
                    and (end is None or int(row["startTime"]) < end)
                )
            ]

            if not candles:
                continue

            events = []

            for action_row in position_actions:
                if action_row.get("status") != "EXECUTED":
                    continue

                try:
                    action = json.loads(action_row["payload_json"])
                except (
                    KeyError,
                    TypeError,
                    json.JSONDecodeError,
                ):
                    continue

                if str(action.get("symbol") or "").upper() != symbol:
                    continue

                order_id = str(action_row.get("bybit_order_id") or "")

                action_fills = [
                    execution
                    for execution in (
                        executions_by_order.get(
                            order_id,
                            [],
                        )
                    )
                    if (
                        int(execution["execTime"]) > start
                        and (end is None or int(execution["execTime"]) < end)
                    )
                ]

                if not action_fills:
                    continue

                action_qty = sum(
                    float(execution["execQty"]) for execution in action_fills
                )

                if action_qty <= 0:
                    continue

                action_price = (
                    sum(
                        float(execution["execQty"]) * float(execution["execPrice"])
                        for execution in action_fills
                    )
                    / action_qty
                )

                events.append(
                    {
                        "time": min(
                            int(execution["execTime"]) for execution in action_fills
                        ),
                        "kind": "LIFECYCLE",
                        "action": action["action"],
                        "close_pct": (
                            float(action["close_pct"])
                            if (action.get("close_pct") is not None)
                            else None
                        ),
                        "price": action_price,
                    }
                )

            for transaction in transaction_log:
                if transaction.get("type") != "SETTLEMENT":
                    continue

                if str(transaction.get("symbol") or "").upper() != symbol:
                    continue

                event_time = int(transaction.get("transactionTime") or 0)

                if event_time <= start or (end is not None and event_time >= end):
                    continue

                historical_qty = abs(
                    float(transaction.get("qty") or transaction.get("size") or 0)
                )

                funding_price = float(transaction.get("tradePrice") or 0)

                funding_cash = float(transaction.get("funding") or 0)

                notional = historical_qty * funding_price

                if notional <= 0 or funding_cash == 0:
                    continue

                events.append(
                    {
                        "time": event_time,
                        "kind": "FUNDING",
                        # Bybit funding is already
                        # signed from the account
                        # perspective. Convert the
                        # historical cash value into
                        # a rate so it scales to the
                        # simulated V2 quantity.
                        "rate": (funding_cash / notional),
                        "price": funding_price,
                    }
                )

            events.sort(
                key=lambda event: (
                    event["time"],
                    event["kind"],
                )
            )

            cases.append(
                {
                    "start": start,
                    "symbol": symbol,
                    "side": (intent["side"]),
                    "entry": entry,
                    "stop": float(intent["stop_loss"]),
                    "trader_tp": (
                        float(intent["take_profit"])
                        if (intent.get("take_profit") is not None)
                        else None
                    ),
                    "candles": candles,
                    "events": events,
                }
            )

    return (
        fee_rate,
        cases,
    )


def side_sign(
    case,
) -> float:
    if case["side"] == "LONG":
        return 1.0

    return -1.0


def r_value(
    case,
    avg: float,
    price: float,
) -> float:
    return side_sign(case) * (price - avg) / abs(avg - case["stop"])


def replay(
    case,
    candidate: Candidate,
    fee_rate: float,
    *,
    use_events: bool,
) -> Result:
    sign = side_sign(case)

    first_entry = case["entry"]

    original_distance = abs(first_entry - case["stop"])

    levels = (
        first_entry,
        (first_entry - sign * candidate.depths[0] * original_distance),
        (first_entry - sign * candidate.depths[1] * original_distance),
    )

    legs = []

    for index, (
        price,
        risk_weight,
    ) in enumerate(
        zip(
            levels,
            candidate.weights,
            strict=True,
        )
    ):
        distance = abs(price - case["stop"])

        if distance <= 0:
            raise ValueError("Entry crossed stop")

        # Normalize the strategy to
        # a total risk budget of 1R.
        #
        # qty × stop distance
        # = leg risk allocation.
        legs.append(
            {
                "price": price,
                "qty": (risk_weight / distance),
                "filled": (index == 0),
            }
        )

    qty = legs[0]["qty"]
    avg = first_entry

    # Entry fee is included immediately.
    realized = -(qty * avg * fee_rate)

    frozen = False

    tp_prices = []
    tp_qty = []

    trailing = False
    trail_distance = 0.0
    trail_price = 0.0
    trail_peak = 0.0

    mfe = 0.0

    event_index = 0
    lifecycle_actions = 0
    funding_r = 0.0

    def close(
        amount: float,
        price: float,
    ) -> None:
        nonlocal qty
        nonlocal realized

        amount = min(
            amount,
            qty,
        )

        if amount <= 0:
            return

        realized += amount * sign * (price - avg) - amount * price * fee_rate

        qty = max(
            0.0,
            qty - amount,
        )

    def freeze() -> None:
        nonlocal frozen
        nonlocal tp_prices
        nonlocal tp_qty

        if frozen:
            return

        frozen = True

        target_rs = [
            0.5,
            1.0,
            1.5,
        ]

        trader_tp = case["trader_tp"]

        if trader_tp is not None:
            trader_r = r_value(
                case,
                avg,
                trader_tp,
            )

            # V2 keeps universal
            # de-risking even when the
            # trader supplies a TP.
            if 0.5 <= trader_r < 1.5:
                target_rs = [
                    0.5,
                    (0.5 + trader_r) / 2,
                    trader_r,
                ]

        tp_prices = [
            (avg + sign * target_r * abs(avg - case["stop"])) for target_r in target_rs
        ]

        # 25% TP1
        # 25% TP2
        # 25% TP3
        # 25% runner
        tp_qty = [
            qty * 0.25,
            qty * 0.25,
            qty * 0.25,
        ]

    for candle in case["candles"]:
        adverse = candle["low"] if (case["side"] == "LONG") else candle["high"]

        favorable = candle["high"] if (case["side"] == "LONG") else candle["low"]

        # One-minute OHLC does not reveal
        # intraminute ordering.
        #
        # Use adverse-first ordering to
        # avoid optimistic backtesting.
        if not frozen:
            for leg in legs[1:]:
                if leg["filled"]:
                    continue

                touched = (
                    adverse <= leg["price"]
                    if (case["side"] == "LONG")
                    else adverse >= leg["price"]
                )

                if not touched:
                    continue

                old_qty = qty

                qty += leg["qty"]

                avg = ((avg * old_qty) + (leg["price"] * leg["qty"])) / qty

                realized -= leg["qty"] * leg["price"] * fee_rate

                leg["filled"] = True

        protection = case["stop"]

        reason = "STOP"

        if trailing:
            if case["side"] == "LONG" and trail_price > protection:
                protection = trail_price

                reason = "TRAIL"

            elif case["side"] == "SHORT" and trail_price < protection:
                protection = trail_price

                reason = "TRAIL"

        protection_hit = (
            adverse <= protection if (case["side"] == "LONG") else adverse >= protection
        )

        if protection_hit:
            close(
                qty,
                protection,
            )

            return Result(
                start=case["start"],
                net_r=realized,
                mfe_r=mfe,
                stopped=(reason == "STOP"),
                lifecycle_actions=(lifecycle_actions),
                funding_r=funding_r,
            )

        # The candle does not reveal whether
        # its high or low happened first.
        # Protection therefore wins before
        # lifecycle/favorable processing.
        #
        # This deliberately biases ambiguous
        # candles against the strategy.
        candle_end = candle["time"] + 60_000

        if use_events:
            while (
                event_index < len(case["events"])
                and case["events"][event_index]["time"] < candle_end
            ):
                event = case["events"][event_index]

                event_index += 1

                if qty <= 0:
                    continue

                if event["kind"] == "FUNDING":
                    payment = event["rate"] * qty * event["price"]

                    realized += payment
                    funding_r += payment
                    continue

                freeze()

                if event["action"] == "CLOSE":
                    close(
                        qty,
                        event["price"],
                    )

                    lifecycle_actions += 1

                    return Result(
                        start=case["start"],
                        net_r=realized,
                        mfe_r=mfe,
                        stopped=False,
                        lifecycle_actions=(lifecycle_actions),
                        funding_r=funding_r,
                    )

                if event["action"] == "REDUCE" and event["close_pct"] is not None:
                    quantity_before = qty

                    close(
                        quantity_before * event["close_pct"] / 100,
                        event["price"],
                    )

                    lifecycle_actions += 1

                    # V2 rebuilds remaining
                    # exit quantities after a
                    # REDUCE. Preserve the
                    # relative allocation of
                    # every still-pending TP
                    # and the runner.
                    if quantity_before > 0 and qty > 0:
                        scale = qty / quantity_before

                        tp_qty = [amount * scale for amount in tp_qty]

        current_r = r_value(
            case,
            avg,
            favorable,
        )

        mfe = max(
            mfe,
            current_r,
        )

        # Once a trade reaches either
        # TP1 or trailing activation,
        # pending scale-ins are frozen.
        if not frozen and current_r >= min(
            0.5,
            candidate.trail_at,
        ):
            freeze()

        if frozen:
            for index, target in enumerate(tp_prices):
                if tp_qty[index] <= 0:
                    continue

                touched = (
                    favorable >= target
                    if (case["side"] == "LONG")
                    else favorable <= target
                )

                if not touched:
                    continue

                close(
                    tp_qty[index],
                    target,
                )

                tp_qty[index] = 0.0

        if qty <= 0:
            return Result(
                start=case["start"],
                net_r=realized,
                mfe_r=mfe,
                stopped=False,
                lifecycle_actions=(lifecycle_actions),
                funding_r=funding_r,
            )

        if current_r >= candidate.trail_at:
            if not trailing:
                freeze()

                base_distance = candidate.trail_by * abs(avg - case["stop"])

                # Solve the initial trail floor
                # so an immediate trailing exit
                # still leaves at least +0.05R
                # after the estimated exit fee.
                if case["side"] == "LONG":
                    required_floor = (0.05 - realized + qty * avg) / (
                        qty * (1 - fee_rate)
                    )

                    trail_distance = min(
                        base_distance,
                        max(
                            0.0,
                            favorable - required_floor,
                        ),
                    )

                    trail_price = favorable - trail_distance

                else:
                    required_floor = (qty * avg + realized - 0.05) / (
                        qty * (1 + fee_rate)
                    )

                    trail_distance = min(
                        base_distance,
                        max(
                            0.0,
                            required_floor - favorable,
                        ),
                    )

                    trail_price = favorable + trail_distance

                trail_peak = favorable
                trailing = True

            elif case["side"] == "LONG" and favorable > trail_peak:
                trail_peak = favorable

                trail_price = favorable - trail_distance

            elif case["side"] == "SHORT" and favorable < trail_peak:
                trail_peak = favorable

                trail_price = favorable + trail_distance

    # Keep still-open historical
    # positions marked to market at
    # the forensic snapshot boundary.
    close(
        qty,
        case["candles"][-1]["close"],
    )

    return Result(
        start=case["start"],
        net_r=realized,
        mfe_r=mfe,
        stopped=False,
        lifecycle_actions=(lifecycle_actions),
        funding_r=funding_r,
    )


def candidates():
    return (
        Candidate(
            weights=weights,
            depths=depths,
            trail_at=trail_at,
            trail_by=trail_by,
        )
        for (
            weights,
            depths,
            trail_at,
            trail_by,
        ) in itertools.product(
            (
                (
                    0.70,
                    0.20,
                    0.10,
                ),
                (
                    0.65,
                    0.20,
                    0.15,
                ),
                (
                    0.60,
                    0.25,
                    0.15,
                ),
            ),
            (
                (
                    0.20,
                    0.40,
                ),
                (
                    0.25,
                    0.50,
                ),
                (
                    0.33,
                    0.66,
                ),
            ),
            (
                0.40,
                0.50,
                0.60,
                0.75,
            ),
            (
                0.30,
                0.40,
                0.45,
                0.50,
            ),
        )
    )


def max_drawdown(
    results,
) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0

    for result in sorted(
        results,
        key=lambda item: item.start,
    ):
        equity += result.net_r

        peak = max(
            peak,
            equity,
        )

        worst = max(
            worst,
            peak - equity,
        )

    return worst


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay deterministic Strategy V2 rules against a CCB forensic bundle."
        )
    )

    parser.add_argument(
        "bundle",
        type=Path,
    )

    parser.add_argument(
        "--top",
        type=int,
        default=15,
    )

    parser.add_argument(
        "--autonomous",
        action="store_true",
        help=("Ignore historical REDUCE/CLOSE and funding events."),
    )

    args = parser.parse_args()

    fee_rate, cases = load_cases(args.bundle)

    ranked = []

    for candidate in candidates():
        results = [
            replay(
                case,
                candidate,
                fee_rate,
                use_events=(not args.autonomous),
            )
            for case in cases
        ]

        roundtrip = sum(result.roundtrip_loss for result in results)

        stops = sum(result.stopped for result in results)

        drawdown = max_drawdown(results)

        giveback = statistics.mean(
            max(
                0.0,
                (result.mfe_r - result.net_r),
            )
            for result in results
        )

        net_r = sum(result.net_r for result in results)

        lifecycle_actions = sum(result.lifecycle_actions for result in results)

        funding_r = sum(result.funding_r for result in results)

        # Loss prevention intentionally
        # dominates historical return.
        score = (
            roundtrip,
            stops,
            drawdown,
            giveback,
            -net_r,
        )

        ranked.append(
            (
                score,
                candidate,
                {
                    "roundtrip": roundtrip,
                    "stops": stops,
                    "drawdown": drawdown,
                    "giveback": giveback,
                    "net_r": net_r,
                    "lifecycle_actions": (lifecycle_actions),
                    "funding_r": funding_r,
                },
            )
        )

    ranked.sort(key=lambda item: item[0])

    historical_lifecycle_actions = sum(
        event["kind"] == "LIFECYCLE" for case in cases for event in case["events"]
    )

    funding_events = sum(
        event["kind"] == "FUNDING" for case in cases for event in case["events"]
    )

    print("mode=" + ("autonomous" if args.autonomous else "historical-lifecycle"))

    print(f"filled_cases={len(cases)}")

    print(f"median_fee_rate={fee_rate:.8f}")

    print(f"historical_lifecycle_actions={historical_lifecycle_actions}")

    print(f"funding_events={funding_events}")

    print()

    for index, (
        _,
        candidate,
        result,
    ) in enumerate(
        ranked[: args.top],
        start=1,
    ):
        print(
            f"{index:2d}. "
            f"roundtrip="
            f"{result['roundtrip']} "
            f"stops="
            f"{result['stops']} "
            f"maxDD="
            f"{result['drawdown']:.3f}R "
            f"giveback="
            f"{result['giveback']:.3f}R "
            f"net="
            f"{result['net_r']:.3f}R "
            f"actions="
            f"{result['lifecycle_actions']} "
            f"funding="
            f"{result['funding_r']:+.3f}R "
            f"weights="
            f"{candidate.weights} "
            f"depths="
            f"{candidate.depths} "
            f"trail="
            f"{candidate.trail_at}/"
            f"{candidate.trail_by}R"
        )


if __name__ == "__main__":
    main()
