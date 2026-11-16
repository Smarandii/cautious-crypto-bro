"""Offline replay of a single-entry, full-exit mirrored position."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from itertools import chain, islice
from typing import Literal

from .domain import Side


@dataclass(frozen=True, slots=True)
class MirrorBar:
    opening: Decimal
    high: Decimal
    low: Decimal
    # Positive rates are paid by longs to shorts.
    funding_rate: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class MirrorOutcome:
    status: Literal["stop", "target", "ambiguous", "open", "unfilled"]
    net_r: Decimal | None = None


def replay_mirror(
    source_side: Side,
    entry: Decimal,
    standard_stop: Decimal,
    standard_first_target: Decimal,
    bars: Iterable[MirrorBar],
    *,
    market_entry: bool,
    entry_fee: Decimal,
    exit_fee: Decimal,
    stop_slippage: Decimal = Decimal(0),
    horizon_close: Decimal | None = None,
    limit_entry_bars: int | None = None,
) -> MirrorOutcome:
    """Use adverse stop gaps and expose bars with indeterminate event order."""
    sign = Decimal(-1 if source_side is Side.LONG else 1)
    stop, target = standard_first_target, standard_stop
    _validate_inputs(entry, stop, target, sign, (entry_fee, exit_fee, stop_slippage))
    remaining = iter(bars)
    eligible = remaining if market_entry else islice(remaining, limit_entry_bars)
    entry_fill = _entry_fill(eligible, sign, entry, market_entry)
    if entry_fill is None:
        return MirrorOutcome("unfilled")
    fill, first_bar = entry_fill
    if not sign * (stop - fill) < 0 < sign * (target - fill):
        return MirrorOutcome("ambiguous")
    if first_bar.opening == fill:
        remaining = chain((first_bar,), remaining)
    elif _exit_hit(first_bar, sign, stop, target):
        return MirrorOutcome("ambiguous")

    funding = Decimal(0)
    for bar in remaining:
        exit_hit = _exit_hit(bar, sign, stop, target)
        if exit_hit is None:
            if bar is not first_bar:
                funding -= sign * bar.opening * bar.funding_rate
            continue
        status, exit_price = exit_hit
        if exit_price is None:
            return MirrorOutcome("ambiguous")
        if status == "stop":
            exit_price *= 1 - sign * stop_slippage
        break
    else:
        status, exit_price = "open", horizon_close

    return MirrorOutcome(
        status,
        None
        if exit_price is None
        else (
            sign * (exit_price - fill)
            + funding
            - fill * entry_fee
            - exit_price * exit_fee
        )
        / abs(entry - stop),
    )


def _validate_inputs(
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    sign: Decimal,
    rates: tuple[Decimal, ...],
) -> None:
    if any(not price.is_finite() or price <= 0 for price in (entry, stop, target)):
        raise ValueError("Mirror prices must be positive and finite")
    if not sign * (stop - entry) < 0 < sign * (target - entry):
        raise ValueError("Mirror stop and target must straddle entry")
    if any(not rate.is_finite() or not 0 <= rate < 1 for rate in rates):
        raise ValueError("Mirror cost rates must be finite and in [0, 1)")


def _entry_fill(
    bars: Iterator[MirrorBar], sign: Decimal, entry: Decimal, market_entry: bool
) -> tuple[Decimal, MirrorBar] | None:
    for bar in bars:
        if market_entry:
            return bar.opening, bar
        if sign > 0 and bar.low <= entry:
            return min(entry, bar.opening), bar
        if sign < 0 and bar.high >= entry:
            return max(entry, bar.opening), bar
    return None


def _exit_hit(
    bar: MirrorBar, sign: Decimal, stop: Decimal, target: Decimal
) -> tuple[Literal["stop", "target", "ambiguous"], Decimal | None] | None:
    if sign * (bar.opening - stop) <= 0:
        return "stop", bar.opening
    if sign * (bar.opening - target) >= 0:
        return "target", target
    adverse = bar.low if sign > 0 else bar.high
    favorable = bar.high if sign > 0 else bar.low
    stop_hit = sign * (adverse - stop) <= 0
    target_hit = sign * (favorable - target) >= 0
    if stop_hit and target_hit:
        return "ambiguous", None
    if stop_hit:
        return "stop", stop
    if target_hit:
        return "target", target
    return None
