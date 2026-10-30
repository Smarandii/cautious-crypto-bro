"""Validate instrument/tier metadata and select leverage without exchange writes."""

from dataclasses import dataclass
from decimal import Decimal

from ..domain import EntryPreflightError, LeverageFilter, TradeExecutionError
from .normalize import parse_decimal, position_exposure_from_item


def instrument_leverage_filter(instrument: dict) -> LeverageFilter:
    raw = instrument.get("leverageFilter")
    if not isinstance(raw, dict):
        raise TradeExecutionError("Bybit instrument leverage filter is unavailable")
    try:
        return LeverageFilter(
            min_leverage=parse_decimal(raw.get("minLeverage")),
            max_leverage=parse_decimal(raw.get("maxLeverage")),
            step=parse_decimal(raw.get("leverageStep")),
        )
    except (ValueError, ArithmeticError, TradeExecutionError) as exc:
        raise TradeExecutionError(
            "Bybit instrument leverage filter is invalid"
        ) from exc


def risk_tier_id(raw: object) -> int:
    text = str(raw)
    if (
        isinstance(raw, bool)
        or not text.isascii()
        or not text.isdigit()
        or int(text) <= 0
    ):
        raise TradeExecutionError("Bybit risk tier is missing or unsupported")
    return int(text)


def flat_position_risk_tier(positions: list[dict], symbol: str) -> int:
    """Require every row to prove flat one-way state and the same risk tier."""
    if not positions:
        raise EntryPreflightError("Current symbol risk-tier evidence is incomplete")
    current_ids = set()
    # Demo can return several flat rows with different historical fields.
    # Every row must authorize the same flat one-way risk tier; never pick
    # one row and discard contradictory exposure or mode evidence.
    for position in positions:
        if not isinstance(position, dict) or position.get("symbol") != symbol:
            raise EntryPreflightError("Current symbol risk-tier evidence is incomplete")
        if isinstance(position.get("positionIdx"), bool) or position.get(
            "positionIdx"
        ) not in (0, "0"):
            raise EntryPreflightError(
                "One-way position mode cannot be verified for leverage preparation"
            )
        if position_exposure_from_item(position) is not None:
            raise EntryPreflightError(
                "Existing position prevents entry leverage preparation"
            )
        if len(positions) > 1 and position.get("side") != "":
            raise EntryPreflightError(
                "Multiple position rows do not confirm an empty side"
            )
        if position.get("positionStatus") not in ("Normal", ""):
            raise EntryPreflightError(
                "Current position status does not permit entry preparation"
            )
        current_ids.add(risk_tier_id(position.get("riskId")))
    if len(current_ids) != 1:
        raise EntryPreflightError("Current position risk-tier evidence conflicts")
    return current_ids.pop()


@dataclass(frozen=True)
class RiskTier:
    tier_id: int
    value_limit: Decimal
    max_leverage: Decimal

    def __post_init__(self) -> None:
        if (
            isinstance(self.tier_id, bool)
            or self.tier_id <= 0
            or not self.value_limit.is_finite()
            or self.value_limit <= 0
            or not self.max_leverage.is_finite()
            or self.max_leverage < 1
        ):
            raise TradeExecutionError("Bybit risk-limit bounds are invalid")


def risk_tiers(items: list, symbol: str) -> tuple[RiskTier, ...]:
    tiers = []
    for item in items:
        if not isinstance(item, dict) or item.get("symbol") != symbol:
            raise TradeExecutionError("Bybit risk-limit symbol evidence is invalid")
        try:
            tier = RiskTier(
                risk_tier_id(item.get("id")),
                parse_decimal(item.get("riskLimitValue")),
                parse_decimal(item.get("maxLeverage")),
            )
        except (ArithmeticError, ValueError) as exc:
            raise TradeExecutionError(
                "Bybit risk-limit numeric evidence is invalid"
            ) from exc
        tiers.append(tier)
    if (
        not tiers
        or len({t.tier_id for t in tiers}) != len(tiers)
        or len({t.value_limit for t in tiers}) != len(tiers)
    ):
        raise TradeExecutionError("Bybit risk-limit tiers are empty or ambiguous")
    return tuple(sorted(tiers, key=lambda t: t.value_limit))


def tier_leverage(
    requested: Decimal,
    limits: LeverageFilter,
    tiers: tuple[RiskTier, ...],
    current_tier_id: int,
    projected_notional: Decimal,
) -> Decimal:
    if not projected_notional.is_finite() or projected_notional <= 0:
        raise TradeExecutionError(
            "Projected entry notional must be positive and finite"
        )
    current = next((t for t in tiers if t.tier_id == current_tier_id), None)
    projected = next((t for t in tiers if projected_notional <= t.value_limit), None)
    if current is None or projected is None:
        raise TradeExecutionError(
            "Applicable current/projected risk tier cannot be verified"
        )
    ceiling = min(limits.max_leverage, current.max_leverage, projected.max_leverage)
    try:
        return LeverageFilter(limits.min_leverage, ceiling, limits.step).select(
            requested
        )
    except ValueError as exc:
        raise TradeExecutionError(
            "No supported leverage fits the applicable tiers"
        ) from exc
