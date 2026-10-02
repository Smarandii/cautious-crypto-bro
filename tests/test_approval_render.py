from dataclasses import replace
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

import pytest

from cautious_crypto_bro.approval_presenter import (
    render,
    render_account_state,
    render_position_protection,
)
from cautious_crypto_bro.domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    Entry,
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    PlannedOrder,
    Side,
    SourceMessage,
    TradingIntent,
)


@pytest.mark.parametrize("leverage", [None, 20])
def test_render_contains_execution_policy(leverage) -> None:
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=-100123,
            channel_title="Trader & Co",
            channel_username="trader",
            message_id=99,
            published_at=datetime(
                2026,
                9,
                12,
                18,
                0,
                tzinfo=UTC,
            ),
            received_at=datetime(
                2026,
                9,
                12,
                18,
                0,
                1,
                tzinfo=UTC,
            ),
            text="signal",
        ),
        symbol="ETHUSDT",
        side=Side.LONG,
        entry=Entry(
            type=EntryType.RANGE,
            range_low=4000,
            range_high=4020,
        ),
        stop_loss=3900,
        take_profit=4300,
        leverage=leverage,
        summary=("Bounce from support."),
        confidence=0.9,
    )

    policy = ExecutionPolicy(
        trading_capital_usdt=(Decimal("6800")),
        risk_per_trade_pct=(Decimal("1")),
        range_order_count=3,
    )

    plan = ExecutionPlan(
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        side=intent.side,
        leverage=Decimal(leverage if leverage is not None else 10),
        orders=(
            PlannedOrder(
                order_type=(ExecutionOrderType.LIMIT),
                quantity=Decimal("0.2"),
                price=Decimal("4000"),
                reference_price=(Decimal("4000")),
            ),
            PlannedOrder(
                order_type=(ExecutionOrderType.LIMIT),
                quantity=Decimal("0.2"),
                price=Decimal("4010"),
                reference_price=(Decimal("4010")),
            ),
            PlannedOrder(
                order_type=(ExecutionOrderType.LIMIT),
                quantity=Decimal("0.2"),
                price=Decimal("4020"),
                reference_price=(Decimal("4020")),
            ),
        ),
        stop_loss=Decimal("3900"),
        take_profit=Decimal("4300"),
        policy=policy,
        planned_max_loss_usdt=(Decimal("66")),
    )

    rendered = render(
        intent,
        plan,
    )

    assert "LONG ETHUSDT" in rendered
    assert "Orders: 3 · total 0.6" in rendered
    assert "Risk: <b>≤ 66 USDT</b> (1% policy)" in rendered
    assert (
        "Leverage: <b>20x</b> (Trader)"
        if leverage is not None
        else "Leverage: <b>10x</b> (Default)"
    ) in rendered
    assert "R:R:" in rendered
    assert "Open source message" in rendered
    assert "Trader &amp; Co" in rendered
    assert "Confidence:" not in rendered
    assert "Published:" not in rendered


def test_account_card_shows_v2_take_profits_and_position_trailing_stop() -> None:
    now = datetime.now(UTC)
    position = AccountPosition(
        symbol="NEARUSDT",
        side=Side.SHORT,
        size=Decimal("109.7"),
        avg_price=Decimal("5.09"),
        mark_price=Decimal("4.9"),
        unrealised_pnl=Decimal("20"),
        status="Normal",
        take_profit=None,
        stop_loss=Decimal("5.144"),
        trailing_stop=Decimal("0.148"),
    )
    exit_order = AccountOrder(
        symbol="NEARUSDT",
        side=Side.LONG,
        order_type="Limit",
        status="New",
        quantity=Decimal("36.5"),
        remaining_quantity=Decimal("36.5"),
        price=Decimal("4.5"),
        avg_price=None,
        order_id="tp2",
        order_link_id="ccb-v2-01f16f999d7c42658f44-t2r3",
        reduce_only=True,
        updated_at=now,
    )
    exits = (
        exit_order,
        replace(
            exit_order,
            order_id="tp3",
            order_link_id="ccb-v2-01f16f999d7c42658f44-t3r3",
            price=Decimal("4.2"),
            status="PartiallyFilled",
        ),
    )
    rendered = render_account_state(
        AccountStateSummary(as_of=now, positions=(position,), open_orders=exits)
    )
    assert "Protection: SL 5.144 · TP 4.2 / 4.5 · Trailing stop active" in rendered

    # V2 TP ladder orders count as protective, not as reduce/close orders.
    assert (
        "Pending orders: <b>0</b> entry · <b>2</b> protective · <b>0</b> reduce"
        in rendered
    )

    # Genuine reduce-only close orders stay in the reduce/close bucket.
    closing = replace(
        exit_order,
        order_id="close",
        order_link_id="ccb-action-0123456789abcdef01234567",
        order_type="Market",
        price=None,
    )
    counted = render_account_state(
        AccountStateSummary(
            as_of=now,
            positions=(position,),
            open_orders=(*exits, closing),
        )
    )
    assert (
        "Pending orders: <b>0</b> entry · <b>2</b> protective · <b>1</b> reduce"
        in counted
    )

    # Long positions also use opposite-side reduce-only limits.
    long_position = replace(position, side=Side.LONG)
    long_exit = replace(exit_order, side=Side.SHORT, price=Decimal("6"))
    assert "TP 6" in render_position_protection(long_position, (long_exit,))

    # Orders unrelated to the live V2 TP ladder must not imply protection.
    for changes in (
        {"symbol": "BTCUSDT"},
        {"side": Side.SHORT},
        {"order_type": "Market"},
        {"reduce_only": False},
        {"order_link_id": "ccb-v2-01f16f999d7c42658f44-e1"},
        {"order_link_id": "manual-t2r3"},
        {"status": "Cancelled"},
        {"status": "Filled"},
        {"remaining_quantity": Decimal("0")},
        {"price": None},
    ):
        assert "TP —" in render_position_protection(
            position, (replace(exit_order, **changes),)
        ), changes

    trailing_only = replace(position, stop_loss=None)
    assert render_position_protection(trailing_only, ()) == (
        "Protection: SL — · TP — · Trailing stop active"
    )
    for trailing in (None, Decimal("0")):
        assert (
            render_position_protection(
                replace(trailing_only, trailing_stop=trailing), ()
            )
            == "⚠️ No SL/TP protection detected"
        )

    # Existing attached TP/SL and order-derived trailing presentation still works.
    attached = replace(exit_order, order_link_id="")
    attached_orders = (
        replace(attached, stop_order_type="TakeProfit", trigger_price=Decimal("4.5")),
        replace(attached, stop_order_type="StopLoss", trigger_price=Decimal("5.144")),
        replace(attached, stop_order_type="TrailingStop"),
    )
    assert (
        render_position_protection(
            replace(position, stop_loss=None, trailing_stop=None), attached_orders
        )
        == "Protection: SL 5.144 · TP 4.5 · Trailing stop active"
    )
