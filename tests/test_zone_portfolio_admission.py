"""Verify independent-zone portfolio stop-risk admission without budget splitting (#455)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from test_zone_runner_protection import _child_stop_order, _make_plan, _open_order

from cautious_crypto_bro.domain import (
    AccountPosition,
    AccountStateSummary,
    Side,
)
from cautious_crypto_bro.zone_ledger import ZoneExecution, ZoneOrder
from cautious_crypto_bro.zone_portfolio_admission import (
    detect_zone_group_manual_drift,
    evaluate_zone_portfolio_admission,
)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_zone_portfolio_admission_enforces_cap_without_splitting_budgets(
    side: Side,
) -> None:
    now = datetime.now(UTC)
    if side is Side.LONG:
        _, plan_a = _make_plan(side, "98", "100", "90")
        _, plan_b = _make_plan(side, "92", "94", "84")
        fill_a = D("100")
        locked_stop_a = D("101")
    else:
        _, plan_a = _make_plan(side, "100", "102", "110")
        _, plan_b = _make_plan(side, "106", "108", "116")
        fill_a = D("100")
        locked_stop_a = D("99")

    link_a1 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e1"
    link_a2 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e2"
    link_a3 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e3"
    q1 = plan_a.entry_orders[0].quantity
    q2 = plan_a.entry_orders[1].quantity
    q3 = plan_a.entry_orders[2].quantity

    orders_a = (
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-e1",
            order_link_id=link_a1,
            role="ENTRY",
            quantity=q1,
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-sl1",
            order_link_id="",
            parent_order_link_id=link_a1,
            role="STOP",
            quantity=q1,
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-e2",
            order_link_id=link_a2,
            role="ENTRY",
            quantity=q2,
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-e3",
            order_link_id=link_a3,
            role="ENTRY",
            quantity=q3,
        ),
    )
    exec_a1 = ZoneExecution(
        execution_id="ex-a1",
        order_id="a-e1",
        side=side,
        quantity=q1,
        closed_quantity=D("0"),
        price=fill_a,
        executed_at_ms=10,
    )
    account_open_a = AccountStateSummary(
        as_of=now,
        positions=(
            AccountPosition(
                symbol="BTCUSDT",
                side=side,
                size=q1,
                avg_price=fill_a,
                mark_price=fill_a,
                unrealised_pnl=D("0"),
                status="Open",
                take_profit=None,
                stop_loss=None,
            ),
            AccountPosition(
                symbol="ETHUSDT",
                side=Side.LONG,
                size=D("2"),
                avg_price=D("2000"),
                mark_price=D("2000"),
                unrealised_pnl=D("0"),
                status="Open",
                take_profit=None,
                stop_loss=D("1975"),
            ),
        ),
        open_orders=(
            _child_stop_order("a-sl1", link_a1, side, q1, plan_a.stop_loss),
            _open_order("a-e2", link_a2, side, q2),
            _open_order("a-e3", link_a3, side, q3),
        ),
    )

    # 1. With a 300 USDT cap, Zone A (~100) + ETHUSDT (50) + Zone B (~100) <= 300: admitted with full budget.
    admitted = evaluate_zone_portfolio_admission(
        plan_b,
        (plan_a,),
        orders_a,
        (exec_a1,),
        account_open_a,
        portfolio_stop_risk_cap_usdt=D("300"),
        configured_risk_per_trade_pct=D("1"),
    )
    assert admitted.admitted is True
    assert admitted.rejection_reason is None
    assert admitted.candidate_risk_budget_usdt == D("100")
    assert admitted.other_symbols_risk_usdt == D("50")
    assert admitted.trading_enabled is False

    # 2. With a 200 USDT cap, Zone A (~100) + ETHUSDT (50) + Zone B (~100) > 200: rejected without shrinking budget.
    rejected = evaluate_zone_portfolio_admission(
        plan_b,
        (plan_a,),
        orders_a,
        (exec_a1,),
        account_open_a,
        portfolio_stop_risk_cap_usdt=D("200"),
        configured_risk_per_trade_pct=D("1"),
    )
    assert rejected.admitted is False
    assert rejected.candidate_risk_budget_usdt == D("100")
    assert (
        rejected.rejection_reason is not None
        and "refusing to split zone budget" in rejected.rejection_reason
    )
    assert rejected.trading_enabled is False

    # 3. Once Zone A freezes entries and locks profit (PartialStopLoss beyond entry average),
    # Zone A contributes 0 open stop risk, so Zone B + ETHUSDT (50) fits inside the 200 USDT cap.
    account_locked_a = AccountStateSummary(
        as_of=now,
        positions=account_open_a.positions,
        open_orders=(_child_stop_order("a-sl1", link_a1, side, q1, locked_stop_a),),
    )
    admitted_after_lock = evaluate_zone_portfolio_admission(
        plan_b,
        (plan_a,),
        orders_a[:2],
        (exec_a1,),
        account_locked_a,
        portfolio_stop_risk_cap_usdt=D("200"),
        configured_risk_per_trade_pct=D("1"),
        protected_stops={plan_a.intent_id: locked_stop_a},
    )
    assert admitted_after_lock.admitted is True
    assert admitted_after_lock.existing_group_risk_usdt == D("0")

    # 4. Splitting Zone B's risk_per_trade_pct to 0.5% (half budget) is rejected explicitly.
    split_policy = plan_b.policy.model_copy(update={"risk_per_trade_pct": D("0.5")})
    split_plan_b = plan_b.model_copy(
        update={"policy": split_policy, "planned_max_loss_usdt": D("45")}
    )
    with pytest.raises(ValueError, match="must not be split across sibling zones"):
        evaluate_zone_portfolio_admission(
            split_plan_b,
            (plan_a,),
            orders_a,
            (exec_a1,),
            account_open_a,
            portfolio_stop_risk_cap_usdt=D("200"),
        )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_detect_zone_group_manual_drift_catches_stop_freeze_and_exit_drift(
    side: Side,
) -> None:
    from cautious_crypto_bro.domain import AccountOrder

    now = datetime.now(UTC)
    if side is Side.LONG:
        _, plan_a = _make_plan(side, "98", "100", "90")
        fill_a = D("100")
        tp1_price = D("105.0")
    else:
        _, plan_a = _make_plan(side, "100", "102", "110")
        fill_a = D("100")
        tp1_price = D("95.0")

    link_a1 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e1"
    link_a2 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e2"
    link_tp1 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-t1r1"
    q1 = plan_a.entry_orders[0].quantity
    q2 = plan_a.entry_orders[1].quantity
    orders = (
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-e1",
            order_link_id=link_a1,
            role="ENTRY",
            quantity=q1,
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-sl1",
            order_link_id="",
            parent_order_link_id=link_a1,
            role="STOP",
            quantity=q1,
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-e2",
            order_link_id=link_a2,
            role="ENTRY",
            quantity=q2,
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-tp1",
            order_link_id=link_tp1,
            role="EXIT",
            quantity=D("1.5"),
        ),
    )
    exec_a1 = ZoneExecution(
        execution_id="ex-a1",
        order_id="a-e1",
        side=side,
        quantity=q1,
        closed_quantity=D("0"),
        price=fill_a,
        executed_at_ms=10,
    )
    exit_side = Side.SHORT if side is Side.LONG else Side.LONG
    tp1_order = AccountOrder(
        symbol="BTCUSDT",
        side=exit_side,
        order_type="Limit",
        status="New",
        quantity=D("1.5"),
        remaining_quantity=D("1.5"),
        price=tp1_price,
        avg_price=None,
        order_id="a-tp1",
        order_link_id=link_tp1,
        reduce_only=True,
        updated_at=now,
    )
    clean_account = AccountStateSummary(
        as_of=now,
        positions=(
            AccountPosition(
                symbol="BTCUSDT",
                side=side,
                size=q1,
                avg_price=fill_a,
                mark_price=fill_a,
                unrealised_pnl=D("0"),
                status="Open",
                take_profit=None,
                stop_loss=None,
            ),
        ),
        open_orders=(
            _child_stop_order("a-sl1", link_a1, side, q1, plan_a.stop_loss),
            _open_order("a-e2", link_a2, side, q2),
            tp1_order,
        ),
    )
    clean = detect_zone_group_manual_drift(
        (plan_a,),
        orders,
        (exec_a1,),
        clean_account,
        expected_exits_by_zone={plan_a.intent_id: {link_tp1: (D("1.5"), tp1_price)}},
    )
    assert clean.drifted is False
    assert clean.pause_reason is None
    assert clean.open_stop_risk_usdt is not None
    assert clean.trading_enabled is False

    # Frozen zone still has open entry order a-e2 -> drifted.
    frozen_drift = detect_zone_group_manual_drift(
        (plan_a,),
        orders,
        (exec_a1,),
        clean_account,
        entry_frozen_zones=frozenset({plan_a.intent_id}),
    )
    assert frozen_drift.drifted is True
    assert (
        frozen_drift.pause_reason is not None
        and "after entry freeze" in frozen_drift.pause_reason
    )

    # TP1 exit order price modified externally -> drifted.
    drifted_exit = detect_zone_group_manual_drift(
        (plan_a,),
        orders,
        (exec_a1,),
        clean_account,
        expected_exits_by_zone={
            plan_a.intent_id: {link_tp1: (D("1.5"), tp1_price + D("1"))}
        },
    )
    assert drifted_exit.drifted is True
    assert (
        drifted_exit.pause_reason is not None
        and "drifted from expected geometry" in drifted_exit.pause_reason
    )

    # Missing child stop -> drifted via zone_group_open_stop_risk_usdt.
    no_stop_account = AccountStateSummary(
        as_of=now,
        positions=clean_account.positions,
        open_orders=(_open_order("a-e2", link_a2, side, q2),),
    )
    stop_drift = detect_zone_group_manual_drift(
        (plan_a,), orders, (exec_a1,), no_stop_account
    )
    assert stop_drift.drifted is True
    assert (
        stop_drift.pause_reason is not None
        and "not covered by verified child stop" in stop_drift.pause_reason
    )
