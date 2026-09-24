from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    Side,
    SourceMessage,
    TakeProfitSource,
    TradingIntent,
)
from cautious_crypto_bro.execution import (
    ExecutionPlanner,
    ExecutionPlanningError,
    InstrumentContext,
)


def source() -> SourceMessage:
    now = datetime.now(UTC)

    return SourceMessage(
        channel_id=1,
        channel_title="Test",
        message_id=1,
        published_at=now,
        received_at=now,
        text="test",
    )


def range_intent() -> TradingIntent:
    return TradingIntent(
        source=source(),
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


def market_intent() -> TradingIntent:
    return TradingIntent(
        source=source(),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(
            type=EntryType.MARKET,
        ),
        stop_loss=90,
        take_profit=None,
        summary="Test market.",
        confidence=1,
    )


def limit_intent(
    *,
    side: Side = Side.LONG,
) -> TradingIntent:
    if side is Side.LONG:
        price = 100
        stop = 90
    else:
        price = 100
        stop = 110

    return TradingIntent(
        source=source(),
        symbol="BTCUSDT",
        side=side,
        entry=Entry(
            type=EntryType.LIMIT,
            price=price,
        ),
        stop_loss=stop,
        take_profit=None,
        summary="Test limit.",
        confidence=1,
    )


def context(
    market_price: str = "120",
) -> InstrumentContext:
    return InstrumentContext(
        market_price=Decimal(market_price),
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def policy(
    risk: str = "1",
) -> ExecutionPolicy:
    return ExecutionPolicy(
        trading_capital_usdt=Decimal("6800"),
        risk_per_trade_pct=Decimal(risk),
        range_order_count=3,
    )


def test_range_plan_has_weighted_entries_independent_exits_and_runner() -> None:
    plan = ExecutionPlanner().plan(
        range_intent(),
        policy(),
        context(),
    )

    assert plan.strategy_version == 2

    assert [order.name for order in plan.orders] == [
        "E1",
        "E2",
        "E3",
    ]

    assert [order.risk_pct for order in plan.orders] == [
        Decimal("60"),
        Decimal("25"),
        Decimal("15"),
    ]

    assert [order.price for order in plan.orders] == [
        Decimal("110.0"),
        Decimal("105.0"),
        Decimal("100.0"),
    ]

    assert all(order.take_profit is None for order in plan.orders)

    assert plan.planned_max_loss_usdt <= Decimal("68")

    assert plan.planned_max_loss_usdt > Decimal("67.9")

    assert [target.name for target in plan.take_profit_targets] == [
        "TP1",
        "TP2",
        "TP3",
    ]

    assert [target.close_pct for target in plan.take_profit_targets] == [
        Decimal("25"),
        Decimal("25"),
        Decimal("25"),
    ]

    assert plan.runner_pct == Decimal("25")

    # Trader TP=130 falls just below the
    # planned 1.5R policy target after the
    # risk-weighted average entry is used,
    # so V2 compresses TP2/TP3 and preserves
    # the trader's 130 final fixed target.
    assert plan.take_profit_source is TakeProfitSource.TRADER

    assert plan.take_profit_targets[-1].price == Decimal("130.0")


def test_market_plan_builds_market_starter_and_two_limits() -> None:
    plan = ExecutionPlanner().plan(
        market_intent(),
        policy(),
        context(),
    )

    assert [order.order_type for order in plan.orders] == [
        ExecutionOrderType.MARKET,
        ExecutionOrderType.LIMIT,
        ExecutionOrderType.LIMIT,
    ]

    assert [order.reference_price for order in plan.orders] == [
        Decimal("120"),
        Decimal("110.1"),
        Decimal("100.2"),
    ]

    assert [order.price for order in plan.orders] == [
        None,
        Decimal("110.1"),
        Decimal("100.2"),
    ]

    assert plan.planned_max_loss_usdt <= Decimal("68")


def test_long_limit_plan_scales_toward_stop() -> None:
    plan = ExecutionPlanner().plan(
        limit_intent(),
        policy(),
        context(),
    )

    assert [order.reference_price for order in plan.orders] == [
        Decimal("100.0"),
        Decimal("96.7"),
        Decimal("93.4"),
    ]


def test_short_limit_plan_scales_toward_stop() -> None:
    plan = ExecutionPlanner().plan(
        limit_intent(
            side=Side.SHORT,
        ),
        policy(),
        context(
            market_price="90",
        ),
    )

    assert [order.reference_price for order in plan.orders] == [
        Decimal("100.0"),
        Decimal("103.3"),
        Decimal("106.6"),
    ]


def test_two_percent_risk_doubles_v2_budget() -> None:
    one_percent = ExecutionPlanner().plan(
        market_intent(),
        policy("1"),
        context(),
    )

    two_percent = ExecutionPlanner().plan(
        market_intent(),
        policy("2"),
        context(),
    )

    assert two_percent.policy.risk_budget_usdt == Decimal("136")

    assert two_percent.planned_max_loss_usdt <= Decimal("136")

    assert (
        two_percent.planned_max_loss_usdt
        > one_percent.planned_max_loss_usdt * Decimal("1.99")
    )


def test_trader_tp_too_close_is_rejected() -> None:
    intent = limit_intent().model_copy(
        update={
            "take_profit": 99,
        }
    )

    try:
        ExecutionPlanner().plan(
            intent,
            policy(),
            context(),
        )

    except ExecutionPlanningError as exc:
        assert "too close" in str(exc)

    else:
        raise AssertionError("Expected close trader TP to be rejected")


def test_historical_v1_plan_still_parses() -> None:
    historical = {
        "intent_id": ("11111111-1111-1111-1111-111111111111"),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "orders": [
            {
                "order_type": "LIMIT",
                "quantity": "0.1",
                "price": "100",
                "reference_price": "100",
                "take_profit": "120",
            }
        ],
        "stop_loss": "90",
        "take_profit": "120",
        "take_profit_targets": [],
        "take_profit_source": "TRADER",
        "policy": {
            "trading_capital_usdt": "6800",
            "risk_per_trade_pct": "1",
            "range_order_count": 3,
        },
        "planned_max_loss_usdt": "1",
    }

    plan = ExecutionPlan.model_validate(historical)

    assert plan.strategy_version == 1
    assert plan.runner_pct == 0
    assert plan.orders[0].name == "ENTRY"
    assert plan.orders[0].risk_pct is None


def test_v2_exit_r_multiples_are_not_changed_by_tick_rounding() -> None:
    plan = ExecutionPlanner().plan(
        market_intent(),
        policy(),
        InstrumentContext(
            market_price=Decimal("100"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )

    assert [target.r_multiple for target in plan.take_profit_targets] == [
        Decimal("0.5"),
        Decimal("1"),
        Decimal("1.5"),
    ]


def sol_smoke_intent() -> TradingIntent:
    return TradingIntent(
        source=source(),
        symbol="SOLUSDT",
        side=Side.LONG,
        entry=Entry(
            type=EntryType.MARKET,
        ),
        stop_loss=111.08,
        take_profit=None,
        summary="SOL V2 smoke.",
        confidence=1,
    )


def sol_smoke_context() -> InstrumentContext:
    return InstrumentContext(
        market_price=Decimal("116.93"),
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.1"),
        min_qty=Decimal("0.1"),
        min_notional=Decimal("5"),
    )


def test_v2_rejects_primary_entry_too_small_for_partial_exit() -> None:
    tiny_policy = ExecutionPolicy(
        trading_capital_usdt=(Decimal("7341.40")),
        risk_per_trade_pct=(Decimal("0.05")),
    )

    try:
        ExecutionPlanner().plan(
            sol_smoke_intent(),
            tiny_policy,
            sol_smoke_context(),
        )

    except ExecutionPlanningError as exc:
        assert "too small to support any fixed partial exit" in str(exc)

    else:
        raise AssertionError("Expected undersized V2 plan rejection")


def test_v2_accepts_primary_entry_with_partial_exit_and_runner() -> None:
    smoke_policy = ExecutionPolicy(
        trading_capital_usdt=(Decimal("7341.40")),
        risk_per_trade_pct=(Decimal("0.10")),
    )

    plan = ExecutionPlanner().plan(
        sol_smoke_intent(),
        smoke_policy,
        sol_smoke_context(),
    )

    assert plan.orders[0].quantity >= Decimal("0.4")


def test_market_plan_rebases_scale_ins_from_actual_fill() -> None:
    planner = ExecutionPlanner()

    initial = planner.plan(
        market_intent(),
        policy(),
        context(),
    )

    rebased = planner.rebase_market_plan(
        initial,
        fill_price=Decimal("121"),
        filled_quantity=(initial.orders[0].quantity),
        context=context("121"),
    )

    assert rebased.orders[0].reference_price == Decimal("121")

    assert rebased.orders[0].quantity == initial.orders[0].quantity

    assert [order.reference_price for order in rebased.orders] == [
        Decimal("121"),
        Decimal("110.8"),
        Decimal("100.5"),
    ]

    assert [order.price for order in rebased.orders] == [
        None,
        Decimal("110.8"),
        Decimal("100.5"),
    ]

    # Adverse E1 slippage consumes more than
    # the nominal 60% E1 risk allocation, so
    # E2/E3 are conservatively downsized.
    assert rebased.orders[1].quantity < initial.orders[1].quantity

    assert rebased.orders[2].quantity < initial.orders[2].quantity

    assert rebased.planned_max_loss_usdt <= rebased.policy.risk_budget_usdt

    assert rebased.take_profit_targets != initial.take_profit_targets
