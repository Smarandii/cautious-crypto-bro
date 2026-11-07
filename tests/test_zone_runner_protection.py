"""Verify per-zone runner economics remain isolated from sibling fills (#312)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from cautious_crypto_bro.domain import (
    AccountOrder,
    AccountStateSummary,
    Entry,
    EntryType,
    ExecutionPolicy,
    InstrumentContext,
    Side,
    SourceMessage,
    StrategyV2Policy,
    TradeExecutionError,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import ZoneExecution, ZoneOrder, zone_group_id
from cautious_crypto_bro.zone_runner_protection import (
    cancel_zone_pending_entries,
    derive_zone_runner_state,
    zone_pending_entry_orders,
)

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


def _open_order(
    order_id: str,
    link_id: str,
    side: Side,
    quantity: Decimal,
    *,
    symbol: str = "BTCUSDT",
    reduce_only: bool = False,
) -> AccountOrder:
    now = datetime.now(UTC)
    return AccountOrder(
        symbol=symbol,
        side=side,
        order_type="Limit",
        status="New",
        quantity=quantity,
        remaining_quantity=quantity,
        price=D("95"),
        avg_price=None,
        order_id=order_id,
        order_link_id=link_id,
        reduce_only=reduce_only,
        updated_at=now,
    )


class _ZoneCancelGateway:
    def __init__(
        self,
        open_orders: list[AccountOrder],
        recovered_by_link: dict[str, AccountOrder] | None = None,
        *,
        ignore_cancel: bool = False,
    ) -> None:
        self.open_orders = list(open_orders)
        self.recovered_by_link = dict(recovered_by_link or {})
        self.ignore_cancel = ignore_cancel
        self.cancelled_ids: list[str] = []

    async def strategy_order(self, symbol: str, order_link_id: str):
        assert symbol == "BTCUSDT"
        return self.recovered_by_link.get(order_link_id)

    async def account_state(self) -> AccountStateSummary:
        return AccountStateSummary(
            as_of=datetime.now(UTC),
            positions=(),
            open_orders=tuple(self.open_orders),
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        assert symbol == "BTCUSDT"
        self.cancelled_ids.append(order_id)
        if not self.ignore_cancel:
            self.open_orders = [o for o in self.open_orders if o.order_id != order_id]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_cancel_zone_pending_entries_preserves_sibling_and_recovers_lost_ack(
    tmp_path, side: Side
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "zone-cancel.db")
        await store.initialize()
        if side is Side.LONG:
            intent_a, plan_a = _make_plan(side, "98", "100", "90")
            intent_b, plan_b = _make_plan(side, "92", "94", "84")
        else:
            intent_a, plan_a = _make_plan(side, "100", "102", "110")
            intent_b, plan_b = _make_plan(side, "106", "108", "116")

        claim = await store.claim_source(intent_a.source, lease_seconds=300)
        assert claim is not None
        assert await store.create_signal_batch_and_complete_source(
            ((intent_a, plan_a), (intent_b, plan_b)), (), claim
        )
        group = zone_group_id("BTCUSDT", side)
        await store.register_zone(group, plan_a.intent_id)
        await store.register_zone(group, plan_b.intent_id)

        prefix_a = f"ccb-v2-{plan_a.intent_id.hex[:20]}-"
        prefix_b = f"ccb-v2-{plan_b.intent_id.hex[:20]}-"
        link_a_e2 = prefix_a + "e2"
        link_a_e3 = prefix_a + "e3"
        link_b_e1 = prefix_b + "e1"

        # Bind E2 for zone A and E1 for sibling zone B; leave zone A's E3 unbound
        # to simulate a lost submission acknowledgement that must be recovered first.
        await store.bind_zone_order(
            ZoneOrder(
                zone_id=plan_a.intent_id,
                order_id="ord-a-e2",
                order_link_id=link_a_e2,
                role="ENTRY",
                quantity=plan_a.entry_orders[1].quantity,
            )
        )
        await store.bind_zone_order(
            ZoneOrder(
                zone_id=plan_b.intent_id,
                order_id="ord-b-e1",
                order_link_id=link_b_e1,
                role="ENTRY",
                quantity=plan_b.entry_orders[0].quantity,
            )
        )

        open_a_e2 = _open_order(
            "ord-a-e2", link_a_e2, side, plan_a.entry_orders[1].quantity
        )
        open_a_e3 = _open_order(
            "ord-a-e3", link_a_e3, side, plan_a.entry_orders[2].quantity
        )
        open_b_e1 = _open_order(
            "ord-b-e1", link_b_e1, side, plan_b.entry_orders[0].quantity
        )
        other_sym = _open_order("ord-eth", "eth-link", side, D("1"), symbol="ETHUSDT")
        gateway = _ZoneCancelGateway(
            [open_a_e2, open_a_e3, open_b_e1, other_sym],
            recovered_by_link={link_a_e3: open_a_e3},
        )

        account, cancelled = await cancel_zone_pending_entries(
            plan_a,
            store,
            gateway,  # type: ignore[arg-type]
        )
        assert cancelled == ("ord-a-e2", "ord-a-e3")
        assert [o.order_id for o in account.open_orders] == ["ord-b-e1", "ord-eth"]

        # Unconfirmed cancellation fails closed.
        stuck_gateway = _ZoneCancelGateway([open_a_e2, open_b_e1], ignore_cancel=True)
        with pytest.raises(TradeExecutionError, match="not confirmed"):
            await cancel_zone_pending_entries(
                plan_a,
                store,
                stuck_gateway,  # type: ignore[arg-type]
            )

        # Unbound prefix order or mismatched binding fails closed.
        orders = await store.zone_bound_orders(group)
        unbound_e1 = _open_order(
            "ord-a-e1-unbound",
            prefix_a + "e1",
            side,
            plan_a.entry_orders[0].quantity,
        )
        with pytest.raises(TradeExecutionError, match="Unbound or mismatched"):
            zone_pending_entry_orders(plan_a, orders, (unbound_e1,))

        mismatched = _open_order(
            "ord-a-e2",
            "wrong-link",
            side,
            plan_a.entry_orders[1].quantity,
        )
        with pytest.raises(TradeExecutionError, match="conflicts with zone binding"):
            zone_pending_entry_orders(plan_a, orders, (mismatched,))

    asyncio.run(run())
