from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import math
import random
import statistics
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class Candidate:
    weights: tuple[float, float, float]
    depths: tuple[float, float]
    trail_at: float
    trail_by: float
    take_profit_rs: tuple[float, float, float] = (0.5, 1.0, 1.5)
    take_profit_pcts: tuple[float, float, float] = (25.0, 25.0, 25.0)


@dataclass
class Result:
    start: int
    net_r: float
    mfe_r: float
    stopped: bool
    lifecycle_actions: int = 0
    funding_r: float = 0.0
    entry_legs_filled: int = 1

    @property
    def roundtrip_loss(self) -> bool:
        return self.mfe_r >= 0.5 and self.net_r <= 0


@dataclass(frozen=True)
class TimeStop:
    after_minutes: int
    max_close_r: float
    only_before_trail_activation: bool = False

    def __post_init__(self) -> None:
        if self.after_minutes < 1:
            raise ValueError("Time stop age must be positive")
        if not math.isfinite(self.max_close_r):
            raise ValueError("Time stop R threshold must be finite")


@dataclass(frozen=True)
class RiskGeometry:
    stop_distance_multiplier: float = 1.0
    entry_grid_multiplier: float = 1.0

    def __post_init__(self) -> None:
        for name, multiplier in (
            ("Stop distance", self.stop_distance_multiplier),
            ("Entry grid", self.entry_grid_multiplier),
        ):
            if not math.isfinite(multiplier) or multiplier <= 0:
                raise ValueError(f"{name} multiplier must be positive and finite")


LATE_TARGETS_REDUCED_E3_FROZEN_AFTER = "2026-10-07T15:53:00+00:00"
EXIT_ONLY_FROZEN_AFTER = "2026-10-07T16:28:40+00:00"
STOP_GRID_FROZEN_AFTER = "2026-10-07T18:13:30+00:00"
EARLY_TRAIL_FROZEN_AFTER = "2026-10-07T18:45:48+00:00"
DEPTH_EXIT_FROZEN_AFTER = "2026-10-07T19:10:00+00:00"
DEMO_TRAIL_SHADOW_FROZEN_AFTER = "2026-10-08T17:32:00+00:00"
PROSPECTIVE_SHADOW_REVIEW_CASES = 20


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
    first_fill_time = min(int(item["execTime"]) for item in items)
    first_fill_items = [
        item for item in items if int(item["execTime"]) == first_fill_time
    ]
    first_fill_qty = sum(float(item["execQty"]) for item in first_fill_items)
    first_fill_price = (
        sum(
            float(item["execQty"]) * float(item["execPrice"])
            for item in first_fill_items
        )
        / first_fill_qty
    )

    return (
        first_fill_time,
        price,
        first_fill_price,
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
        maker_rates = [
            abs(float(item["feeRate"]))
            for item in executions
            if (
                item.get("execType") == "Trade"
                and item.get("isMaker") is True
                and float(item.get("feeRate") or 0) > 0
            )
        ]
        taker_rates = [
            abs(float(item["feeRate"]))
            for item in executions
            if (
                item.get("execType") == "Trade"
                and item.get("isMaker") is False
                and float(item.get("feeRate") or 0) > 0
            )
        ]
        maker_fee_rate = statistics.median(maker_rates) if maker_rates else fee_rate
        taker_fee_rate = statistics.median(taker_rates) if taker_rates else fee_rate

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

                start, entry, first_fill_entry = fills(actual)

                raw.append(
                    (
                        item,
                        start,
                        entry,
                        first_fill_entry,
                    )
                )

        raw.sort(key=lambda value: value[1])

        by_symbol = {}

        for index, (
            item,
            start,
            _,
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
            first_fill_entry,
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

            filled_entry_legs = set()
            for order_id in item.get("entry_order_ids", []):
                for execution in executions_by_order.get(str(order_id), []):
                    if (
                        execution.get("execType") == "Trade"
                        and float(execution.get("closedSize") or 0) == 0
                    ):
                        leg_name = (
                            str(execution.get("orderLinkId") or "")
                            .rsplit("-", 1)[-1]
                            .upper()
                        )
                        if leg_name in {"E1", "E2", "E3"}:
                            filled_entry_legs.add(leg_name)

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
                    "intent_id": item.get("intent_id", ""),
                    "plan_created_at": item.get("created_at"),
                    "observed_fill_pattern": "+".join(
                        leg for leg in ("E1", "E2", "E3") if leg in filled_entry_legs
                    )
                    or "unknown",
                    "symbol": symbol,
                    "side": (intent["side"]),
                    "entry": entry,
                    "first_fill_entry": first_fill_entry,
                    "risk_per_trade_pct": item.get("metadata", {}).get(
                        "risk_per_trade_pct"
                    ),
                    "exit_profile": item.get("metadata", {}).get("exit_profile"),
                    "primary_entry_maker": any(
                        execution.get("isMaker") is True
                        for order_id in item.get("entry_order_ids", [])
                        for execution in executions_by_order.get(str(order_id), [])
                        if (
                            execution.get("execType") == "Trade"
                            and str(execution.get("orderLinkId") or "")
                            .rsplit("-", 1)[-1]
                            .upper()
                            == "E1"
                        )
                    ),
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
        maker_fee_rate,
        taker_fee_rate,
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
    e3_loss_cap_r: float | None = None,
    e3_risk_trim_cap_r: float | None = None,
    e3_min_delay_minutes: int = 0,
    e3_reclaim_e2: bool = False,
    time_stop: TimeStop | None = None,
    risk_geometry: RiskGeometry | None = None,
    trail_activation_on_close: bool = False,
    maker_fee_rate: float | None = None,
    taker_fee_rate: float | None = None,
) -> Result:
    if e3_min_delay_minutes < 0:
        raise ValueError("E3 minimum delay cannot be negative")
    if e3_loss_cap_r is not None and e3_risk_trim_cap_r is not None:
        raise ValueError("E3 stop cap and risk trim cannot be combined")
    if e3_risk_trim_cap_r is not None and (
        not math.isfinite(e3_risk_trim_cap_r) or e3_risk_trim_cap_r <= 0
    ):
        raise ValueError("E3 risk trim cap must be positive and finite")
    geometry = risk_geometry or RiskGeometry()
    maker_fee_rate = fee_rate if maker_fee_rate is None else maker_fee_rate
    taker_fee_rate = fee_rate if taker_fee_rate is None else taker_fee_rate
    if any(
        not math.isfinite(rate) or rate < 0 for rate in (maker_fee_rate, taker_fee_rate)
    ):
        raise ValueError("Fee rates must be finite and non-negative")

    sign = side_sign(case)

    first_entry = case["entry"]

    planned_stop_distance = abs(first_entry - case["stop"])
    effective_stop = (
        first_entry - sign * planned_stop_distance * geometry.stop_distance_multiplier
    )
    replay_case = {**case, "stop": effective_stop}

    levels = (
        first_entry,
        (
            first_entry
            - sign
            * candidate.depths[0]
            * planned_stop_distance
            * geometry.entry_grid_multiplier
        ),
        (
            first_entry
            - sign
            * candidate.depths[1]
            * planned_stop_distance
            * geometry.entry_grid_multiplier
        ),
    )
    if any(sign * (price - effective_stop) <= 0 for price in levels):
        raise ValueError("Entry level crossed effective stop")

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
        distance = abs(price - effective_stop)

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
                "risk_weight": risk_weight,
                "filled": (index == 0),
            }
        )

    qty = legs[0]["qty"]
    avg = first_entry
    protective_stop = effective_stop

    # Entry fee is included immediately.
    primary_fee_rate = (
        maker_fee_rate if case.get("primary_entry_maker", False) else taker_fee_rate
    )
    realized = -(qty * avg * primary_fee_rate)

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
    e3_touched_at: int | None = None
    e3_filled_at: int | None = None
    e3_risk_trim_fill_at: int | None = None

    def make_result(*, stopped: bool = False) -> Result:
        return Result(
            start=case["start"],
            net_r=realized,
            mfe_r=mfe,
            stopped=stopped,
            lifecycle_actions=lifecycle_actions,
            funding_r=funding_r,
            entry_legs_filled=sum(leg["filled"] for leg in legs),
        )

    def close(
        amount: float,
        price: float,
        close_fee_rate: float = taker_fee_rate,
    ) -> None:
        nonlocal qty
        nonlocal realized

        amount = min(
            amount,
            qty,
        )

        if amount <= 0:
            return

        realized += amount * sign * (price - avg) - amount * price * close_fee_rate

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

        target_rs = resolve_target_rs(avg)

        tp_prices = [
            avg + sign * target_r * abs(avg - replay_case["stop"])
            for target_r in target_rs
        ]

        tp_qty = [qty * percentage / 100 for percentage in candidate.take_profit_pcts]

    def resolve_target_rs(entry_price: float) -> list[float]:
        target_rs = list(candidate.take_profit_rs)

        trader_tp = case["trader_tp"]

        if trader_tp is not None:
            trader_r = r_value(
                replay_case,
                entry_price,
                trader_tp,
            )

            # V2 keeps universal
            # de-risking even when the
            # trader supplies a TP.
            if 0.5 <= trader_r < target_rs[-1]:
                if trader_r > target_rs[0]:
                    target_rs = [
                        target_rs[0],
                        (target_rs[0] + trader_r) / 2,
                        trader_r,
                    ]
                else:
                    target_rs = [
                        target_r * trader_r / target_rs[-1] for target_r in target_rs
                    ]

        return target_rs

    for candle in case["candles"]:
        adverse = candle["low"] if (case["side"] == "LONG") else candle["high"]

        favorable = candle["high"] if (case["side"] == "LONG") else candle["low"]

        # One-minute OHLC does not reveal
        # intraminute ordering.
        #
        # Use adverse-first ordering to
        # avoid optimistic backtesting.
        if not frozen:
            for leg_index, leg in enumerate(legs[1:], start=1):
                if leg["filled"]:
                    continue
                if (
                    leg_index == 2
                    and candle["time"] < case["start"] + e3_min_delay_minutes * 60_000
                ):
                    continue

                touched = (
                    adverse <= leg["price"]
                    if (case["side"] == "LONG")
                    else adverse >= leg["price"]
                )

                fill_price = leg["price"]
                fill_qty = leg["qty"]

                if leg_index == 2 and e3_reclaim_e2:
                    if e3_touched_at is None:
                        if touched:
                            e3_touched_at = candle["time"]
                        continue

                    # Require a later candle to confirm recovery through E2.
                    # Its close is the simulated taker fill, not the old E3 limit.
                    if candle["time"] <= e3_touched_at:
                        continue
                    reclaimed = (
                        candle["close"] >= levels[1]
                        if case["side"] == "LONG"
                        else candle["close"] <= levels[1]
                    )
                    if not reclaimed:
                        continue
                    fill_price = candle["close"]
                    distance = abs(fill_price - effective_stop)
                    if distance <= 0:
                        continue
                    fill_qty = leg["risk_weight"] / distance
                    e3_filled_at = candle["time"]
                elif not touched:
                    continue

                if not touched:
                    if not (
                        leg_index == 2 and e3_reclaim_e2 and e3_filled_at is not None
                    ):
                        continue

                old_qty = qty

                qty += fill_qty

                avg = ((avg * old_qty) + (fill_price * fill_qty)) / qty

                fill_fee_rate = (
                    taker_fee_rate
                    if leg_index == 2
                    and e3_reclaim_e2
                    and e3_filled_at == candle["time"]
                    else maker_fee_rate
                )
                realized -= fill_qty * fill_price * fill_fee_rate

                leg["filled"] = True

                if leg_index == 2 and e3_risk_trim_cap_r is not None:
                    e3_risk_trim_fill_at = candle["time"]

                if leg_index == 2 and e3_loss_cap_r is not None:
                    cap_stop = (-e3_loss_cap_r - realized + qty * sign * avg) / (
                        qty * (sign - taker_fee_rate)
                    )
                    if case["side"] == "LONG":
                        protective_stop = max(protective_stop, cap_stop)
                    else:
                        protective_stop = min(protective_stop, cap_stop)

        protection = protective_stop

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

            return make_result(stopped=(reason == "STOP"))

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

                    return make_result()

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

        if e3_risk_trim_fill_at == candle["time"] and e3_risk_trim_cap_r is not None:
            stop_out_per_qty = (
                sign * (protective_stop - avg) - protective_stop * taker_fee_rate
            )
            close_per_qty = sign * (candle["close"] - avg) - (
                candle["close"] * taker_fee_rate
            )
            projected_stop_pnl = realized + qty * stop_out_per_qty
            improvement_per_qty = close_per_qty - stop_out_per_qty
            trimmed = False
            if projected_stop_pnl < -e3_risk_trim_cap_r and improvement_per_qty > 0:
                trim_qty = min(
                    qty,
                    (-e3_risk_trim_cap_r - projected_stop_pnl) / improvement_per_qty,
                )
                close(trim_qty, candle["close"])
                trimmed = trim_qty > 0
            if trimmed:
                continue

        # The recovery close is the entry price; earlier movement in that
        # candle must not trigger a post-entry target or trailing stop.
        if e3_filled_at == candle["time"]:
            continue

        current_r = r_value(
            replay_case,
            avg,
            favorable,
        )

        activation_price = candle["close"] if trail_activation_on_close else favorable
        activation_r = r_value(replay_case, avg, activation_price)

        mfe = max(
            mfe,
            current_r,
        )

        # Once a trade reaches either
        # TP1 or trailing activation,
        # pending scale-ins are frozen.
        first_target_r = resolve_target_rs(avg)[0]

        if not frozen and (
            current_r >= first_target_r or activation_r >= candidate.trail_at
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
                    maker_fee_rate,
                )

                tp_qty[index] = 0.0

        if qty <= 0:
            return make_result()

        if (
            time_stop is not None
            and candle_end - case["start"] >= time_stop.after_minutes * 60_000
            and r_value(replay_case, avg, candle["close"]) <= time_stop.max_close_r
            and (not time_stop.only_before_trail_activation or mfe < candidate.trail_at)
        ):
            close(qty, candle["close"])
            return make_result()

        if activation_r >= candidate.trail_at:
            if not trailing:
                freeze()

                base_distance = candidate.trail_by * abs(avg - effective_stop)
                trail_anchor = activation_price

                # Solve the initial trail floor
                # so an immediate trailing exit
                # still leaves at least +0.05R
                # after the estimated exit fee.
                if case["side"] == "LONG":
                    required_floor = (0.05 - realized + qty * avg) / (
                        qty * (1 - taker_fee_rate)
                    )

                    trail_distance = min(
                        base_distance,
                        max(
                            0.0,
                            trail_anchor - required_floor,
                        ),
                    )

                    trail_price = trail_anchor - trail_distance

                else:
                    required_floor = (qty * avg + realized - 0.05) / (
                        qty * (1 + taker_fee_rate)
                    )

                    trail_distance = min(
                        base_distance,
                        max(
                            0.0,
                            required_floor - trail_anchor,
                        ),
                    )

                    trail_price = trail_anchor + trail_distance

                trail_peak = trail_anchor
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

    return make_result()


def _valid_trailing_pair(trail_at: float, trail_by: float) -> bool:
    activation = Decimal(str(trail_at))
    distance = Decimal(str(trail_by))
    return distance < activation and activation - distance >= Decimal("0.05")


def _candidate_grid(entry_weights):
    exit_profiles = (
        ((0.5, 1.0, 1.5), (25.0, 25.0, 25.0)),
        ((0.75, 1.5, 2.5), (20.0, 20.0, 20.0)),
        ((1.0, 2.0, 3.0), (20.0, 20.0, 20.0)),
        ((1.0, 2.0, 4.0), (15.0, 20.0, 25.0)),
    )

    return (
        Candidate(
            weights=weights,
            depths=depths,
            trail_at=trail_at,
            trail_by=trail_by,
            take_profit_rs=take_profit_rs,
            take_profit_pcts=take_profit_pcts,
        )
        for (
            weights,
            depths,
            trail_at,
            trail_by,
            (take_profit_rs, take_profit_pcts),
        ) in itertools.product(
            entry_weights,
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
            exit_profiles,
        )
        if _valid_trailing_pair(trail_at, trail_by)
    )


def candidates():
    """Policy-valid candidates with all three positive entry allocations."""
    return _candidate_grid(
        (
            (0.80, 0.15, 0.05),
            (0.75, 0.20, 0.05),
            (0.75, 0.15, 0.10),
            (0.70, 0.25, 0.05),
            (0.65, 0.25, 0.10),
            (0.70, 0.20, 0.10),
            (0.65, 0.20, 0.15),
            (0.60, 0.25, 0.15),
        )
    )


def no_e3_counterfactuals():
    """Counterfactual-only candidates; current V2 policy rejects zero E3 risk."""
    return _candidate_grid(
        (
            (0.80, 0.20, 0.00),
            (0.75, 0.25, 0.00),
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


def summarize_results(results) -> dict[str, float | int | None]:
    wins = [result.net_r for result in results if result.net_r > 0]
    losses = [result.net_r for result in results if result.net_r < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "count": len(results),
        "net_r": sum(result.net_r for result in results),
        "expectancy_r": statistics.mean(result.net_r for result in results)
        if results
        else None,
        "win_rate": len(wins) / len(results) if results else None,
        "avg_win_r": statistics.mean(wins) if wins else None,
        "avg_loss_r": statistics.mean(losses) if losses else None,
        "profit_factor": (
            gross_profit / gross_loss if gross_loss else float("inf") if wins else None
        ),
        "max_drawdown_r": max_drawdown(results),
    }


def summarize_exit_excursion(
    results: list[Result],
    *,
    trail_activation_r: float,
) -> dict[str, float | int]:
    """Separate losing paths that never activated the trail from exit giveback."""
    if trail_activation_r <= 0:
        raise ValueError("Trail activation must be positive")
    losing_results = [result for result in results if result.net_r <= 0]
    reached_activation = sum(
        result.mfe_r >= trail_activation_r for result in losing_results
    )
    return {
        "mean_mfe_r": statistics.mean(result.mfe_r for result in results)
        if results
        else 0.0,
        "losing_cases": len(losing_results),
        "losing_cases_below_trail_activation": len(losing_results) - reached_activation,
        "losing_cases_reaching_trail_activation": reached_activation,
    }


def format_optional_r(value: float | None) -> str:
    """Format an unavailable R statistic without aborting a diagnostic."""
    return "n/a" if value is None else f"{value:.3f}R"


def summarize_by_side(cases, results):
    """Summarize replay outcomes per direction without changing ranking."""
    return {
        side: summarize_results(
            [
                result
                for case, result in zip(cases, results, strict=True)
                if case["side"] == side
            ]
        )
        for side in ("LONG", "SHORT")
    }


def paired_difference_summary(
    baseline_results,
    candidate_results,
    *,
    bootstrap_iterations: int = 10_000,
    seed: int = 20261007,
) -> dict[str, float | int | tuple[float, float] | None]:
    """Summarize paired R changes with IID and circular 4-case bootstraps."""
    if len(baseline_results) != len(candidate_results):
        raise ValueError("Paired replay result sets must have equal lengths")
    if bootstrap_iterations < 1:
        raise ValueError("Bootstrap iterations must be positive")

    differences = [
        candidate.net_r - baseline.net_r
        for baseline, candidate in zip(
            baseline_results,
            candidate_results,
            strict=True,
        )
    ]
    if not differences:
        return {
            "count": 0,
            "improved_cases": 0,
            "delta_net_r": 0.0,
            "mean_delta_r": 0.0,
            "top_3_positive_case_delta_r": 0.0,
            "delta_net_r_excluding_top_3_positive_cases": 0.0,
            "iid_bootstrap_95ci_delta_net_r": None,
            "circular_block_4_bootstrap_95ci_delta_net_r": None,
        }

    top_positive_delta_sum, adjusted_delta = concentration_adjusted_delta(differences)
    rng = random.Random(seed)
    bootstrapped_sums = sorted(
        sum(rng.choices(differences, k=len(differences)))
        for _ in range(bootstrap_iterations)
    )
    last = len(bootstrapped_sums) - 1
    block_rng = random.Random(seed + 1)
    block_sums = []
    for _ in range(bootstrap_iterations):
        sample = []
        while len(sample) < len(differences):
            start = block_rng.randrange(len(differences))
            sample.extend(
                differences[(start + offset) % len(differences)] for offset in range(4)
            )
        block_sums.append(sum(sample[: len(differences)]))
    block_sums.sort()
    block_last = len(block_sums) - 1
    return {
        "count": len(differences),
        "improved_cases": sum(value > 0 for value in differences),
        "delta_net_r": sum(differences),
        "mean_delta_r": statistics.mean(differences),
        "top_3_positive_case_delta_r": top_positive_delta_sum,
        "delta_net_r_excluding_top_3_positive_cases": adjusted_delta,
        "iid_bootstrap_95ci_delta_net_r": (
            bootstrapped_sums[int(0.025 * last)],
            bootstrapped_sums[int(0.975 * last)],
        ),
        "circular_block_4_bootstrap_95ci_delta_net_r": (
            block_sums[int(0.025 * block_last)],
            block_sums[int(0.975 * block_last)],
        ),
    }


def concentration_adjusted_delta(differences: list[float]) -> tuple[float, float]:
    """Return top-three positive contribution and remaining paired delta."""
    top_three_positive = sum(
        sorted(
            (difference for difference in differences if difference > 0), reverse=True
        )[:3]
    )
    return top_three_positive, sum(differences) - top_three_positive


def late_target_reduced_e3_candidate() -> Candidate:
    """Frozen research challenger surfaced by the 2026-10-07 replay search."""
    return Candidate(
        weights=(0.70, 0.25, 0.05),
        depths=(0.33, 0.66),
        trail_at=0.40,
        trail_by=0.30,
        take_profit_rs=(1.0, 2.0, 4.0),
        take_profit_pcts=(15.0, 20.0, 25.0),
    )


def exit_only_candidate() -> Candidate:
    """Frozen current-sizing challenger from the 2026-10-07 exit-profile sweep."""
    return Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.33, 0.66),
        trail_at=0.40,
        trail_by=0.30,
        take_profit_rs=(1.0, 2.0, 4.0),
        take_profit_pcts=(15.0, 20.0, 25.0),
    )


def live_demo_payoff_exit_candidate() -> Candidate:
    """Mirror the original 2026-10-08 Demo payoff profile at 0.40R activation."""
    return Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.33, 0.66),
        trail_at=0.40,
        trail_by=0.10,
        take_profit_rs=(1.0, 2.0, 4.0),
        take_profit_pcts=(15.0, 20.0, 25.0),
    )


def live_demo_early_trail_candidate() -> Candidate:
    """Mirror the separately tagged 0.20R Demo early-trail experiment."""
    return Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.33, 0.66),
        trail_at=0.20,
        trail_by=0.10,
        take_profit_rs=(1.0, 2.0, 4.0),
        take_profit_pcts=(15.0, 20.0, 25.0),
    )


def live_demo_payoff_exit_reduced_e3_candidate() -> Candidate:
    """Pair the live payoff exits with the researched 5% E3 risk allocation."""
    return Candidate(
        weights=(0.70, 0.25, 0.05),
        depths=(0.33, 0.66),
        trail_at=0.40,
        trail_by=0.10,
        take_profit_rs=(1.0, 2.0, 4.0),
        take_profit_pcts=(15.0, 20.0, 25.0),
    )


def early_trail_candidate() -> Candidate:
    """Frozen current-sizing, current-target challenger with an earlier trail."""
    baseline = current_policy()
    return Candidate(
        weights=baseline.weights,
        depths=baseline.depths,
        trail_at=0.20,
        trail_by=0.10,
        take_profit_rs=baseline.take_profit_rs,
        take_profit_pcts=baseline.take_profit_pcts,
    )


def live_demo_tight_trail_candidate() -> Candidate:
    """Mirror the original 0.20R/0.05R Demo tight-trail profile."""
    return replace(live_demo_early_trail_candidate(), trail_by=0.05)


def live_demo_long_015_candidate() -> Candidate:
    """Mirror the 0.15R LONG activation challenger at the tight trail width."""
    return replace(live_demo_tight_trail_candidate(), trail_at=0.15)


def depth_exit_candidate() -> Candidate:
    """Freeze the training-selected shallower-grid and later-target profile."""
    return Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.25, 0.50),
        trail_at=0.40,
        trail_by=0.30,
        take_profit_rs=(1.0, 2.0, 4.0),
        take_profit_pcts=(15.0, 20.0, 25.0),
    )


def completed_position_starts(bundle_path: Path | BinaryIO) -> set[int]:
    """Load entry timestamps whose actual closed size reconciles in the bundle."""
    analyzer_path = Path(__file__).with_name("analyze_forensic_pnl.py")
    spec = importlib.util.spec_from_file_location(
        "forensic_pnl_for_replay",
        analyzer_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load forensic P&L analyzer: {analyzer_path}")
    analyzer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(analyzer)
    return {
        int(position["start"])
        for position in analyzer.reconcile(bundle_path)
        if position["complete"]
    }


def prospective_shadow_cases(
    cases,
    completed_starts: set[int],
    *,
    frozen_after: str = LATE_TARGETS_REDUCED_E3_FROZEN_AFTER,
) -> list[dict]:
    """Keep only reconciled positions opened after the challenger was frozen."""
    cutoff = datetime.fromisoformat(frozen_after).astimezone(UTC)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    return [
        case
        for case in cases
        if case["start"] > cutoff_ms and case["start"] in completed_starts
    ]


def reconciled_position_cases(cases, completed_starts: set[int]) -> list[dict]:
    """Keep only positions whose actual closed size reconciles to entry fills."""
    return [case for case in cases if case["start"] in completed_starts]


def report_prospective_shadow(cases, fee_rate: float) -> None:
    """Compare frozen candidate to current policy on future, closed positions."""
    report_prospective_candidate_shadow(
        cases,
        fee_rate,
        challenger=late_target_reduced_e3_candidate(),
        frozen_after=LATE_TARGETS_REDUCED_E3_FROZEN_AFTER,
    )


def report_prospective_exit_shadow(cases, fee_rate: float) -> None:
    """Compare the frozen exit-only candidate on future, reconciled trades."""
    report_prospective_candidate_shadow(
        cases,
        fee_rate,
        challenger=exit_only_candidate(),
        frozen_after=EXIT_ONLY_FROZEN_AFTER,
    )


def report_prospective_early_trail_shadow(cases, fee_rate: float) -> None:
    """Track the frozen 0.20R/0.10R trail on later reconciled positions."""
    report_prospective_candidate_shadow(
        cases,
        fee_rate,
        challenger=early_trail_candidate(),
        frozen_after=EARLY_TRAIL_FROZEN_AFTER,
    )


def report_prospective_depth_exit_shadow(cases, fee_rate: float) -> None:
    """Track the frozen shallower-grid, later-target profile prospectively."""
    report_prospective_candidate_shadow(
        cases,
        fee_rate,
        challenger=depth_exit_candidate(),
        frozen_after=DEPTH_EXIT_FROZEN_AFTER,
    )


def report_prospective_demo_trail_shadow(cases, fee_rate: float) -> None:
    """Compare a later trail trigger with the exact active Demo exit profile."""
    report_prospective_candidate_shadow(
        cases,
        fee_rate,
        baseline=live_demo_early_trail_candidate(),
        challenger=live_demo_payoff_exit_candidate(),
        frozen_after=DEMO_TRAIL_SHADOW_FROZEN_AFTER,
    )


def report_prospective_stop_distance_shadow(cases, fee_rate: float) -> None:
    """Track the training-selected doubled stop and entry grid prospectively."""
    report_prospective_candidate_shadow(
        cases,
        fee_rate,
        challenger=current_policy(),
        frozen_after=STOP_GRID_FROZEN_AFTER,
        risk_geometry=RiskGeometry(2.0, 2.0),
    )


def report_prospective_candidate_shadow(
    cases,
    fee_rate: float,
    *,
    challenger: Candidate,
    frozen_after: str,
    baseline: Candidate | None = None,
    risk_geometry: RiskGeometry | None = None,
) -> None:
    """Compare a frozen offline candidate to current policy on future cases."""
    print(
        "prospective_shadow_note=offline replay only; actual policy and orders are "
        "unchanged; candidate fills are simulated, not executed"
    )
    print(f"prospective_shadow_frozen_after={frozen_after}")
    if risk_geometry is not None:
        print(f"prospective_shadow_risk_geometry={risk_geometry}")
    print(f"prospective_shadow_cases={len(cases)}")
    status = (
        "ready_for_review"
        if len(cases) >= PROSPECTIVE_SHADOW_REVIEW_CASES
        else "collecting"
    )
    print(
        f"prospective_shadow_status={status}/target:{PROSPECTIVE_SHADOW_REVIEW_CASES}"
    )
    if not cases:
        return

    baseline = baseline or current_policy()
    for side in ("ALL", "LONG", "SHORT"):
        selected = [case for case in cases if side == "ALL" or case["side"] == side]
        baseline_results = [
            replay(case, baseline, fee_rate, use_events=True) for case in selected
        ]
        challenger_results = [
            replay(
                case,
                challenger,
                fee_rate,
                use_events=True,
                risk_geometry=risk_geometry,
            )
            for case in selected
        ]
        print(
            f"prospective_shadow_metrics={side}/current",
            json.dumps(summarize_results(baseline_results), sort_keys=True),
        )
        print(
            f"prospective_shadow_metrics={side}/challenger",
            json.dumps(summarize_results(challenger_results), sort_keys=True),
        )
        print(
            f"prospective_shadow_paired_delta={side}",
            json.dumps(
                paired_difference_summary(baseline_results, challenger_results),
                sort_keys=True,
            ),
        )


def compare_challenger(cases, fee_rate: float, *, use_events: bool) -> None:
    """Compare a frozen offline challenger against current policy on paired cases."""
    baseline = current_policy()
    challenger = late_target_reduced_e3_candidate()
    print(
        "challenger_note=post-hoc research candidate selected after reviewing this "
        "bundle's results; only a later unseen bundle can provide prospective evidence"
    )
    print(
        "paired_bootstrap_note=IID over paired per-case R differences; assumes "
        "independent cases and ignores time clustering"
    )
    print(f"challenger_policy={challenger}")
    for split in ("train", "validation", "holdout"):
        selected = [case for case in cases if case.get("split") == split]
        baseline_results = [
            replay(case, baseline, fee_rate, use_events=use_events) for case in selected
        ]
        challenger_results = [
            replay(case, challenger, fee_rate, use_events=use_events)
            for case in selected
        ]
        for side in ("ALL", "LONG", "SHORT"):
            paired = [
                (base, test)
                for case, base, test in zip(
                    selected,
                    baseline_results,
                    challenger_results,
                    strict=True,
                )
                if side == "ALL" or case["side"] == side
            ]
            base_results = [base for base, _ in paired]
            test_results = [test for _, test in paired]
            print(
                f"challenger_metrics={split}/{side}/current",
                json.dumps(summarize_results(base_results), sort_keys=True),
            )
            print(
                f"challenger_metrics={split}/{side}/candidate",
                json.dumps(summarize_results(test_results), sort_keys=True),
            )
            print(
                f"challenger_paired_delta={split}/{side}",
                json.dumps(
                    paired_difference_summary(base_results, test_results),
                    sort_keys=True,
                ),
            )


def current_policy() -> Candidate:
    return Candidate(
        weights=(0.60, 0.25, 0.15),
        depths=(0.33, 0.66),
        trail_at=0.50,
        trail_by=0.30,
    )


def report_fee_schedule_diagnostics(cases, fee_rate, maker_fee_rate, taker_fee_rate):
    """Compare scalar fees with observed maker/taker fees; research only."""
    print(
        "fee_schedule_note=diagnostic only; E1 defaults to taker unless observed "
        "maker, E2/E3 and TP limits use maker, market exits use taker; no live policy change"
    )
    print(
        f"fee_schedule_rates=scalar:{fee_rate:.8f},maker:{maker_fee_rate:.8f},"
        f"taker:{taker_fee_rate:.8f}"
    )
    ordered = sorted(cases, key=lambda case: case["start"])
    validation_start = int(len(ordered) * 0.5)
    holdout_start = int(len(ordered) * 0.7)
    partitions = {
        "train": ordered[:validation_start],
        "validation": ordered[validation_start:holdout_start],
        "holdout": ordered[holdout_start:],
    }
    policy_results = {}
    for name, candidate in (
        ("current", current_policy()),
        ("late_targets_reduced_e3", late_target_reduced_e3_candidate()),
    ):
        for partition, selected in partitions.items():
            scalar = [
                replay(case, candidate, fee_rate, use_events=True) for case in selected
            ]
            split = [
                replay(
                    case,
                    candidate,
                    fee_rate,
                    use_events=True,
                    maker_fee_rate=maker_fee_rate,
                    taker_fee_rate=taker_fee_rate,
                )
                for case in selected
            ]
            policy_results[(name, partition, "scalar")] = scalar
            policy_results[(name, partition, "maker_taker")] = split
            print(
                f"fee_schedule_metrics={name}/{partition}/scalar",
                json.dumps(summarize_results(scalar), sort_keys=True),
            )
            print(
                f"fee_schedule_metrics={name}/{partition}/maker_taker",
                json.dumps(summarize_results(split), sort_keys=True),
            )
            print(
                f"fee_schedule_delta={name}/{partition}/maker_taker_minus_scalar",
                json.dumps(paired_difference_summary(scalar, split), sort_keys=True),
            )
    for partition in partitions:
        for schedule in ("scalar", "maker_taker"):
            print(
                f"fee_schedule_policy_delta={partition}/{schedule}/"
                "late_targets_reduced_e3_minus_current",
                json.dumps(
                    paired_difference_summary(
                        policy_results[("current", partition, schedule)],
                        policy_results[
                            ("late_targets_reduced_e3", partition, schedule)
                        ],
                    ),
                    sort_keys=True,
                ),
            )


def report_time_stop_diagnostics(
    partitions,
    candidate: Candidate,
    fee_rate: float,
    *,
    use_events: bool,
) -> None:
    """Evaluate a small, fixed time-stop grid without changing live policy."""
    profiles = tuple(
        TimeStop(after_minutes, max_close_r)
        for after_minutes in (60, 240, 720, 1_440)
        for max_close_r in (0.0, -0.25, -0.50)
    )
    baseline_by_split = {
        split: [
            replay(case, candidate, fee_rate, use_events=use_events)
            for case in split_cases
        ]
        for split, split_cases in partitions.items()
    }
    ranked_training_profiles = []

    print(
        "time_stop_note=research only; exit remaining quantity at the 1m close "
        "only after the age threshold when close R is at or below the threshold; "
        "multiple tested thresholds are exploratory, not independent validation"
    )
    for profile in profiles:
        results_by_split = {
            split: [
                replay(
                    case,
                    candidate,
                    fee_rate,
                    use_events=use_events,
                    time_stop=profile,
                )
                for case in split_cases
            ]
            for split, split_cases in partitions.items()
        }
        training_summary = summarize_results(results_by_split["train"])
        ranked_training_profiles.append(
            (training_summary["net_r"], -training_summary["max_drawdown_r"], profile)
        )

        for split, results in results_by_split.items():
            summary = summarize_results(results)
            paired = paired_difference_summary(
                baseline_by_split[split],
                results,
                bootstrap_iterations=2_000,
            )
            interval = paired["iid_bootstrap_95ci_delta_net_r"]
            interval_text = (
                "n/a"
                if interval is None
                else f"[{interval[0]:+.3f},{interval[1]:+.3f}]"
            )
            print(
                f"time_stop_profile={profile.after_minutes}m/"
                f"close<={profile.max_close_r:+.2f}R/{split} "
                f"net:{summary['net_r']:+.3f}R/"
                f"PF{summary['profit_factor']:.2f}/"
                f"WR{summary['win_rate']:.0%}/"
                f"DD{summary['max_drawdown_r']:.3f}R/"
                f"paired_delta:{paired['delta_net_r']:+.3f}R/"
                f"CI{interval_text}/n{summary['count']}"
            )

    selected_profile = max(
        ranked_training_profiles,
        key=lambda row: (
            row[0],
            row[1],
            -row[2].after_minutes,
            row[2].max_close_r,
        ),
    )
    profile = selected_profile[2]
    print(
        "time_stop_selected_by_train="
        f"{profile.after_minutes}m/close<={profile.max_close_r:+.2f}R; "
        "selection is in-sample and not a production recommendation"
    )


def report_untriggered_time_stop_diagnostics(
    partitions,
    candidate: Candidate,
    fee_rate: float,
    maker_fee_rate: float,
    taker_fee_rate: float,
) -> None:
    """Test close-confirmed loss cuts only before the favorable trail activates."""
    baseline_by_split = {
        split: [
            replay(
                case,
                candidate,
                fee_rate,
                use_events=False,
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
            )
            for case in split_cases
        ]
        for split, split_cases in partitions.items()
    }
    print(
        "untriggered_time_stop_note=research only; exits remaining size at the "
        "1m close only after age and adverse-close thresholds, and only if prior "
        "favorable excursion is still below the 0.20R trail trigger; OHLC path "
        "ordering and retrospective selection remain limitations"
    )
    for after_minutes in (60, 240, 720):
        for max_close_r in (-0.25, -0.50, -0.75):
            profile = TimeStop(
                after_minutes,
                max_close_r,
                only_before_trail_activation=True,
            )
            for split, split_cases in partitions.items():
                results = [
                    replay(
                        case,
                        candidate,
                        fee_rate,
                        use_events=False,
                        time_stop=profile,
                        maker_fee_rate=maker_fee_rate,
                        taker_fee_rate=taker_fee_rate,
                    )
                    for case in split_cases
                ]
                paired = paired_difference_summary(
                    baseline_by_split[split],
                    results,
                    bootstrap_iterations=2_000,
                )
                summary = summarize_results(results)
                interval = paired["iid_bootstrap_95ci_delta_net_r"]
                print(
                    f"untriggered_time_stop={after_minutes}m/"
                    f"close<={max_close_r:+.2f}R/{split} "
                    f"net:{summary['net_r']:+.3f}R/"
                    f"delta:{paired['delta_net_r']:+.3f}R/"
                    f"CI{interval}/n{summary['count']}"
                )


def report_trail_activation_poll_diagnostics(
    partitions,
    candidate: Candidate,
    fee_rate: float,
    maker_fee_rate: float,
    taker_fee_rate: float,
) -> None:
    """Bound sensitivity to supervisor-polled trail activation with 1m bars."""
    print(
        "trail_activation_poll_note=research-only sensitivity; baseline activates "
        "on intrabar high/low, challenger requires the 1m close to confirm the "
        "threshold and anchors the initial trail at that close; the live 2s "
        "supervisor may activate intraminute, so this is conservative, not an "
        "exact runtime replay; observed fills/fees retained"
    )
    for split, cases in partitions.items():
        intrabar = [
            replay(
                case,
                candidate,
                fee_rate,
                use_events=True,
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
            )
            for case in cases
        ]
        close_confirmed = [
            replay(
                case,
                candidate,
                fee_rate,
                use_events=True,
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
                trail_activation_on_close=True,
            )
            for case in cases
        ]
        baseline_summary = summarize_results(intrabar)
        close_summary = summarize_results(close_confirmed)
        paired = paired_difference_summary(
            intrabar,
            close_confirmed,
            bootstrap_iterations=2_000,
        )
        print(
            f"trail_activation_poll={split} n={len(cases)} "
            f"intrabar_net={baseline_summary['net_r']:+.3f}R "
            f"close_confirmed_net={close_summary['net_r']:+.3f}R "
            f"close_minus_intrabar={paired['delta_net_r']:+.3f}R "
            f"iid95={paired['iid_bootstrap_95ci_delta_net_r']} "
            f"block4_95={paired['circular_block_4_bootstrap_95ci_delta_net_r']}"
        )


def demo_target_sweep_candidates() -> tuple[tuple[str, Candidate], ...]:
    """Sweep targets against the original 0.20R/0.05R tight-trail profile."""
    baseline = live_demo_tight_trail_candidate()
    return (
        ("active_1_2_4R", baseline),
        ("targets_1.25_2.5_5R", replace(baseline, take_profit_rs=(1.25, 2.5, 5.0))),
        ("targets_1.5_3_6R", replace(baseline, take_profit_rs=(1.5, 3.0, 6.0))),
        ("targets_2_4_8R", replace(baseline, take_profit_rs=(2.0, 4.0, 8.0))),
    )


def demo_runner_allocation_candidates() -> tuple[tuple[str, Candidate], ...]:
    """Sweep scale-outs against the original 0.20R/0.05R tight-trail profile."""
    baseline = live_demo_tight_trail_candidate()
    allocations = (
        ("runner_30pct", (20.0, 25.0, 25.0)),
        ("active_runner_40pct", (15.0, 20.0, 25.0)),
        ("runner_50pct", (10.0, 15.0, 25.0)),
        ("runner_60pct", (10.0, 10.0, 20.0)),
        ("runner_70pct", (5.0, 10.0, 15.0)),
    )
    return tuple(
        (
            name,
            replace(baseline, take_profit_pcts=take_profit_pcts),
        )
        for name, take_profit_pcts in allocations
    )


def demo_trail_activation_candidates() -> tuple[tuple[str, Candidate], ...]:
    """Sweep activation from the original 0.20R/0.05R tight-trail profile."""
    baseline = live_demo_tight_trail_candidate()
    return tuple(
        (f"activation_{activation:.2f}R", replace(baseline, trail_at=activation))
        for activation in (0.10, 0.15, 0.20, 0.25, 0.30)
    )


def report_demo_trail_activation_diagnostics(
    partitions,
    fee_rate: float,
    maker_fee_rate: float,
    taker_fee_rate: float,
) -> None:
    """Compare activation alternatives against the original 0.20R profile."""
    profiles = demo_trail_activation_candidates()
    baseline = profiles[2][1]
    print(
        "demo_trail_activation_note=research only; changes activation threshold "
        "only; preserves 0.05R trail distance, target levels/fractions, entry "
        "risk and fills/fees; no live policy change"
    )
    for split, cases in partitions.items():
        for side in ("ALL", "LONG", "SHORT"):
            selected = [case for case in cases if side == "ALL" or case["side"] == side]
            baseline_results = [
                replay(
                    case,
                    baseline,
                    fee_rate,
                    use_events=True,
                    maker_fee_rate=maker_fee_rate,
                    taker_fee_rate=taker_fee_rate,
                )
                for case in selected
            ]
            baseline_excursion = summarize_exit_excursion(
                baseline_results,
                trail_activation_r=baseline.trail_at,
            )
            for profile_name, candidate in profiles:
                if candidate.trail_at == baseline.trail_at:
                    continue
                candidate_results = [
                    replay(
                        case,
                        candidate,
                        fee_rate,
                        use_events=True,
                        maker_fee_rate=maker_fee_rate,
                        taker_fee_rate=taker_fee_rate,
                    )
                    for case in selected
                ]
                paired = paired_difference_summary(
                    baseline_results,
                    candidate_results,
                    bootstrap_iterations=2_000,
                )
                candidate_excursion = summarize_exit_excursion(
                    candidate_results,
                    trail_activation_r=candidate.trail_at,
                )
                print(
                    f"demo_trail_activation={profile_name}/{split}/{side} "
                    f"baseline={json.dumps(summarize_results(baseline_results), sort_keys=True)} "
                    f"baseline_excursion={json.dumps(baseline_excursion, sort_keys=True)} "
                    f"candidate={json.dumps(summarize_results(candidate_results), sort_keys=True)} "
                    f"candidate_excursion={json.dumps(candidate_excursion, sort_keys=True)} "
                    f"paired_delta={json.dumps(paired, sort_keys=True)}"
                )


def report_demo_runner_allocation_diagnostics(
    partitions,
    fee_rate: float,
    maker_fee_rate: float,
    taker_fee_rate: float,
) -> None:
    """Compare runner sizes against the original tight-trail profile."""
    profiles = demo_runner_allocation_candidates()
    baseline = profiles[1][1]
    print(
        "demo_runner_allocation_note=research only; changes TP scale-out "
        "fractions only; runner is the unallocated remainder; preserves original "
        "0.20R/0.05R trail, target distances, entry sizing, and observed fees; "
        "small historical sample, no production change"
    )
    for split, cases in partitions.items():
        for side in ("ALL", "LONG", "SHORT"):
            selected = [case for case in cases if side == "ALL" or case["side"] == side]
            baseline_results = [
                replay(
                    case,
                    baseline,
                    fee_rate,
                    use_events=True,
                    maker_fee_rate=maker_fee_rate,
                    taker_fee_rate=taker_fee_rate,
                )
                for case in selected
            ]
            for profile_name, candidate in profiles:
                if profile_name == "active_runner_40pct":
                    continue
                candidate_results = [
                    replay(
                        case,
                        candidate,
                        fee_rate,
                        use_events=True,
                        maker_fee_rate=maker_fee_rate,
                        taker_fee_rate=taker_fee_rate,
                    )
                    for case in selected
                ]
                paired = paired_difference_summary(
                    baseline_results,
                    candidate_results,
                    bootstrap_iterations=2_000,
                )
                print(
                    f"demo_runner_allocation={profile_name}/{split}/{side} "
                    f"baseline={json.dumps(summarize_results(baseline_results), sort_keys=True)} "
                    f"candidate={json.dumps(summarize_results(candidate_results), sort_keys=True)} "
                    f"paired_delta={json.dumps(paired, sort_keys=True)}"
                )


def report_demo_target_sweep_diagnostics(
    partitions,
    fee_rate: float,
    maker_fee_rate: float,
    taker_fee_rate: float,
) -> None:
    """Compare farther targets with the original tight-trail profile."""
    print(
        "demo_target_sweep_note=research only; changes target distances only; "
        "preserves 0.60/0.25/0.15 entry risk, 0.33/0.66 entries, 0.20/0.05 "
        "trail, target fractions, and observed fills/fees; targets are a small "
        "predeclared set, but historical validation/holdout have been inspected; "
        "no production policy change"
    )
    profiles = demo_target_sweep_candidates()
    baseline = profiles[0][1]
    for split, cases in partitions.items():
        for side in ("ALL", "LONG", "SHORT"):
            selected = [case for case in cases if side == "ALL" or case["side"] == side]
            baseline_results = [
                replay(
                    case,
                    baseline,
                    fee_rate,
                    use_events=True,
                    maker_fee_rate=maker_fee_rate,
                    taker_fee_rate=taker_fee_rate,
                )
                for case in selected
            ]
            for profile_name, candidate in profiles[1:]:
                candidate_results = [
                    replay(
                        case,
                        candidate,
                        fee_rate,
                        use_events=True,
                        maker_fee_rate=maker_fee_rate,
                        taker_fee_rate=taker_fee_rate,
                    )
                    for case in selected
                ]
                baseline_summary = summarize_results(baseline_results)
                candidate_summary = summarize_results(candidate_results)
                paired = paired_difference_summary(
                    baseline_results,
                    candidate_results,
                    bootstrap_iterations=2_000,
                )
                print(
                    f"demo_target_sweep={profile_name}/{split}/{side} "
                    f"baseline={json.dumps(baseline_summary, sort_keys=True)} "
                    f"candidate={json.dumps(candidate_summary, sort_keys=True)} "
                    f"paired_delta={json.dumps(paired, sort_keys=True)}"
                )


def demo_trail_distance_candidates() -> tuple[tuple[str, Candidate], ...]:
    """Sweep trail distance against the original 0.20R activation profile."""
    baseline = live_demo_tight_trail_candidate()
    return (
        ("active_0.05R", baseline),
        ("trail_0.10R", replace(baseline, trail_by=0.10)),
        ("trail_0.15R", replace(baseline, trail_by=0.15)),
    )


def no_e3_candidate(candidate: Candidate) -> Candidate:
    """Reallocate E3 risk across E1/E2 while preserving their relative weights."""
    e1_e2_weight = sum(candidate.weights[:2])
    if e1_e2_weight <= 0:
        raise ValueError("E1 and E2 must have positive combined risk weight")
    return replace(
        candidate,
        weights=(
            candidate.weights[0] / e1_e2_weight,
            candidate.weights[1] / e1_e2_weight,
            0.0,
        ),
    )


def report_e3_allocation_diagnostics(
    partitions,
    fee_rate: float,
    maker_fee_rate: float,
    taker_fee_rate: float,
) -> None:
    """Compare E3 allocation with the original tight-trail geometry."""
    baseline = live_demo_tight_trail_candidate()
    candidate = no_e3_candidate(baseline)
    print(
        "e3_allocation_note=research only; preserves exit geometry, planned total "
        "risk, and E1:E2 risk ratio; sets E3 allocation to zero; replay does not "
        "model order rejection or liquidity effects"
    )
    print(
        f"e3_allocation_weights=baseline:{baseline.weights}/no_e3:{candidate.weights}"
    )
    for split, cases in partitions.items():
        for side in ("ALL", "LONG", "SHORT"):
            selected = [case for case in cases if side == "ALL" or case["side"] == side]
            baseline_results = [
                replay(
                    case,
                    baseline,
                    fee_rate,
                    use_events=True,
                    maker_fee_rate=maker_fee_rate,
                    taker_fee_rate=taker_fee_rate,
                )
                for case in selected
            ]
            candidate_results = [
                replay(
                    case,
                    candidate,
                    fee_rate,
                    use_events=True,
                    maker_fee_rate=maker_fee_rate,
                    taker_fee_rate=taker_fee_rate,
                )
                for case in selected
            ]
            paired = paired_difference_summary(
                baseline_results,
                candidate_results,
                bootstrap_iterations=2_000,
            )
            print(
                f"e3_allocation={split}/{side} "
                f"baseline={json.dumps(summarize_results(baseline_results), sort_keys=True)} "
                f"no_e3={json.dumps(summarize_results(candidate_results), sort_keys=True)} "
                f"paired_delta={json.dumps(paired, sort_keys=True)}"
            )


def report_demo_trail_distance_diagnostics(
    partitions,
    fee_rate: float,
    maker_fee_rate: float,
    taker_fee_rate: float,
) -> None:
    """Compare trail widths with the original tight-trail profile."""
    print(
        "demo_trail_distance_note=research only; changes trailing distance only; "
        "preserves 0.60/0.25/0.15 entry risk, 0.33/0.66 entries, 0.20R "
        "activation, 1/2/4R targets, target fractions, and observed fills/fees; "
        "historical validation/holdout have been inspected; no production change"
    )
    profiles = demo_trail_distance_candidates()
    baseline = profiles[0][1]
    for split, cases in partitions.items():
        for side in ("ALL", "LONG", "SHORT"):
            selected = [case for case in cases if side == "ALL" or case["side"] == side]
            baseline_results = [
                replay(
                    case,
                    baseline,
                    fee_rate,
                    use_events=True,
                    maker_fee_rate=maker_fee_rate,
                    taker_fee_rate=taker_fee_rate,
                )
                for case in selected
            ]
            for profile_name, candidate in profiles[1:]:
                candidate_results = [
                    replay(
                        case,
                        candidate,
                        fee_rate,
                        use_events=True,
                        maker_fee_rate=maker_fee_rate,
                        taker_fee_rate=taker_fee_rate,
                    )
                    for case in selected
                ]
                baseline_summary = summarize_results(baseline_results)
                candidate_summary = summarize_results(candidate_results)
                baseline_excursion = summarize_exit_excursion(
                    baseline_results,
                    trail_activation_r=baseline.trail_at,
                )
                candidate_excursion = summarize_exit_excursion(
                    candidate_results,
                    trail_activation_r=candidate.trail_at,
                )
                paired = paired_difference_summary(
                    baseline_results,
                    candidate_results,
                    bootstrap_iterations=2_000,
                )
                print(
                    f"demo_trail_distance={profile_name}/{split}/{side} "
                    f"baseline={json.dumps(baseline_summary, sort_keys=True)} "
                    f"baseline_excursion={json.dumps(baseline_excursion, sort_keys=True)} "
                    f"candidate={json.dumps(candidate_summary, sort_keys=True)} "
                    f"candidate_excursion={json.dumps(candidate_excursion, sort_keys=True)} "
                    f"paired_delta={json.dumps(paired, sort_keys=True)}"
                )


def report_stop_distance_diagnostics(
    partitions,
    candidate: Candidate,
    fee_rate: float,
    *,
    use_events: bool,
) -> None:
    """Separate stop-width effects from entry-grid changes at fixed nominal risk."""
    profiles = [("baseline", RiskGeometry())]
    profiles.extend(
        (
            f"stop_only/{multiplier:.2f}x",
            RiskGeometry(
                stop_distance_multiplier=multiplier,
                entry_grid_multiplier=1.0,
            ),
        )
        for multiplier in (0.75, 1.25, 1.50, 2.0)
    )
    profiles.extend(
        (
            f"stop_and_grid/{multiplier:.2f}x",
            RiskGeometry(
                stop_distance_multiplier=multiplier,
                entry_grid_multiplier=multiplier,
            ),
        )
        for multiplier in (0.75, 1.25, 1.50, 2.0)
    )
    baseline_by_split = {
        split: [
            replay(case, candidate, fee_rate, use_events=use_events)
            for case in split_cases
        ]
        for split, split_cases in partitions.items()
    }
    ranked_training_profiles = []

    print(
        "stop_distance_note=research only; stop_only keeps E2/E3 levels fixed, "
        "stop_and_grid scales both; each entry leg is resized to preserve its "
        "share of nominal 1R; fill/slippage changes and liquidation constraints "
        "are not modeled"
    )
    ranked_training_profiles = []
    for profile_name, geometry in profiles:
        results_by_split = {
            split: [
                replay(
                    case,
                    candidate,
                    fee_rate,
                    use_events=use_events,
                    risk_geometry=geometry,
                )
                for case in split_cases
            ]
            for split, split_cases in partitions.items()
        }
        training_summary = summarize_results(results_by_split["train"])
        ranked_training_profiles.append(
            (
                training_summary["net_r"],
                -training_summary["max_drawdown_r"],
                -(
                    abs(geometry.stop_distance_multiplier - 1.0)
                    + abs(geometry.entry_grid_multiplier - 1.0)
                ),
                profile_name,
                geometry,
            )
        )

        for split, results in results_by_split.items():
            summary = summarize_results(results)
            paired = paired_difference_summary(
                baseline_by_split[split],
                results,
                bootstrap_iterations=2_000,
            )
            interval = paired["iid_bootstrap_95ci_delta_net_r"]
            interval_text = (
                "n/a"
                if interval is None
                else f"[{interval[0]:+.3f},{interval[1]:+.3f}]"
            )
            print(
                f"stop_distance_profile={profile_name}/"
                f"stop={geometry.stop_distance_multiplier:.2f}x/"
                f"grid={geometry.entry_grid_multiplier:.2f}x/{split} "
                f"net:{summary['net_r']:+.3f}R/"
                f"PF{summary['profit_factor']:.2f}/"
                f"WR{summary['win_rate']:.0%}/"
                f"avg_win:{summary['avg_win_r']:.3f}R/"
                f"avg_loss:{format_optional_r(summary['avg_loss_r'])}/"
                f"paired_delta:{paired['delta_net_r']:+.3f}R/"
                f"CI{interval_text}/n{summary['count']}"
            )
            fill_mix = summarize_simulated_fill_mix(results)
            print(
                f"simulated_fill_mix={profile_name}/{split} "
                + " ".join(
                    f"{label}:n{fill_mix[legs]['count']}/"
                    f"{fill_mix[legs]['net_r']:+.3f}R"
                    for legs, label in (
                        (1, "E1"),
                        (2, "E1+E2"),
                        (3, "E1+E2+E3"),
                    )
                )
            )

    selected_profile = max(
        ranked_training_profiles,
        key=lambda row: (row[0], row[1], row[2], row[3]),
    )
    print(
        "stop_distance_selected_by_train="
        f"{selected_profile[3]}/"
        f"stop={selected_profile[4].stop_distance_multiplier:.2f}x/"
        f"grid={selected_profile[4].entry_grid_multiplier:.2f}x; "
        "selection is in-sample and not a production recommendation"
    )


def summarize_simulated_fill_mix(results):
    """Aggregate outcomes by the number of simulated entry legs filled."""
    return {
        entry_legs: summarize_results(
            [result for result in results if result.entry_legs_filled == entry_legs]
        )
        for entry_legs in (1, 2, 3)
    }


def observed_fill_diagnostics(
    cases,
    candidate: Candidate,
    fee_rate: float,
    *,
    use_events: bool,
) -> dict[str, dict[str, dict[str, float | int]]]:
    results = [
        (
            case,
            replay(case, candidate, fee_rate, use_events=use_events),
        )
        for case in cases
    ]
    patterns = sorted(
        {case.get("observed_fill_pattern", "unknown") for case, _ in results}
        - {"unknown"}
    )
    diagnostics = {}
    for pattern in patterns:
        cohort = [
            (case, result)
            for case, result in results
            if case.get("observed_fill_pattern", "unknown") == pattern
        ]
        diagnostics[pattern] = {
            "all": summarize_results([result for _, result in cohort]),
            "holdout": summarize_results(
                [result for case, result in cohort if case.get("split") == "holdout"]
            ),
        }
    return diagnostics


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
        "--reconciled-only",
        action="store_true",
        help=(
            "Exclude open and size-unreconciled positions from historical "
            "candidate search and diagnostics."
        ),
    )

    parser.add_argument(
        "--rank-by-concentration-adjusted-train",
        action="store_true",
        help=(
            "Rank historical candidates by training paired delta after removing "
            "the three largest positive case contributions."
        ),
    )

    parser.add_argument(
        "--autonomous",
        action="store_true",
        help=("Ignore historical REDUCE/CLOSE and funding events."),
    )

    parser.add_argument(
        "--validation-fraction",
        type=float,
        default=0.20,
        help="Chronological middle segment reported separately from training.",
    )

    parser.add_argument(
        "--holdout-fraction",
        type=float,
        default=0.30,
        help="Latest untouched chronological segment used for final comparison.",
    )

    parser.add_argument(
        "--e3-cap-diagnostics-only",
        action="store_true",
        help="Compare current exits with post-E3 stop-cap counterfactuals only.",
    )

    parser.add_argument(
        "--e3-risk-trim-diagnostics-only",
        action="store_true",
        help=(
            "Research only: trim after E3 at the same-minute close to cap projected "
            "stop loss; does not change live policy."
        ),
    )

    parser.add_argument(
        "--e3-delay-diagnostics-only",
        action="store_true",
        help=(
            "Compare current and frozen-challenger policies when E3 activation "
            "is delayed; research only."
        ),
    )

    parser.add_argument(
        "--e3-reclaim-diagnostics-only",
        action="store_true",
        help=(
            "Research only: compare baseline with E3 entered at candle close "
            "after a later reclaim through E2."
        ),
    )

    parser.add_argument(
        "--time-stop-diagnostics-only",
        action="store_true",
        help=(
            "Research only: evaluate close-confirmed time stops on chronological "
            "splits; does not change live policy."
        ),
    )

    parser.add_argument(
        "--untriggered-time-stop-diagnostics-only",
        action="store_true",
        help=(
            "Research only: evaluate close-confirmed loss cuts before trail "
            "activation under the live Demo early-trail profile."
        ),
    )

    parser.add_argument(
        "--trail-activation-poll-diagnostics-only",
        action="store_true",
        help=(
            "Research only: compare intrabar trail activation with conservative "
            "1m-close-confirmed activation under the live Demo profile."
        ),
    )

    parser.add_argument(
        "--demo-target-sweep-diagnostics-only",
        action="store_true",
        help=(
            "Research only: compare farther take-profit levels against the "
            "original 0.20R/0.05R profile without changing other settings."
        ),
    )

    parser.add_argument(
        "--demo-runner-allocation-diagnostics-only",
        action="store_true",
        help=(
            "Research only: compare take-profit scale-out fractions with the "
            "remainder held as runner under the original tight-trail profile."
        ),
    )

    parser.add_argument(
        "--demo-trail-activation-diagnostics-only",
        action="store_true",
        help=(
            "Research only: compare trail activation thresholds at the active "
            "0.05R trail distance."
        ),
    )

    parser.add_argument(
        "--demo-trail-distance-diagnostics-only",
        action="store_true",
        help=(
            "Research only: compare trailing distances under the original "
            "0.20R/0.05R profile without changing other settings."
        ),
    )

    parser.add_argument(
        "--e3-allocation-diagnostics-only",
        action="store_true",
        help=(
            "Research only: reallocate E3 risk across E1/E2 while preserving "
            "the original tight-trail exit geometry."
        ),
    )

    parser.add_argument(
        "--stop-distance-diagnostics-only",
        action="store_true",
        help=(
            "Research only: scale planned stop distance and entry grid while "
            "preserving nominal risk; does not change live policy."
        ),
    )

    parser.add_argument(
        "--compare-late-targets-reduced-e3",
        action="store_true",
        help=(
            "Compare the frozen 70/25/5, 1/2/4R research challenger against "
            "current policy with paired per-case bootstrap diagnostics."
        ),
    )

    parser.add_argument(
        "--prospective-shadow",
        action="store_true",
        help=(
            "Compare the frozen challenger only on fully reconciled trades opened "
            "after its freeze timestamp; does not change live orders."
        ),
    )

    parser.add_argument(
        "--prospective-exit-shadow",
        action="store_true",
        help=(
            "Compare the frozen current-sizing exit-only candidate only on fully "
            "reconciled trades opened after its freeze time."
        ),
    )

    parser.add_argument(
        "--prospective-stop-distance-shadow",
        action="store_true",
        help=(
            "Research only: compare the frozen 2x stop/entry-grid geometry on "
            "later fully reconciled positions."
        ),
    )

    parser.add_argument(
        "--prospective-early-trail-shadow",
        action="store_true",
        help=(
            "Compare the frozen 0.20R activation / 0.10R distance exit-only "
            "candidate on later fully reconciled positions."
        ),
    )

    parser.add_argument(
        "--prospective-depth-exit-shadow",
        action="store_true",
        help=(
            "Research only: compare the frozen 0.25/0.50R entry depths, "
            "0.40/0.30R trail, and 1/2/4R targets on later reconciled cases."
        ),
    )

    parser.add_argument(
        "--prospective-demo-trail-shadow",
        action="store_true",
        help=(
            "Compare 0.40R versus 0.20R trail activation with the active Demo "
            "targets, sizing, and 0.10R trail distance on cases after its freeze."
        ),
    )

    parser.add_argument(
        "--fee-schedule-diagnostics-only",
        action="store_true",
        help=(
            "Research only: compare the scalar historical fee estimate with "
            "observed maker/taker fee rates; does not change live policy."
        ),
    )

    args = parser.parse_args()

    fee_rate, cases, maker_fee_rate, taker_fee_rate = load_cases(args.bundle)
    if args.reconciled_only:
        original_case_count = len(cases)
        cases = reconciled_position_cases(
            cases,
            completed_position_starts(args.bundle),
        )
        print(f"reconciled_only_cases={len(cases)}/{original_case_count}")

    if (
        sum(
            (
                args.prospective_shadow,
                args.prospective_exit_shadow,
                args.prospective_stop_distance_shadow,
                args.prospective_early_trail_shadow,
                args.prospective_depth_exit_shadow,
                args.prospective_demo_trail_shadow,
                args.rank_by_concentration_adjusted_train,
                args.compare_late_targets_reduced_e3,
                args.e3_cap_diagnostics_only,
                args.e3_risk_trim_diagnostics_only,
                args.e3_delay_diagnostics_only,
                args.e3_reclaim_diagnostics_only,
                args.time_stop_diagnostics_only,
                args.untriggered_time_stop_diagnostics_only,
                args.trail_activation_poll_diagnostics_only,
                args.demo_target_sweep_diagnostics_only,
                args.demo_runner_allocation_diagnostics_only,
                args.demo_trail_activation_diagnostics_only,
                args.demo_trail_distance_diagnostics_only,
                args.e3_allocation_diagnostics_only,
                args.stop_distance_diagnostics_only,
                args.fee_schedule_diagnostics_only,
            )
        )
        > 1
    ):
        parser.error("research diagnostics are mutually exclusive")

    if args.prospective_shadow:
        report_prospective_shadow(
            prospective_shadow_cases(
                cases,
                completed_position_starts(args.bundle),
            ),
            fee_rate,
        )
        return

    if args.fee_schedule_diagnostics_only:
        report_fee_schedule_diagnostics(
            cases,
            fee_rate,
            maker_fee_rate,
            taker_fee_rate,
        )
        return

    if args.prospective_exit_shadow:
        report_prospective_exit_shadow(
            prospective_shadow_cases(
                cases,
                completed_position_starts(args.bundle),
                frozen_after=EXIT_ONLY_FROZEN_AFTER,
            ),
            fee_rate,
        )
        return

    if args.prospective_stop_distance_shadow:
        report_prospective_stop_distance_shadow(
            prospective_shadow_cases(
                cases,
                completed_position_starts(args.bundle),
                frozen_after=STOP_GRID_FROZEN_AFTER,
            ),
            fee_rate,
        )
        return

    if args.prospective_early_trail_shadow:
        report_prospective_early_trail_shadow(
            prospective_shadow_cases(
                cases,
                completed_position_starts(args.bundle),
                frozen_after=EARLY_TRAIL_FROZEN_AFTER,
            ),
            fee_rate,
        )
        return

    if args.prospective_depth_exit_shadow:
        report_prospective_depth_exit_shadow(
            prospective_shadow_cases(
                cases,
                completed_position_starts(args.bundle),
                frozen_after=DEPTH_EXIT_FROZEN_AFTER,
            ),
            fee_rate,
        )
        return

    if args.prospective_demo_trail_shadow:
        report_prospective_demo_trail_shadow(
            prospective_shadow_cases(
                cases,
                completed_position_starts(args.bundle),
                frozen_after=DEMO_TRAIL_SHADOW_FROZEN_AFTER,
            ),
            fee_rate,
        )
        return

    if not 0 < args.validation_fraction < 1:
        parser.error("--validation-fraction must be between 0 and 1")

    if not 0 < args.holdout_fraction < 1:
        parser.error("--holdout-fraction must be between 0 and 1")

    if args.validation_fraction + args.holdout_fraction >= 1:
        parser.error("validation and holdout fractions must total less than 1")

    cases.sort(key=lambda case: case["start"])
    holdout_fraction = Decimal(str(args.holdout_fraction))
    validation_fraction = Decimal(str(args.validation_fraction))
    case_count = Decimal(len(cases))
    holdout_start = int(case_count * (1 - holdout_fraction))
    validation_start = int(case_count * (1 - holdout_fraction - validation_fraction))

    if (
        validation_start < 2
        or holdout_start <= validation_start
        or holdout_start >= len(cases)
    ):
        parser.error(
            "not enough cases for non-empty train, validation and holdout sets"
        )

    partitions = {
        "train": cases[:validation_start],
        "validation": cases[validation_start:holdout_start],
        "holdout": cases[holdout_start:],
    }
    for name, partition in partitions.items():
        for case in partition:
            case["split"] = name

    if (
        args.rank_by_concentration_adjusted_train
        and args.compare_late_targets_reduced_e3
    ):
        parser.error(
            "--rank-by-concentration-adjusted-train cannot be combined with "
            "--compare-late-targets-reduced-e3"
        )

    if args.compare_late_targets_reduced_e3:
        compare_challenger(cases, fee_rate, use_events=(not args.autonomous))
        return

    baseline_candidate = current_policy()

    research_modes = (
        args.e3_cap_diagnostics_only,
        args.e3_risk_trim_diagnostics_only,
        args.e3_delay_diagnostics_only,
        args.e3_reclaim_diagnostics_only,
        args.time_stop_diagnostics_only,
        args.untriggered_time_stop_diagnostics_only,
        args.trail_activation_poll_diagnostics_only,
        args.demo_target_sweep_diagnostics_only,
        args.demo_runner_allocation_diagnostics_only,
        args.demo_trail_activation_diagnostics_only,
        args.demo_trail_distance_diagnostics_only,
        args.e3_allocation_diagnostics_only,
        args.stop_distance_diagnostics_only,
        args.fee_schedule_diagnostics_only,
    )
    if sum(research_modes) > 1:
        parser.error("research-only diagnostics are mutually exclusive")

    if args.time_stop_diagnostics_only:
        report_time_stop_diagnostics(
            partitions,
            baseline_candidate,
            fee_rate,
            use_events=(not args.autonomous),
        )
        return

    if args.untriggered_time_stop_diagnostics_only:
        report_untriggered_time_stop_diagnostics(
            partitions,
            live_demo_tight_trail_candidate(),
            fee_rate,
            maker_fee_rate,
            taker_fee_rate,
        )
        return

    if args.trail_activation_poll_diagnostics_only:
        report_trail_activation_poll_diagnostics(
            partitions,
            live_demo_tight_trail_candidate(),
            fee_rate,
            maker_fee_rate,
            taker_fee_rate,
        )
        return

    if args.demo_target_sweep_diagnostics_only:
        report_demo_target_sweep_diagnostics(
            partitions,
            fee_rate,
            maker_fee_rate,
            taker_fee_rate,
        )
        return

    if args.demo_runner_allocation_diagnostics_only:
        report_demo_runner_allocation_diagnostics(
            partitions,
            fee_rate,
            maker_fee_rate,
            taker_fee_rate,
        )
        return

    if args.demo_trail_activation_diagnostics_only:
        report_demo_trail_activation_diagnostics(
            partitions,
            fee_rate,
            maker_fee_rate,
            taker_fee_rate,
        )
        return

    if args.demo_trail_distance_diagnostics_only:
        report_demo_trail_distance_diagnostics(
            partitions,
            fee_rate,
            maker_fee_rate,
            taker_fee_rate,
        )
        return

    if args.e3_allocation_diagnostics_only:
        report_e3_allocation_diagnostics(
            partitions,
            fee_rate,
            maker_fee_rate,
            taker_fee_rate,
        )
        return

    if args.stop_distance_diagnostics_only:
        report_stop_distance_diagnostics(
            partitions,
            baseline_candidate,
            fee_rate,
            use_events=(not args.autonomous),
        )
        return

    def report_e3_cap_diagnostics() -> None:
        print(
            "e3_cap_note=diagnostic only; tightens stop after E3 fill and is not "
            "implemented by production policy"
        )
        profiles = (("current", None), ("cap_0.50R", 0.50), ("cap_0.75R", 0.75))
        for profile_name, cap_r in profiles:
            for name, partition in partitions.items():
                results = [
                    replay(
                        case,
                        baseline_candidate,
                        fee_rate,
                        use_events=(not args.autonomous),
                        e3_loss_cap_r=cap_r,
                    )
                    for case in partition
                ]
                summary = summarize_results(results)
                print(
                    f"e3_cap_profile={profile_name}/{name} "
                    f"net:{summary['net_r']:+.3f}R/"
                    f"PF{summary['profit_factor']:.2f}/"
                    f"WR{summary['win_rate']:.0%}/n{summary['count']}"
                )

    if args.e3_cap_diagnostics_only:
        report_e3_cap_diagnostics()
        return

    if args.e3_risk_trim_diagnostics_only:
        baseline_results = {
            name: [
                replay(
                    case,
                    baseline_candidate,
                    fee_rate,
                    use_events=(not args.autonomous),
                )
                for case in partition
            ]
            for name, partition in partitions.items()
        }
        print(
            "e3_risk_trim_note=research only; after E3 fill, if the same 1m candle "
            "does not hit the protective stop, reduce at its close just enough to "
            "cap projected stop loss; taker fees included, slippage omitted; "
            "historical holdout is already inspected"
        )
        for cap_r in (0.75, 0.85, 0.95):
            for name, partition in partitions.items():
                candidate_results = [
                    replay(
                        case,
                        baseline_candidate,
                        fee_rate,
                        use_events=(not args.autonomous),
                        e3_risk_trim_cap_r=cap_r,
                    )
                    for case in partition
                ]
                summary = summarize_results(candidate_results)
                paired = paired_difference_summary(
                    baseline_results[name],
                    candidate_results,
                    bootstrap_iterations=3_000,
                )
                interval = paired["circular_block_4_bootstrap_95ci_delta_net_r"]
                interval_text = (
                    "n/a"
                    if interval is None
                    else f"[{interval[0]:+.3f},{interval[1]:+.3f}]"
                )
                print(
                    f"e3_risk_trim=cap_{cap_r:.2f}R/{name} "
                    f"net:{summary['net_r']:+.3f}R/"
                    f"PF{summary['profit_factor']:.2f}/"
                    f"WR{summary['win_rate']:.0%}/"
                    f"avg_win:{summary['avg_win_r']:.3f}R/"
                    f"avg_loss:{format_optional_r(summary['avg_loss_r'])}/"
                    f"delta:{paired['delta_net_r']:+.3f}R/"
                    f"block_CI:{interval_text}/"
                    f"delta_ex_top3:{paired['delta_net_r_excluding_top_3_positive_cases']:+.3f}R/"
                    f"n{summary['count']}"
                )
        return

    if args.e3_delay_diagnostics_only:
        print(
            "e3_delay_note=research only; E3 order is inactive until the selected "
            "age; prices already through E3 when activated are filled at E3 limit "
            "(conservative vs a marketable limit fill)"
        )
        for name, candidate in (
            ("current", baseline_candidate),
            ("combined_frozen", late_target_reduced_e3_candidate()),
        ):
            for delay_minutes in (0, 15, 60, 240):
                for split, partition in partitions.items():
                    results = [
                        replay(
                            case,
                            candidate,
                            fee_rate,
                            use_events=(not args.autonomous),
                            e3_min_delay_minutes=delay_minutes,
                        )
                        for case in partition
                    ]
                    summary = summarize_results(results)
                    print(
                        f"e3_delay_profile={name}/{delay_minutes}m/{split} "
                        f"net:{summary['net_r']:+.3f}R/"
                        f"PF{summary['profit_factor']:.2f}/"
                        f"WR{summary['win_rate']:.0%}/n{summary['count']}"
                    )
        return

    if args.e3_reclaim_diagnostics_only:
        print(
            "e3_reclaim_note=research only; after E3 is first touched, enter at "
            "a later 1m close through E2; preserve E3 stop-risk weight, charge "
            "the configured taker fee, skip same-candle post-entry price action; "
            "slippage is not modeled"
        )
        for name, partition in partitions.items():
            baseline_results = [
                replay(
                    case,
                    baseline_candidate,
                    fee_rate,
                    use_events=(not args.autonomous),
                )
                for case in partition
            ]
            reclaim_results = [
                replay(
                    case,
                    baseline_candidate,
                    fee_rate,
                    use_events=(not args.autonomous),
                    e3_reclaim_e2=True,
                )
                for case in partition
            ]
            print(
                f"e3_reclaim_profile={name}/baseline",
                json.dumps(summarize_results(baseline_results), sort_keys=True),
            )
            print(
                f"e3_reclaim_profile={name}/reclaim_e2",
                json.dumps(summarize_results(reclaim_results), sort_keys=True),
            )
            print(
                f"e3_reclaim_paired_delta={name}",
                json.dumps(
                    paired_difference_summary(baseline_results, reclaim_results),
                    sort_keys=True,
                ),
            )
        return

    baseline_training_results = [
        replay(
            case,
            baseline_candidate,
            fee_rate,
            use_events=(not args.autonomous),
        )
        for case in partitions["train"]
    ]

    def evaluate(candidate_set):
        ranked_candidates = []
        for candidate in candidate_set:
            partition_results = {
                name: [
                    replay(
                        case,
                        candidate,
                        fee_rate,
                        use_events=(not args.autonomous),
                    )
                    for case in partition
                ]
                for name, partition in partitions.items()
            }

            train = summarize_results(partition_results["train"])
            train_differences = [
                candidate_result.net_r - baseline_result.net_r
                for baseline_result, candidate_result in zip(
                    baseline_training_results,
                    partition_results["train"],
                    strict=True,
                )
            ]
            _, train_delta_after_top_three = concentration_adjusted_delta(
                train_differences
            )
            validation = summarize_results(partition_results["validation"])
            holdout = summarize_results(partition_results["holdout"])
            all_results = [
                result for values in partition_results.values() for result in values
            ]
            giveback = statistics.mean(
                max(0.0, result.mfe_r - result.net_r) for result in all_results
            )
            roundtrip = sum(result.roundtrip_loss for result in all_results)
            stops = sum(result.stopped for result in all_results)
            lifecycle_actions = sum(result.lifecycle_actions for result in all_results)
            funding_r = sum(result.funding_r for result in all_results)
            ranking_score = (
                train_delta_after_top_three
                if args.rank_by_concentration_adjusted_train
                else train["net_r"]
            )
            ranked_candidates.append(
                (
                    (-ranking_score, train["max_drawdown_r"], giveback),
                    candidate,
                    {
                        "train": train,
                        "train_delta_after_top_three": train_delta_after_top_three,
                        "validation": validation,
                        "holdout": holdout,
                        "roundtrip": roundtrip,
                        "stops": stops,
                        "giveback": giveback,
                        "lifecycle_actions": lifecycle_actions,
                        "funding_r": funding_r,
                    },
                )
            )
        return sorted(ranked_candidates, key=lambda item: item[0])

    ranked = evaluate([baseline_candidate, *candidates()])
    no_e3_ranked = evaluate(no_e3_counterfactuals())
    baseline = next(item for item in ranked if item[1] == baseline_candidate)
    best_no_e3 = no_e3_ranked[0]

    historical_lifecycle_actions = sum(
        event["kind"] == "LIFECYCLE" for case in cases for event in case["events"]
    )

    funding_events = sum(
        event["kind"] == "FUNDING" for case in cases for event in case["events"]
    )

    print("mode=" + ("autonomous" if args.autonomous else "historical-lifecycle"))

    print(f"filled_cases={len(cases)}")

    print(
        "chronological_split="
        f"train:{len(partitions['train'])}/"
        f"validation:{len(partitions['validation'])}/"
        f"holdout:{len(partitions['holdout'])}"
    )

    print(
        "ranking="
        + (
            "training paired delta after removing top three positive cases"
            if args.rank_by_concentration_adjusted_train
            else "training net R"
        )
        + ", then training drawdown; validation/holdout are not ranked"
    )

    print(
        "no_e3_status=counterfactual only; current V2 policy requires positive "
        "risk allocation for every entry leg"
    )

    print(
        "current_policy="
        f"train:{baseline[2]['train']['net_r']:+.3f}R/"
        f"PF{baseline[2]['train']['profit_factor']:.2f} "
        f"validation:{baseline[2]['validation']['net_r']:+.3f}R/"
        f"PF{baseline[2]['validation']['profit_factor']:.2f} "
        f"holdout:{baseline[2]['holdout']['net_r']:+.3f}R/"
        f"PF{baseline[2]['holdout']['profit_factor']:.2f}"
    )

    print(f"median_fee_rate={fee_rate:.8f}")

    print(f"historical_lifecycle_actions={historical_lifecycle_actions}")

    print(f"funding_events={funding_events}")

    print("fill_cohort_note=observed fills condition on adverse price path; not causal")

    diagnostic_profiles = (
        ("current", baseline_candidate),
        ("best_train", ranked[0][1]),
        ("best_no_e3_train", best_no_e3[1]),
    )
    for profile_name, profile in diagnostic_profiles:
        profile_results = {
            name: [
                replay(case, profile, fee_rate, use_events=(not args.autonomous))
                for case in partition
            ]
            for name, partition in partitions.items()
        }
        for name, partition in partitions.items():
            total = summarize_results(profile_results[name])
            print(
                f"profile_cohort={profile_name}/{name} "
                f"net:{total['net_r']:+.3f}R/"
                f"PF{total['profit_factor']:.2f}/"
                f"WR{total['win_rate']:.0%}/n{total['count']}"
            )
            for side, summary in summarize_by_side(
                partition,
                profile_results[name],
            ).items():
                print(
                    f"direction_cohort={profile_name}/{name}/{side} "
                    f"net:{summary['net_r']:+.3f}R/"
                    f"PF{summary['profit_factor']:.2f}/"
                    f"WR{summary['win_rate']:.0%}/n{summary['count']}"
                )
        diagnostics = observed_fill_diagnostics(
            cases,
            profile,
            fee_rate,
            use_events=(not args.autonomous),
        )
        for pattern, periods in diagnostics.items():
            all_result = periods["all"]
            holdout_result = periods["holdout"]
            print(
                f"fill_cohort={profile_name}/{pattern} "
                f"all:{all_result['net_r']:+.3f}R/"
                f"PF{all_result['profit_factor']:.2f}/"
                f"n{all_result['count']} "
                f"holdout:{holdout_result['net_r']:+.3f}R/"
                f"PF{holdout_result['profit_factor']:.2f}/"
                f"n{holdout_result['count']}"
            )

    report_e3_cap_diagnostics()

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
            f"train={result['train']['net_r']:+.3f}R/"
            f"PF{result['train']['profit_factor']:.2f}/"
            f"WR{result['train']['win_rate']:.0%} "
            f"train_delta_after_top3="
            f"{result['train_delta_after_top_three']:+.3f}R "
            f"validation={result['validation']['net_r']:+.3f}R/"
            f"PF{result['validation']['profit_factor']:.2f} "
            f"holdout={result['holdout']['net_r']:+.3f}R/"
            f"PF{result['holdout']['profit_factor']:.2f}/"
            f"WR{result['holdout']['win_rate']:.0%} "
            f"roundtrip="
            f"{result['roundtrip']} "
            f"stops="
            f"{result['stops']} "
            f"trainDD="
            f"{result['train']['max_drawdown_r']:.3f}R "
            f"giveback="
            f"{result['giveback']:.3f}R "
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
            f"{candidate.trail_by}R "
            f"take_profit="
            f"{candidate.take_profit_rs}/"
            f"{candidate.take_profit_pcts}%"
        )


if __name__ == "__main__":
    main()
