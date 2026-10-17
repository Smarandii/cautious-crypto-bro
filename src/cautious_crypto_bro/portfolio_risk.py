from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from .domain import (
    AccountStateSummary,
    ExecutionPlan,
    ExecutionPolicy,
    PositionStrategy,
    Side,
)


class PortfolioRiskUnavailable(ValueError):
    """The current account risk cannot be bounded safely."""


def open_stop_risk_usdt(
    account_state: AccountStateSummary,
    active_strategies: Iterable[tuple[PositionStrategy, ExecutionPlan]],
) -> Decimal:
    """Estimate current open risk from live stops and outstanding V2 entries."""
    plans = tuple(plan for _, plan in active_strategies)
    total = Decimal("0")

    for position in account_state.positions:
        if position.stop_loss is None:
            raise PortfolioRiskUnavailable(
                f"{position.symbol} position has no exchange stop price"
            )
        if position.side is Side.LONG:
            loss_per_unit = max(
                Decimal("0"),
                position.avg_price - position.stop_loss,
            )
        else:
            loss_per_unit = max(
                Decimal("0"),
                position.stop_loss - position.avg_price,
            )
        total += position.size * loss_per_unit

    for order in account_state.open_orders:
        if (
            order.remaining_quantity <= 0
            or order.reduce_only
            or order.close_on_trigger
            or order.is_protective
        ):
            continue

        prefix = "ccb-v2-"
        plan = next(
            (
                candidate
                for candidate in plans
                if order.order_link_id.startswith(
                    f"{prefix}{candidate.intent_id.hex[:20]}-"
                )
            ),
            None,
        )
        if plan is None:
            raise PortfolioRiskUnavailable(
                f"{order.symbol} has an entry order without a known V2 stop plan"
            )

        if order.symbol != plan.symbol or order.side is not plan.side:
            raise PortfolioRiskUnavailable(
                f"{order.symbol} entry order does not match its V2 plan"
            )

        entry_name = order.order_link_id.rsplit("-", 1)[-1].upper()
        planned_order = next(
            (item for item in plan.orders if item.name.upper() == entry_name),
            None,
        )
        entry_price = order.price or (
            planned_order.reference_price if planned_order is not None else None
        )
        if entry_price is None:
            raise PortfolioRiskUnavailable(
                f"{order.symbol} entry order has no measurable price"
            )

        total += order.remaining_quantity * abs(entry_price - plan.stop_loss)

    return total


def policy_with_remaining_portfolio_risk(
    policy: ExecutionPolicy,
    remaining_risk_usdt: Decimal,
) -> ExecutionPolicy:
    """Limit one trade's risk budget without increasing its configured budget."""
    if remaining_risk_usdt <= 0:
        raise ValueError("No portfolio stop-risk capacity remains")

    current_budget = policy.risk_budget_usdt
    effective_budget = min(current_budget, remaining_risk_usdt)
    if effective_budget == current_budget:
        return policy

    if policy.trading_capital_usdt is None:
        raise ValueError("Execution policy has no frozen live-capital snapshot")

    effective_risk_pct = effective_budget * Decimal("100") / policy.trading_capital_usdt
    return policy.model_copy(update={"risk_per_trade_pct": effective_risk_pct})
