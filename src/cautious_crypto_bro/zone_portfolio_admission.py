"""Enforce the account-wide portfolio stop-risk cap across independent zone admissions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from .domain import (
    AccountOrder,
    AccountStateSummary,
    ExecutionPlan,
    PositionStrategy,
    Side,
)
from .portfolio_risk import PortfolioRiskUnavailable, open_stop_risk_usdt
from .position_management._helpers import _matches_exit
from .zone_ledger import ZoneExecution, ZoneOrder
from .zone_runner_protection import zone_group_open_stop_risk_usdt


@dataclass(frozen=True, slots=True)
class ZoneAdmissionDecision:
    zone_id: UUID
    admitted: bool
    candidate_risk_budget_usdt: Decimal
    candidate_planned_risk_usdt: Decimal
    existing_group_risk_usdt: Decimal
    other_symbols_risk_usdt: Decimal
    prospective_portfolio_risk_usdt: Decimal
    portfolio_stop_risk_cap_usdt: Decimal | None
    rejection_reason: str | None
    trading_enabled: bool


@dataclass(frozen=True, slots=True)
class ZoneGroupDriftStatus:
    drifted: bool
    pause_reason: str | None
    open_stop_risk_usdt: Decimal | None
    new_entries_allowed: bool
    automatic_mutations_allowed: bool
    trading_enabled: bool


def _verify_independent_full_budget(
    candidate_plan: ExecutionPlan,
    existing_group_plans: Sequence[ExecutionPlan],
    configured_risk_per_trade_pct: Decimal | None,
) -> Decimal:
    budget = candidate_plan.policy.risk_budget_usdt
    if candidate_plan.planned_max_loss_usdt <= 0:
        raise ValueError("Candidate zone planned stop risk must be positive")
    if (
        configured_risk_per_trade_pct is not None
        and candidate_plan.policy.risk_per_trade_pct < configured_risk_per_trade_pct
    ):
        raise ValueError("Zone risk budget must not be split across sibling zones")
    for sibling in existing_group_plans:
        if sibling.intent_id == candidate_plan.intent_id:
            raise ValueError("Candidate zone is already enrolled in the zone group")
        if (
            sibling.symbol != candidate_plan.symbol
            or sibling.side is not candidate_plan.side
        ):
            raise ValueError("Zone group plans must share symbol and side")
        if (
            sibling.policy.trading_capital_usdt
            == candidate_plan.policy.trading_capital_usdt
            and candidate_plan.policy.risk_per_trade_pct
            < sibling.policy.risk_per_trade_pct
        ):
            raise ValueError("Zone risk budget must not be split across sibling zones")
    return budget


def evaluate_zone_portfolio_admission(
    candidate_plan: ExecutionPlan,
    existing_group_plans: Sequence[ExecutionPlan],
    group_orders: Sequence[ZoneOrder],
    group_executions: Sequence[ZoneExecution],
    account_state: AccountStateSummary,
    *,
    other_active_strategies: Sequence[tuple[PositionStrategy, ExecutionPlan]] = (),
    portfolio_stop_risk_cap_usdt: Decimal | None = None,
    configured_risk_per_trade_pct: Decimal | None = None,
    protected_stops: Mapping[UUID, Decimal] | None = None,
) -> ZoneAdmissionDecision:
    """Check a full-budget zone against existing group and account-wide stop risk."""
    if portfolio_stop_risk_cap_usdt is not None and portfolio_stop_risk_cap_usdt <= 0:
        raise ValueError("Portfolio stop-risk cap must be positive")
    budget = _verify_independent_full_budget(
        candidate_plan, existing_group_plans, configured_risk_per_trade_pct
    )
    if existing_group_plans:
        group_risk = zone_group_open_stop_risk_usdt(
            existing_group_plans,
            group_orders,
            group_executions,
            account_state,
            protected_stops=protected_stops,
        )
    else:
        group_risk = Decimal("0")
    other_account = AccountStateSummary(
        as_of=account_state.as_of,
        positions=tuple(
            p for p in account_state.positions if p.symbol != candidate_plan.symbol
        ),
        open_orders=tuple(
            o for o in account_state.open_orders if o.symbol != candidate_plan.symbol
        ),
    )
    other_strategies = tuple(
        (state, plan)
        for state, plan in other_active_strategies
        if state.symbol != candidate_plan.symbol
    )
    other_risk = open_stop_risk_usdt(other_account, other_strategies)
    prospective = group_risk + other_risk + candidate_plan.planned_max_loss_usdt
    if (
        portfolio_stop_risk_cap_usdt is not None
        and prospective > portfolio_stop_risk_cap_usdt
    ):
        reason = (
            f"Combined portfolio stop risk {prospective} USDT exceeds cap "
            f"{portfolio_stop_risk_cap_usdt} USDT; refusing to split zone budget"
        )
        return ZoneAdmissionDecision(
            zone_id=candidate_plan.intent_id,
            admitted=False,
            candidate_risk_budget_usdt=budget,
            candidate_planned_risk_usdt=candidate_plan.planned_max_loss_usdt,
            existing_group_risk_usdt=group_risk,
            other_symbols_risk_usdt=other_risk,
            prospective_portfolio_risk_usdt=prospective,
            portfolio_stop_risk_cap_usdt=portfolio_stop_risk_cap_usdt,
            rejection_reason=reason,
            trading_enabled=False,
        )
    return ZoneAdmissionDecision(
        zone_id=candidate_plan.intent_id,
        admitted=True,
        candidate_risk_budget_usdt=budget,
        candidate_planned_risk_usdt=candidate_plan.planned_max_loss_usdt,
        existing_group_risk_usdt=group_risk,
        other_symbols_risk_usdt=other_risk,
        prospective_portfolio_risk_usdt=prospective,
        portfolio_stop_risk_cap_usdt=portfolio_stop_risk_cap_usdt,
        rejection_reason=None,
        trading_enabled=False,
    )


def _check_zone_exit_drift(
    symbol: str,
    side: Side,
    open_orders: Sequence[AccountOrder],
    orders: Sequence[ZoneOrder],
    expected_exits_by_zone: Mapping[UUID, Mapping[str, tuple[Decimal, Decimal]]],
) -> str | None:
    known_by_id = {o.order_id: o for o in orders}
    for live in open_orders:
        if live.symbol != symbol:
            continue
        binding = known_by_id.get(live.order_id)
        if binding is None or binding.role != "EXIT":
            continue
        expected_map = expected_exits_by_zone.get(binding.zone_id, {})
        expected = expected_map.get(live.order_link_id)
        if expected is None or not _matches_exit(live, side, expected[0], expected[1]):
            return (
                f"zone {binding.zone_id} fixed exit {live.order_link_id} drifted "
                "from expected geometry; manual review required"
            )
    return None


def detect_zone_group_manual_drift(
    plans: Sequence[ExecutionPlan],
    orders: Sequence[ZoneOrder],
    executions: Sequence[ZoneExecution],
    account_state: AccountStateSummary,
    *,
    protected_stops: Mapping[UUID, Decimal] | None = None,
    entry_frozen_zones: frozenset[UUID] = frozenset(),
    expected_exits_by_zone: (
        Mapping[UUID, Mapping[str, tuple[Decimal, Decimal]]] | None
    ) = None,
) -> ZoneGroupDriftStatus:
    """Inspect a zone group for position, child-stop, frozen-entry, or exit drift."""
    if not plans:
        raise ValueError("Zone group drift detection requires at least one plan")
    try:
        open_risk = zone_group_open_stop_risk_usdt(
            plans,
            orders,
            executions,
            account_state,
            protected_stops=protected_stops,
        )
    except PortfolioRiskUnavailable as exc:
        return ZoneGroupDriftStatus(
            drifted=True,
            pause_reason=str(exc),
            open_stop_risk_usdt=None,
            new_entries_allowed=False,
            automatic_mutations_allowed=False,
            trading_enabled=False,
        )
    symbol, side = plans[0].symbol, plans[0].side
    known_by_id = {o.order_id: o for o in orders}
    for live in account_state.open_orders:
        if live.symbol != symbol:
            continue
        binding = known_by_id.get(live.order_id)
        if (
            binding is not None
            and binding.role == "ENTRY"
            and binding.zone_id in entry_frozen_zones
        ):
            return ZoneGroupDriftStatus(
                drifted=True,
                pause_reason=(
                    f"zone {binding.zone_id} has open entry order {live.order_id} "
                    "after entry freeze; manual review required"
                ),
                open_stop_risk_usdt=None,
                new_entries_allowed=False,
                automatic_mutations_allowed=False,
                trading_enabled=False,
            )
    if expected_exits_by_zone:
        exit_drift = _check_zone_exit_drift(
            symbol, side, account_state.open_orders, orders, expected_exits_by_zone
        )
        if exit_drift is not None:
            return ZoneGroupDriftStatus(
                drifted=True,
                pause_reason=exit_drift,
                open_stop_risk_usdt=None,
                new_entries_allowed=False,
                automatic_mutations_allowed=False,
                trading_enabled=False,
            )
    return ZoneGroupDriftStatus(
        drifted=False,
        pause_reason=None,
        open_stop_risk_usdt=open_risk,
        new_entries_allowed=False,
        automatic_mutations_allowed=False,
        trading_enabled=False,
    )
