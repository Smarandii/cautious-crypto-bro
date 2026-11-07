"""Verify per-zone runner economics remain isolated from sibling fills (#312)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionPolicy,
    InstrumentContext,
    Side,
    SourceMessage,
    StrategyV2Policy,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import ZoneExecution, ZoneOrder, zone_group_id
from cautious_crypto_bro.zone_runner_protection import derive_zone_runner_state

D = Decimal
CONTEXT = InstrumentContext(
    market_price=D("100"),
    tick_size=D("0.1"),
    qty_step=D("0.01"),
    min_qty=D("0.01"),
    min_notional=D("5"),
)


def _make_plan(
    side: Side,
    low: str,
    high: str,
    stop: str,
    *,
    activation_r: str = "0.5",
    trail_r: str = "0.2",
    locked_r: str = "0.1",
):
    now = datetime.now(UTC)
    intent = TradingIntent(
        intent_id=uuid4(),
        source=SourceMessage(
            channel_id=-1001,
            channel_title="Test",
            message_id=1,
            published_at=now,
            received_at=now,
            text="Synthetic zone",
        ),
        symbol="BTCUSDT",
        side=side,
        entry=Entry(type=EntryType.RANGE, range_low=float(low), range_high=float(high)),
        stop_loss=float(stop),
        summary="Zone plan",
        confidence=1.0,
    )
    policy = ExecutionPolicy(
        trading_capital_usdt=D("10000"),
        risk_per_trade_pct=D("1"),
        strategy_v2=StrategyV2Policy(
            trailing_activation_r=D(activation_r),
            trailing_distance_r=D(trail_r),
            minimum_locked_profit_r=D(locked_r),
        ),
    )
    quote = D("105") if side is Side.LONG else D("85")
    return intent, ExecutionPlanner().plan(
        intent, policy, replace(CONTEXT, market_price=quote)
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_unequal_sibling_zones_preserve_independent_runner_economics_across_restart(
    tmp_path, side: Side
) -> None:
    async def run() -> None:
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        if side is Side.LONG:
            intent_a, plan_a = _make_plan(
                side,
                "98",
                "100",
                "90",
                activation_r="0.5",
                trail_r="0.2",
                locked_r="0.1",
            )
            intent_b, plan_b = _make_plan(
                side,
                "92",
                "94",
                "84",
                activation_r="1.5",
                trail_r="0.3",
                locked_r="0.2",
            )
            fill_price_a, fill_price_b = D("100"), D("93")
            mark_activate_a_only = D("105")
            mark_activate_both = D("109")
        else:
            intent_a, plan_a = _make_plan(
                side,
                "100",
                "102",
                "110",
                activation_r="0.5",
                trail_r="0.2",
                locked_r="0.1",
            )
            intent_b, plan_b = _make_plan(
                side,
                "106",
                "108",
                "116",
                activation_r="1.5",
                trail_r="0.3",
                locked_r="0.2",
            )
            fill_price_a, fill_price_b = D("100"), D("107")
            mark_activate_a_only = D("95")
            mark_activate_both = D("91")

        claim = await store.claim_source(intent_a.source, lease_seconds=300)
        assert claim is not None
        assert await store.create_signal_batch_and_complete_source(
            ((intent_a, plan_a), (intent_b, plan_b)), (), claim
        )
        group = zone_group_id("BTCUSDT", side)
        await store.register_zone(group, plan_a.intent_id)
        await store.register_zone(group, plan_b.intent_id)

        entry_a = ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="entry-a",
            order_link_id="link-entry-a",
            role="ENTRY",
            quantity=D("2"),
        )
        exit_a = ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="exit-a",
            order_link_id="",
            parent_order_link_id="link-entry-a",
            role="EXIT",
            quantity=D("0.5"),
        )
        entry_b = ZoneOrder(
            zone_id=plan_b.intent_id,
            order_id="entry-b",
            order_link_id="link-entry-b",
            role="ENTRY",
            quantity=D("5"),
        )
        for order in (entry_a, exit_a, entry_b):
            await store.bind_zone_order(order)

        exec_a = ZoneExecution(
            execution_id="exec-a-1",
            order_id="entry-a",
            side=side,
            quantity=D("2"),
            closed_quantity=D("0"),
            price=fill_price_a,
            fee=D("0.2"),
            executed_at_ms=10,
        )
        await store.record_zone_execution(exec_a)
        orders = await store.zone_bound_orders(group)

        before_sibling = derive_zone_runner_state(
            plan_a,
            orders,
            (exec_a,),
            mark_price=mark_activate_a_only,
            context=CONTEXT,
        )
        assert before_sibling.trailing_active is True
        assert before_sibling.live_r == D("0.5")

        # Sibling zone B fills a larger quantity at a different price, shifting
        # the net exchange average. Zone A's runner state must not change.
        exec_b = ZoneExecution(
            execution_id="exec-b-1",
            order_id="entry-b",
            side=side,
            quantity=D("4"),
            closed_quantity=D("0"),
            price=fill_price_b,
            fee=D("0.4"),
            executed_at_ms=20,
        )
        await store.record_zone_execution(exec_b)
        after_sibling = derive_zone_runner_state(
            plan_a,
            orders,
            (exec_a, exec_b),
            mark_price=mark_activate_a_only,
            context=CONTEXT,
        )
        state_b = derive_zone_runner_state(
            plan_b,
            orders,
            (exec_a, exec_b),
            mark_price=mark_activate_a_only,
            context=CONTEXT,
        )
        assert after_sibling == before_sibling
        assert state_b.trailing_active is False
        assert state_b.protected_stop_loss == plan_b.stop_loss
        assert state_b.trailing_distance is None

        # Partial close on zone A reduces remaining quantity while preserving its
        # activated trail and ratcheted stop across restart.
        close_side = Side.SHORT if side is Side.LONG else Side.LONG
        exec_close_a = ZoneExecution(
            execution_id="exec-a-close",
            order_id="exit-a",
            side=close_side,
            quantity=D("0.5"),
            closed_quantity=D("0.5"),
            price=mark_activate_a_only,
            fee=D("0"),
            executed_at_ms=30,
        )
        await store.record_zone_execution(exec_close_a)

        reopened = IntentStore(database)
        await reopened.initialize()
        reloaded_orders = await reopened.zone_bound_orders(group)
        both_active_a = derive_zone_runner_state(
            plan_a,
            reloaded_orders,
            (exec_a, exec_b, exec_close_a),
            mark_price=mark_activate_both,
            context=CONTEXT,
            previous_protected_stop=after_sibling.protected_stop_loss,
            already_trailing=True,
        )
        both_active_b = derive_zone_runner_state(
            plan_b,
            reloaded_orders,
            (exec_a, exec_b, exec_close_a),
            mark_price=mark_activate_both,
            context=CONTEXT,
        )
        assert both_active_a.remaining_quantity == D("1.5")
        assert both_active_a.trailing_active is True
        assert both_active_b.remaining_quantity == D("4")
        assert both_active_b.trailing_active is True
        assert both_active_a.protected_stop_loss != both_active_b.protected_stop_loss
        assert both_active_a.trailing_distance != both_active_b.trailing_distance

    asyncio.run(run())


def test_zone_runner_state_handles_unfilled_flat_and_invalid_states() -> None:
    _, plan = _make_plan(Side.LONG, "98", "100", "90")
    order = ZoneOrder(
        zone_id=plan.intent_id,
        order_id="entry-1",
        order_link_id="link-1",
        role="ENTRY",
        quantity=D("1"),
    )
    exit_order = ZoneOrder(
        zone_id=plan.intent_id,
        order_id="exit-1",
        order_link_id="",
        parent_order_link_id="link-1",
        role="EXIT",
        quantity=D("1"),
    )
    unfilled = derive_zone_runner_state(
        plan, (order,), (), mark_price=D("100"), context=CONTEXT
    )
    assert unfilled.remaining_quantity == D("0")
    assert unfilled.entry_average_price is None
    assert unfilled.protected_stop_loss is None

    entry_fill = ZoneExecution(
        execution_id="e1",
        order_id="entry-1",
        side=Side.LONG,
        quantity=D("1"),
        closed_quantity=D("0"),
        price=D("100"),
        executed_at_ms=10,
    )
    close_fill = ZoneExecution(
        execution_id="c1",
        order_id="exit-1",
        side=Side.SHORT,
        quantity=D("1"),
        closed_quantity=D("1"),
        price=D("102"),
        executed_at_ms=20,
    )
    flat = derive_zone_runner_state(
        plan,
        (order, exit_order),
        (entry_fill, close_fill),
        mark_price=D("102"),
        context=CONTEXT,
    )
    assert flat.remaining_quantity == D("0")
    assert flat.entry_average_price == D("100")
    assert flat.protected_stop_loss is None

    with pytest.raises(ValueError, match="positive mark price"):
        derive_zone_runner_state(
            plan, (order,), (entry_fill,), mark_price=D("0"), context=CONTEXT
        )
    with pytest.raises(ValueError, match="exceeds entered quantity"):
        derive_zone_runner_state(
            plan,
            (order, exit_order),
            (close_fill,),
            mark_price=D("100"),
            context=CONTEXT,
        )
    bad_fill = entry_fill.model_copy(update={"price": D("85")})
    with pytest.raises(ValueError, match="conflicts with frozen stop geometry"):
        derive_zone_runner_state(
            plan, (order,), (bad_fill,), mark_price=D("100"), context=CONTEXT
        )
