"""Enforce the account-wide portfolio stop-risk cap across independent zone admissions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from .domain import (
    AccountStateSummary,
    ExecutionPlan,
    PositionStrategy,
)
from .portfolio_risk import open_stop_risk_usdt
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
