from datetime import (
    datetime,
    timezone,
)
from decimal import Decimal

from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionPolicy,
    Side,
    SourceMessage,
    TradingIntent,
)
from cautious_crypto_bro.execution import (
    ExecutionPlanner,
    InstrumentContext,
)


def intent() -> TradingIntent:
    now = datetime.now(
        timezone.utc
    )

    return TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=1,
            published_at=now,
            received_at=now,
            text="test",
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(
            type=EntryType.RANGE,
            range_low=100,
            range_high=110,
        ),
        stop_loss=90,
        take_profit=130,
        summary="Test range.",
        confidence=1,
    )


def context() -> InstrumentContext:
    return InstrumentContext(
        market_price=Decimal("120"),
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def policy(
    order_count: int,
    risk: str = "1",
) -> ExecutionPolicy:
    return ExecutionPolicy(
        trading_capital_usdt=(
            Decimal("6800")
        ),
        risk_per_trade_pct=(
            Decimal(risk)
        ),
        range_order_count=(
            order_count
        ),
    )


def test_three_range_orders_are_evenly_spaced() -> None:
    plan = ExecutionPlanner().plan(
        intent(),
        policy(3),
        context(),
    )

    assert [
        order.price
        for order in plan.orders
    ] == [
        Decimal("100.0"),
        Decimal("105.0"),
        Decimal("110.0"),
    ]

    assert (
        plan.planned_max_loss_usdt
        <= Decimal("68")
    )


def test_one_range_order_uses_midpoint() -> None:
    plan = ExecutionPlanner().plan(
        intent(),
        policy(1),
        context(),
    )

    assert [
        order.price
        for order in plan.orders
    ] == [
        Decimal("105.0")
    ]


def test_five_range_orders_are_evenly_spaced() -> None:
    plan = ExecutionPlanner().plan(
        intent(),
        policy(5),
        context(),
    )

    assert [
        order.price
        for order in plan.orders
    ] == [
        Decimal("100.0"),
        Decimal("102.5"),
        Decimal("105.0"),
        Decimal("107.5"),
        Decimal("110.0"),
    ]


def test_two_percent_risk_doubles_risk_budget() -> None:
    plan = ExecutionPlanner().plan(
        intent(),
        policy(
            3,
            risk="2",
        ),
        context(),
    )

    assert (
        plan.policy.risk_budget_usdt
        == Decimal("136")
    )

    assert (
        plan.planned_max_loss_usdt
        <= Decimal("136")
    )

    assert (
        plan.planned_max_loss_usdt
        > Decimal("68")
    )
