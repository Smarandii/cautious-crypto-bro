from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

from cautious_crypto_bro.approval_bot import (
    ApprovalBot,
)
from cautious_crypto_bro.domain import (
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


def test_render_contains_execution_policy() -> None:
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

    rendered = ApprovalBot._render(
        intent,
        plan,
    )

    assert "LONG ETHUSDT" in rendered
    assert "Orders: 3 · total 0.6" in rendered
    assert "Risk: <b>≤ 66 USDT</b> (1% policy)" in rendered
    assert "R:R:" in rendered
    assert "Open source message" in rendered
    assert "Trader &amp; Co" in rendered
    assert "Confidence:" not in rendered
    assert "Published:" not in rendered
