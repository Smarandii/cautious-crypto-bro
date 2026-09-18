from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

from cautious_crypto_bro.approval_bot import (
    ApprovalBot,
)
from cautious_crypto_bro.bybit import (
    PositionExposure,
    SymbolExposure,
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


def make_intent(
    side: Side = Side.LONG,
) -> TradingIntent:
    now = datetime.now(UTC)

    return TradingIntent(
        source=SourceMessage(
            channel_id=-100123,
            channel_title="Trader",
            channel_username="trader",
            message_id=123,
            published_at=now,
            received_at=now,
            text="signal",
        ),
        symbol="BTCUSDT",
        side=side,
        entry=Entry(
            type=EntryType.LIMIT,
            price=Decimal("61000"),
        ),
        stop_loss=(Decimal("59000") if side is Side.LONG else Decimal("63000")),
        take_profit=(Decimal("65000") if side is Side.LONG else Decimal("57000")),
        summary="Test signal",
        confidence=0.9,
    )


def make_plan(
    intent: TradingIntent,
) -> ExecutionPlan:
    policy = ExecutionPolicy(
        trading_capital_usdt=(Decimal("6800")),
        risk_per_trade_pct=(Decimal("1")),
        range_order_count=1,
    )

    return ExecutionPlan(
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        side=intent.side,
        orders=(
            PlannedOrder(
                order_type=(ExecutionOrderType.LIMIT),
                quantity=Decimal("0.01"),
                price=Decimal("61000"),
                reference_price=(Decimal("61000")),
            ),
        ),
        stop_loss=Decimal(str(intent.stop_loss)),
        take_profit=Decimal(str(intent.take_profit)),
        policy=policy,
        planned_max_loss_usdt=(Decimal("20")),
    )


def test_opposite_exposure_warning() -> None:
    intent = make_intent(Side.SHORT)
    plan = make_plan(intent)

    exposure = SymbolExposure(
        symbol="BTCUSDT",
        positions=(
            PositionExposure(
                side=Side.LONG,
                size=Decimal("0.25"),
                avg_price=(Decimal("60000")),
            ),
        ),
    )

    rendered = ApprovalBot._render(
        intent,
        plan,
        exposure=exposure,
    )

    assert "EXISTING OPPOSITE EXPOSURE" in rendered
    assert "reduce, close, or reverse" in rendered
