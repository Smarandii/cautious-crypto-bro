from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cautious_crypto_bro.domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    PlannedOrder,
    Side,
)
from cautious_crypto_bro.portfolio_risk import (
    PortfolioRiskUnavailable,
    open_stop_risk_usdt,
    policy_with_remaining_portfolio_risk,
)


def _plan():
    intent_id = uuid4()
    return ExecutionPlan(
        strategy_version=1,
        intent_id=intent_id,
        symbol="BTCUSDT",
        side=Side.LONG,
        orders=(
            PlannedOrder(
                name="E1",
                order_type=ExecutionOrderType.LIMIT,
                quantity=Decimal("1"),
                price=Decimal("95"),
                reference_price=Decimal("95"),
            ),
        ),
        stop_loss=Decimal("90"),
        take_profit=Decimal("120"),
        policy=ExecutionPolicy(
            trading_capital_usdt=Decimal("10000"),
            risk_per_trade_pct=Decimal("1"),
        ),
        planned_max_loss_usdt=Decimal("5"),
    )


def test_open_stop_risk_includes_positions_and_unfilled_v2_entry_orders() -> None:
    now = datetime.now(UTC)
    plan = _plan()
    state = AccountStateSummary(
        as_of=now,
        positions=(
            AccountPosition(
                symbol="ETHUSDT",
                side=Side.LONG,
                size=Decimal("1"),
                avg_price=Decimal("100"),
                mark_price=Decimal("100"),
                unrealised_pnl=Decimal("0"),
                status="Open",
                take_profit=None,
                stop_loss=Decimal("90"),
            ),
        ),
        open_orders=(
            AccountOrder(
                symbol="BTCUSDT",
                side=Side.LONG,
                order_type="Limit",
                status="New",
                quantity=Decimal("1"),
                remaining_quantity=Decimal("2"),
                price=Decimal("95"),
                avg_price=None,
                order_id="order-1",
                order_link_id=(f"ccb-v2-{plan.intent_id.hex[:20]}-e1"),
                reduce_only=False,
                updated_at=now,
            ),
            AccountOrder(
                symbol="ETHUSDT",
                side=Side.SHORT,
                order_type="Limit",
                status="New",
                quantity=Decimal("1"),
                remaining_quantity=Decimal("1"),
                price=Decimal("90"),
                avg_price=None,
                order_id="tp-1",
                order_link_id="external-tp",
                reduce_only=True,
                updated_at=now,
            ),
        ),
    )

    assert open_stop_risk_usdt(state, ((SimpleNamespace(), plan),)) == Decimal("20")


def test_open_stop_risk_fails_closed_for_unprotected_position_or_unknown_entry() -> (
    None
):
    now = datetime.now(UTC)
    unprotected = AccountStateSummary(
        as_of=now,
        positions=(
            AccountPosition(
                symbol="ETHUSDT",
                side=Side.LONG,
                size=Decimal("1"),
                avg_price=Decimal("100"),
                mark_price=Decimal("100"),
                unrealised_pnl=Decimal("0"),
                status="Open",
                take_profit=None,
                stop_loss=None,
            ),
        ),
        open_orders=(),
    )
    with pytest.raises(PortfolioRiskUnavailable, match="no exchange stop"):
        open_stop_risk_usdt(unprotected, ())

    unknown_entry = AccountStateSummary(
        as_of=now,
        positions=(),
        open_orders=(
            AccountOrder(
                symbol="ETHUSDT",
                side=Side.LONG,
                order_type="Limit",
                status="New",
                quantity=Decimal("1"),
                remaining_quantity=Decimal("1"),
                price=Decimal("100"),
                avg_price=None,
                order_id="external-entry",
                order_link_id="manual-entry",
                reduce_only=False,
                updated_at=now,
            ),
        ),
    )
    with pytest.raises(PortfolioRiskUnavailable, match="without a known V2"):
        open_stop_risk_usdt(unknown_entry, ())


def test_portfolio_budget_can_only_reduce_per_trade_budget() -> None:
    policy = ExecutionPolicy(
        trading_capital_usdt=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
    )

    reduced = policy_with_remaining_portfolio_risk(policy, Decimal("25"))
    unchanged = policy_with_remaining_portfolio_risk(policy, Decimal("200"))

    assert reduced.risk_budget_usdt == Decimal("25")
    assert reduced.risk_per_trade_pct == Decimal("0.25")
    assert unchanged is policy
    with pytest.raises(ValueError, match="No portfolio stop-risk capacity"):
        policy_with_remaining_portfolio_risk(policy, Decimal("0"))
