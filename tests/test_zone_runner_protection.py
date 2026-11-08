"""Verify per-zone runner economics remain isolated from sibling fills (#312)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from cautious_crypto_bro.domain import (
    AccountOrder,
    AccountPosition,
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
from cautious_crypto_bro.portfolio_risk import PortfolioRiskUnavailable
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import ZoneExecution, ZoneOrder, zone_group_id
from cautious_crypto_bro.zone_runner_protection import (
    cancel_zone_pending_entries,
    derive_zone_runner_state,
    evaluate_zone_group_runner_contract,
    expire_zone_stale_entries,
    zone_group_open_stop_risk_usdt,
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


def _child_stop_order(
    order_id: str,
    parent_link_id: str,
    side: Side,
    quantity: Decimal,
    trigger_price: Decimal,
) -> AccountOrder:
    close_side = Side.SHORT if side is Side.LONG else Side.LONG
    return AccountOrder(
        symbol="BTCUSDT",
        side=close_side,
        order_type="Market",
        status="Untriggered",
        quantity=quantity,
        remaining_quantity=quantity,
        price=None,
        avg_price=None,
        order_id=order_id,
        order_link_id="",
        reduce_only=True,
        updated_at=datetime.now(UTC),
        stop_order_type="PartialStopLoss",
        trigger_price=trigger_price,
        parent_order_link_id=parent_link_id,
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_zone_group_open_stop_risk_floors_profit_locked_zone_and_fails_closed(
    side: Side,
) -> None:
    assert zone_group_open_stop_risk_usdt(
        (), (), (), AccountStateSummary(datetime.now(UTC), (), ())
    ) == D("0")
    if side is Side.LONG:
        _, plan_a = _make_plan(side, "98", "100", "90")
        _, plan_b = _make_plan(side, "92", "94", "84")
        fill_a, fill_b = D("100"), D("93")
        locked_stop_a = D("101")
        expected_filled_b = D("4") * (D("93") - plan_b.stop_loss)
    else:
        _, plan_a = _make_plan(side, "100", "102", "110")
        _, plan_b = _make_plan(side, "106", "108", "116")
        fill_a, fill_b = D("100"), D("107")
        locked_stop_a = D("99")
        expected_filled_b = D("4") * (plan_b.stop_loss - D("107"))

    link_a_e1 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e1"
    link_b_e1 = f"ccb-v2-{plan_b.intent_id.hex[:20]}-e1"
    link_b_e2 = f"ccb-v2-{plan_b.intent_id.hex[:20]}-e2"

    orders = (
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-e1",
            order_link_id=link_a_e1,
            role="ENTRY",
            quantity=D("2"),
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-sl",
            order_link_id="",
            parent_order_link_id=link_a_e1,
            role="STOP",
            quantity=D("2"),
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-tp",
            order_link_id="",
            parent_order_link_id=link_a_e1,
            role="EXIT",
            quantity=D("0.5"),
        ),
        ZoneOrder(
            zone_id=plan_b.intent_id,
            order_id="b-e1",
            order_link_id=link_b_e1,
            role="ENTRY",
            quantity=D("4"),
        ),
        ZoneOrder(
            zone_id=plan_b.intent_id,
            order_id="b-sl",
            order_link_id="",
            parent_order_link_id=link_b_e1,
            role="STOP",
            quantity=D("4"),
        ),
        ZoneOrder(
            zone_id=plan_b.intent_id,
            order_id="b-e2",
            order_link_id=link_b_e2,
            role="ENTRY",
            quantity=plan_b.entry_orders[1].quantity,
        ),
    )
    close_side = Side.SHORT if side is Side.LONG else Side.LONG
    executions = (
        ZoneExecution(
            execution_id="ex-a-1",
            order_id="a-e1",
            side=side,
            quantity=D("2"),
            closed_quantity=D("0"),
            price=fill_a,
            executed_at_ms=10,
        ),
        ZoneExecution(
            execution_id="ex-a-tp",
            order_id="a-tp",
            side=close_side,
            quantity=D("0.5"),
            closed_quantity=D("0.5"),
            price=fill_a,
            executed_at_ms=15,
        ),
        ZoneExecution(
            execution_id="ex-b-1",
            order_id="b-e1",
            side=side,
            quantity=D("4"),
            closed_quantity=D("0"),
            price=fill_b,
            executed_at_ms=20,
        ),
    )
    sl_a = _child_stop_order("a-sl", link_a_e1, side, D("1.5"), locked_stop_a)
    sl_b = _child_stop_order("b-sl", link_b_e1, side, D("4"), plan_b.stop_loss)
    pending_b_e2 = AccountOrder(
        symbol="BTCUSDT",
        side=side,
        order_type="Limit",
        status="New",
        quantity=plan_b.entry_orders[1].quantity,
        remaining_quantity=plan_b.entry_orders[1].quantity,
        price=None,
        avg_price=None,
        order_id="b-e2",
        order_link_id=link_b_e2,
        reduce_only=False,
        updated_at=datetime.now(UTC),
    )
    pos = AccountPosition(
        symbol="BTCUSDT",
        side=side,
        size=D("5.5"),
        avg_price=D("95"),
        mark_price=D("100"),
        unrealised_pnl=D("0"),
        status="Open",
        take_profit=None,
        stop_loss=None,
    )
    account = AccountStateSummary(
        as_of=datetime.now(UTC),
        positions=(pos,),
        open_orders=(sl_a, sl_b, pending_b_e2),
    )
    expected_pending_b = plan_b.entry_orders[1].quantity * abs(
        plan_b.entry_orders[1].reference_price - plan_b.stop_loss
    )
    risk = zone_group_open_stop_risk_usdt(
        (plan_a, plan_b),
        orders,
        executions,
        account,
        protected_stops={plan_a.intent_id: locked_stop_a},
    )
    # Zone A's stop is locked in profit past its entry average, so its open stop
    # risk is 0 (never negative) and does not offset Zone B's risk.
    assert risk == expected_filled_b + expected_pending_b

    # Fail-closed error checks
    with pytest.raises(
        PortfolioRiskUnavailable, match="covered by verified child stop"
    ):
        zone_group_open_stop_risk_usdt(
            (plan_a, plan_b),
            orders,
            executions,
            AccountStateSummary(
                as_of=datetime.now(UTC),
                positions=(pos,),
                open_orders=(sl_b, pending_b_e2),
            ),
            protected_stops={plan_a.intent_id: locked_stop_a},
        )
    with pytest.raises(PortfolioRiskUnavailable, match="position quantity mismatch"):
        zone_group_open_stop_risk_usdt(
            (plan_a, plan_b),
            orders,
            executions,
            AccountStateSummary(
                as_of=datetime.now(UTC),
                positions=(replace(pos, size=D("5")),),
                open_orders=(sl_a, sl_b, pending_b_e2),
            ),
            protected_stops={plan_a.intent_id: locked_stop_a},
        )
    with pytest.raises(PortfolioRiskUnavailable, match="Unscoped whole-position"):
        zone_group_open_stop_risk_usdt(
            (plan_a, plan_b),
            orders,
            executions,
            AccountStateSummary(
                as_of=datetime.now(UTC),
                positions=(replace(pos, stop_loss=plan_a.stop_loss),),
                open_orders=(sl_a, sl_b, pending_b_e2),
            ),
            protected_stops={plan_a.intent_id: locked_stop_a},
        )
    with pytest.raises(PortfolioRiskUnavailable, match="Unattributed open order"):
        zone_group_open_stop_risk_usdt(
            (plan_a, plan_b),
            orders,
            executions,
            AccountStateSummary(
                as_of=datetime.now(UTC),
                positions=(pos,),
                open_orders=(
                    sl_a,
                    sl_b,
                    pending_b_e2,
                    _open_order("rogue", "rogue-link", side, D("1")),
                ),
            ),
            protected_stops={plan_a.intent_id: locked_stop_a},
        )


def test_zone_group_open_stop_risk_rejects_corrupt_ledger_or_order_states() -> None:
    _, plan_a = _make_plan(Side.LONG, "98", "100", "90")
    _, plan_short = _make_plan(Side.SHORT, "100", "102", "110")
    empty_account = AccountStateSummary(datetime.now(UTC), (), ())

    with pytest.raises(PortfolioRiskUnavailable, match="share symbol and side"):
        zone_group_open_stop_risk_usdt((plan_a, plan_short), (), (), empty_account)

    link_a = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e1"
    entry_a = ZoneOrder(
        zone_id=plan_a.intent_id,
        order_id="a-e1",
        order_link_id=link_a,
        role="ENTRY",
        quantity=D("1"),
    )
    stop_a = ZoneOrder(
        zone_id=plan_a.intent_id,
        order_id="a-sl",
        order_link_id="",
        parent_order_link_id=link_a,
        role="STOP",
        quantity=D("1"),
    )
    orphan_exec = ZoneExecution(
        execution_id="orphan",
        order_id="missing",
        side=Side.LONG,
        quantity=D("1"),
        closed_quantity=D("0"),
        price=D("100"),
        executed_at_ms=10,
    )
    with pytest.raises(PortfolioRiskUnavailable, match="no bound zone order"):
        zone_group_open_stop_risk_usdt(
            (plan_a,), (entry_a,), (orphan_exec,), empty_account
        )

    overfill = orphan_exec.model_copy(update={"order_id": "a-e1", "quantity": D("2")})
    with pytest.raises(PortfolioRiskUnavailable, match="overfilled"):
        zone_group_open_stop_risk_usdt(
            (plan_a,), (entry_a,), (overfill,), empty_account
        )

    overclose = ZoneExecution(
        execution_id="oc",
        order_id="a-sl",
        side=Side.SHORT,
        quantity=D("1"),
        closed_quantity=D("1"),
        price=D("90"),
        executed_at_ms=20,
    )
    with pytest.raises(PortfolioRiskUnavailable, match="overclosed"):
        zone_group_open_stop_risk_usdt(
            (plan_a,), (entry_a, stop_a), (overclose,), empty_account
        )

    unscoped_exit = ZoneOrder(
        zone_id=plan_a.intent_id,
        order_id="a-exit-unscoped",
        order_link_id="custom-exit",
        role="EXIT",
        quantity=D("1"),
    )
    with pytest.raises(PortfolioRiskUnavailable, match="lacks a same-zone parent"):
        zone_group_open_stop_risk_usdt(
            (plan_a,),
            (entry_a, unscoped_exit),
            (overclose.model_copy(update={"order_id": "a-exit-unscoped"}),),
            empty_account,
        )

    valid_fill = orphan_exec.model_copy(update={"order_id": "a-e1"})
    bad_sl = replace(
        _child_stop_order("a-sl", link_a, Side.LONG, D("1"), D("89")),
    )
    with pytest.raises(PortfolioRiskUnavailable, match="Invalid native child stop"):
        zone_group_open_stop_risk_usdt(
            (plan_a,),
            (entry_a, stop_a),
            (valid_fill,),
            AccountStateSummary(datetime.now(UTC), (), (bad_sl,)),
        )

    # Flat group with a bad pending entry order binding or missing price
    bad_open_entry = _open_order("a-e1", "wrong-link", Side.LONG, D("1"))
    with pytest.raises(PortfolioRiskUnavailable, match="conflicts with zone binding"):
        zone_group_open_stop_risk_usdt(
            (plan_a,),
            (entry_a,),
            (),
            AccountStateSummary(datetime.now(UTC), (), (bad_open_entry,)),
        )
    unknown_name_entry = ZoneOrder(
        zone_id=plan_a.intent_id,
        order_id="a-e9",
        order_link_id=f"ccb-v2-{plan_a.intent_id.hex[:20]}-e9",
        role="ENTRY",
        quantity=D("1"),
    )
    priceless_open = replace(
        _open_order("a-e9", unknown_name_entry.order_link_id, Side.LONG, D("1")),
        price=None,
    )
    with pytest.raises(PortfolioRiskUnavailable, match="no measurable price"):
        zone_group_open_stop_risk_usdt(
            (plan_a,),
            (unknown_name_entry,),
            (),
            AccountStateSummary(datetime.now(UTC), (), (priceless_open,)),
        )
    with pytest.raises(PortfolioRiskUnavailable, match="unknown zone plan"):
        zone_group_open_stop_risk_usdt(
            (plan_short.model_copy(update={"side": Side.LONG}),),
            (entry_a,),
            (),
            AccountStateSummary(
                datetime.now(UTC),
                (),
                (_open_order("a-e1", link_a, Side.LONG, D("1")),),
            ),
        )
    with pytest.raises(PortfolioRiskUnavailable, match="side or count mismatch"):
        zone_group_open_stop_risk_usdt(
            (plan_a,),
            (entry_a,),
            (),
            AccountStateSummary(
                datetime.now(UTC),
                (
                    AccountPosition(
                        symbol="BTCUSDT",
                        side=Side.SHORT,
                        size=D("1"),
                        avg_price=D("100"),
                        mark_price=D("100"),
                        unrealised_pnl=D("0"),
                        status="Open",
                        take_profit=None,
                        stop_loss=None,
                    ),
                ),
                (),
            ),
        )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_zone_group_runner_contract_handles_late_fills_and_flags_same_account_partial_trailing(
    side: Side,
) -> None:
    if side is Side.LONG:
        _, plan_a = _make_plan(
            side, "98", "100", "90", activation_r="0.5", trail_r="0.2", locked_r="0.1"
        )
        _, plan_b = _make_plan(
            side, "92", "94", "84", activation_r="1.0", trail_r="0.3", locked_r="0.2"
        )
        fill_a1, fill_a2_late, fill_b1 = D("100"), D("99"), D("93")
        mark_pre_trail = D("101")
        mark_trail_a = D("105")
    else:
        _, plan_a = _make_plan(
            side, "100", "102", "110", activation_r="0.5", trail_r="0.2", locked_r="0.1"
        )
        _, plan_b = _make_plan(
            side, "106", "108", "116", activation_r="1.0", trail_r="0.3", locked_r="0.2"
        )
        fill_a1, fill_a2_late, fill_b1 = D("100"), D("101"), D("107")
        mark_pre_trail = D("99")
        mark_trail_a = D("95")

    orders = (
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-e1",
            order_link_id=f"ccb-v2-{plan_a.intent_id.hex[:20]}-e1",
            role="ENTRY",
            quantity=D("2"),
        ),
        ZoneOrder(
            zone_id=plan_a.intent_id,
            order_id="a-e2",
            order_link_id=f"ccb-v2-{plan_a.intent_id.hex[:20]}-e2",
            role="ENTRY",
            quantity=D("2"),
        ),
        ZoneOrder(
            zone_id=plan_b.intent_id,
            order_id="b-e1",
            order_link_id=f"ccb-v2-{plan_b.intent_id.hex[:20]}-e1",
            role="ENTRY",
            quantity=D("3"),
        ),
    )
    exec_a1 = ZoneExecution(
        execution_id="ex-a1",
        order_id="a-e1",
        side=side,
        quantity=D("2"),
        closed_quantity=D("0"),
        price=fill_a1,
        executed_at_ms=10,
    )
    # 1. Only Zone A is active: compatible with single-position Full trailing.
    single_contract = evaluate_zone_group_runner_contract(
        (plan_a, plan_b),
        orders,
        (exec_a1,),
        mark_price=mark_trail_a,
        context=CONTEXT,
    )
    assert single_contract.active_zone_count == 1
    assert single_contract.trailing_zone_count == 1
    assert (
        single_contract.native_trailing_status
        == "SINGLE_ACTIVE_ZONE_FULL_TRAILING_COMPATIBLE"
    )
    assert single_contract.operator_tradeoff_required is False
    assert single_contract.protection_verified is False
    assert single_contract.trading_enabled is False

    # 2. Both zones filled, neither trailing yet: pre-trail partial-stop only.
    exec_b1 = ZoneExecution(
        execution_id="ex-b1",
        order_id="b-e1",
        side=side,
        quantity=D("3"),
        closed_quantity=D("0"),
        price=fill_b1,
        executed_at_ms=20,
    )
    pre_trail = evaluate_zone_group_runner_contract(
        (plan_a, plan_b),
        orders,
        (exec_a1, exec_b1),
        mark_price=mark_pre_trail,
        context=CONTEXT,
    )
    assert pre_trail.active_zone_count == 2
    assert pre_trail.trailing_zone_count == 0
    assert pre_trail.native_trailing_status == "MULTI_ZONE_PRE_TRAIL_PARTIAL_STOP_ONLY"
    assert pre_trail.operator_tradeoff_required is False

    # 3. Late fill arrives on Zone A's E2 while Zone A is already trailing and
    # Zone B is also active: Zone A's economics update from its own fills while
    # Zone B is unaffected, and the contract flags same-account partial trailing.
    exec_a2_late = ZoneExecution(
        execution_id="ex-a2-late",
        order_id="a-e2",
        side=side,
        quantity=D("2"),
        closed_quantity=D("0"),
        price=fill_a2_late,
        executed_at_ms=30,
    )
    initial_stop_a = single_contract.states[0].protected_stop_loss
    assert initial_stop_a is not None
    multi_trail = evaluate_zone_group_runner_contract(
        (plan_a, plan_b),
        orders,
        (exec_a1, exec_b1, exec_a2_late),
        mark_price=mark_trail_a,
        context=CONTEXT,
        previous_protected_stops={plan_a.intent_id: initial_stop_a},
        already_trailing_zones=frozenset({plan_a.intent_id}),
    )
    assert multi_trail.active_zone_count == 2
    assert multi_trail.trailing_zone_count == 2
    assert (
        multi_trail.native_trailing_status
        == "UNSUPPORTED_SAME_ACCOUNT_PARTIAL_TRAILING"
    )
    assert multi_trail.operator_tradeoff_required is True
    assert multi_trail.states[0].remaining_quantity == D("4")
    assert multi_trail.states[0].protected_stop_loss == initial_stop_a
    assert multi_trail.states[1].remaining_quantity == D("3")
    assert multi_trail.protection_verified is False
    assert multi_trail.trading_enabled is False

    with pytest.raises(ValueError, match="at least one plan"):
        evaluate_zone_group_runner_contract(
            (), orders, (), mark_price=mark_trail_a, context=CONTEXT
        )
    wrong_sym_plan = plan_a.model_copy(update={"symbol": "ETHUSDT"})
    with pytest.raises(ValueError, match="share symbol and side"):
        evaluate_zone_group_runner_contract(
            (plan_a, wrong_sym_plan),
            orders,
            (),
            mark_price=mark_trail_a,
            context=CONTEXT,
        )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_expire_zone_stale_entries_isolates_older_zone_from_younger_sibling(
    tmp_path, side: Side
) -> None:
    from datetime import timedelta

    async def run() -> None:
        store = IntentStore(tmp_path / "zone-ttl.db")
        await store.initialize()
        if side is Side.LONG:
            intent_a, plan_a = _make_plan(side, "98", "100", "90")
            intent_b, plan_b = _make_plan(side, "92", "94", "84")
        else:
            intent_a, plan_a = _make_plan(side, "100", "102", "110")
            intent_b, plan_b = _make_plan(side, "106", "108", "116")

        now = datetime.now(UTC)
        plan_a = plan_a.model_copy(update={"created_at": now - timedelta(minutes=250)})
        plan_b = plan_b.model_copy(update={"created_at": now - timedelta(minutes=60)})

        claim = await store.claim_source(intent_a.source, lease_seconds=300)
        assert claim is not None
        assert await store.create_signal_batch_and_complete_source(
            ((intent_a, plan_a), (intent_b, plan_b)), (), claim
        )
        group = zone_group_id("BTCUSDT", side)
        await store.register_zone(group, plan_a.intent_id)
        await store.register_zone(group, plan_b.intent_id)

        link_a_e1 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e1"
        link_a_e2 = f"ccb-v2-{plan_a.intent_id.hex[:20]}-e2"
        link_b_e1 = f"ccb-v2-{plan_b.intent_id.hex[:20]}-e1"

        for z_order in (
            ZoneOrder(
                zone_id=plan_a.intent_id,
                order_id="a-e1",
                order_link_id=link_a_e1,
                role="ENTRY",
                quantity=plan_a.entry_orders[0].quantity,
            ),
            ZoneOrder(
                zone_id=plan_a.intent_id,
                order_id="a-e2",
                order_link_id=link_a_e2,
                role="ENTRY",
                quantity=plan_a.entry_orders[1].quantity,
            ),
            ZoneOrder(
                zone_id=plan_b.intent_id,
                order_id="b-e1",
                order_link_id=link_b_e1,
                role="ENTRY",
                quantity=plan_b.entry_orders[0].quantity,
            ),
        ):
            await store.bind_zone_order(z_order)

        open_a_e2 = _open_order(
            "a-e2", link_a_e2, side, plan_a.entry_orders[1].quantity
        )
        open_b_e1 = _open_order(
            "b-e1", link_b_e1, side, plan_b.entry_orders[0].quantity
        )

        # Younger Zone B has not reached its 240m TTL: no cancellation occurs.
        gw = _ZoneCancelGateway([open_a_e2, open_b_e1])
        not_due = await expire_zone_stale_entries(
            plan_b,
            (),
            store,
            gw,  # type: ignore[arg-type]
            legacy_entry_ttl_minutes=240,
            now=now,
        )
        assert not_due.expired is False
        assert not_due.cancelled_order_ids == ()
        assert len(gw.open_orders) == 2

        # Older Zone A with zero fills expires and closes (zone_closed=True)
        # while Zone B's resting E1 remains open.
        unfilled_exp = await expire_zone_stale_entries(
            plan_a,
            (),
            store,
            gw,  # type: ignore[arg-type]
            legacy_entry_ttl_minutes=240,
            now=now,
        )
        assert unfilled_exp.expired is True
        assert unfilled_exp.cancelled_order_ids == ("a-e2",)
        assert unfilled_exp.entry_frozen is True
        assert unfilled_exp.zone_closed is True
        assert [o.order_id for o in gw.open_orders] == ["b-e1"]

        # Older Zone A with a partial fill on E1 freezes entries without closing
        # its filled allocation (zone_closed=False).
        gw_partial = _ZoneCancelGateway([open_a_e2, open_b_e1])
        fill_a_e1 = ZoneExecution(
            execution_id="ex-a-e1",
            order_id="a-e1",
            side=side,
            quantity=plan_a.entry_orders[0].quantity,
            closed_quantity=D("0"),
            price=D("100"),
            executed_at_ms=10,
        )
        partial_exp = await expire_zone_stale_entries(
            plan_a,
            (fill_a_e1,),
            store,
            gw_partial,  # type: ignore[arg-type]
            legacy_entry_ttl_minutes=240,
            now=now,
        )
        assert partial_exp.expired is True
        assert partial_exp.entry_frozen is True
        assert partial_exp.zone_closed is False
        assert [o.order_id for o in gw_partial.open_orders] == ["b-e1"]

        with pytest.raises(ValueError, match="must not be negative"):
            await expire_zone_stale_entries(
                plan_a,
                (),
                store,
                gw_partial,  # type: ignore[arg-type]
                legacy_entry_ttl_minutes=-1,
                now=now,
            )

    asyncio.run(run())
