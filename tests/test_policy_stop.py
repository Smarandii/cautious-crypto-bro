import asyncio
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_execution import policy, source

from cautious_crypto_bro.domain import (
    ExecutionPlan,
    IntentExtraction,
    StopLossSource,
    StrategyV2Policy,
    TakeProfitSource,
    TradingIntent,
)
from cautious_crypto_bro.execution import (
    ExecutionPlanner,
    ExecutionPlanningError,
    InstrumentContext,
)
from cautious_crypto_bro.openrouter import _signals_from_extraction


@pytest.mark.parametrize(
    "side,entry,market,expected",
    [
        ("LONG", {"type": "MARKET"}, "100", "98"),
        ("SHORT", {"type": "MARKET"}, "100", "102"),
        ("LONG", {"type": "LIMIT", "price": 100}, "101", "98"),
        ("SHORT", {"type": "LIMIT", "price": 100}, "99", "102"),
        ("LONG", {"type": "RANGE", "range_low": 98, "range_high": 100}, "101", "96.04"),
        (
            "SHORT",
            {"type": "RANGE", "range_low": 100, "range_high": 102},
            "99",
            "104.04",
        ),
    ],
)
@pytest.mark.parametrize("trader_stop", [False, True])
def test_extraction_to_concrete_plan(side, entry, market, expected, trader_stop):
    candidate = dict(
        symbol="BTCUSDT", side=side, entry=entry, summary="Explicit entry", confidence=1
    )
    if trader_stop:
        candidate["stop_loss"] = 90 if side == "LONG" else 110
        expected = str(candidate["stop_loss"])
    extraction = IntentExtraction.model_validate(
        dict(actionable=True, reason="Explicit entry", intents=[candidate])
    )
    signals = _signals_from_extraction(source(), extraction)
    assert len(signals.open_intents) == 1
    intent = TradingIntent.model_validate_json(
        signals.open_intents[0].model_dump_json()
    )
    assert (intent.stop_loss is not None) == trader_stop
    context = InstrumentContext(
        Decimal(market),
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("5"),
    )
    plan = ExecutionPlanner().plan(intent, policy(), context)
    assert plan.stop_loss == Decimal(expected)
    assert plan.stop_loss_source == (
        StopLossSource.TRADER if trader_stop else StopLossSource.POLICY
    )
    assert plan.take_profit_source == TakeProfitSource.POLICY
    assert 0 < plan.planned_max_loss_usdt <= plan.policy.risk_budget_usdt
    assert (
        sum(
            order.quantity * abs(order.reference_price - plan.stop_loss)
            for order in plan.orders
        )
        == plan.planned_max_loss_usdt
    )
    assert ExecutionPlan.model_validate_json(plan.model_dump_json()) == plan
    payload = plan.model_dump()
    del payload["stop_loss_source"]
    assert (
        ExecutionPlan.model_validate(payload).stop_loss_source == StopLossSource.TRADER
    )
    del payload["stop_loss"]
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": None},
        {"entry": None},
        {"symbol": ""},
        {"entry": "LIMIT"},
        {"entry": "RANGE"},
        {"stop_loss": 0},
        {"stop_loss": -1},
        {"entry": {"type": "LIMIT", "price": 100}, "take_profit": 95},
    ],
)
def test_missing_stop_does_not_rescue_invalid_candidate(overrides):
    candidate = dict(
        symbol="BTCUSDT", side="LONG", entry="MARKET", summary="Candidate", confidence=1
    )
    candidate.update(overrides)
    extraction = IntentExtraction.model_validate(
        dict(actionable=True, reason="Candidate", intents=[candidate])
    )
    assert not _signals_from_extraction(source(), extraction).open_intents


@pytest.mark.parametrize("distance", [0, -1, 100, 101])
def test_invalid_fallback_distance(distance):
    with pytest.raises(ValidationError):
        StrategyV2Policy(fallback_stop_distance_pct=Decimal(distance))


@pytest.mark.parametrize("tick", ["100", "1"])
def test_fallback_rejects_unrepresentable_geometry(tick):
    intent = TradingIntent(
        source=source(),
        symbol="BTCUSDT",
        side="LONG",
        entry={"type": "MARKET"},
        summary="Entry",
        confidence=1,
    )
    context = InstrumentContext(
        Decimal("1"), Decimal(tick), Decimal("0.001"), Decimal("0.001"), Decimal("5")
    )
    with pytest.raises(ExecutionPlanningError):
        ExecutionPlanner().plan(intent, policy(), context)


@pytest.mark.parametrize(
    "target,expected_source",
    [(101, TakeProfitSource.TRADER), (110, TakeProfitSource.POLICY)],
)
def test_trader_target_survives_missing_stop(target, expected_source):
    intent = TradingIntent(
        source=source(),
        symbol="BTCUSDT",
        side="LONG",
        entry={"type": "LIMIT", "price": 100},
        take_profit=target,
        summary="Entry",
        confidence=1,
    )
    context = InstrumentContext(
        Decimal("101"),
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("5"),
    )
    plan = ExecutionPlanner().plan(intent, policy(), context)
    assert plan.stop_loss_source == StopLossSource.POLICY
    assert plan.take_profit_source == expected_source
    assert plan.trader_take_profit == Decimal(target)


@pytest.mark.parametrize("side,expected", [("LONG", "96.99"), ("SHORT", "103.01")])
def test_custom_distance_rounds_outward_and_survives_rebase(side, expected):
    intent = TradingIntent(
        source=source(),
        symbol="BTCUSDT",
        side=side,
        entry={"type": "MARKET"},
        summary="Entry",
        confidence=1,
    )
    configured = policy().model_copy(
        update={
            "strategy_v2": StrategyV2Policy(fallback_stop_distance_pct=Decimal("3.001"))
        }
    )
    context = InstrumentContext(
        Decimal("100"),
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("5"),
    )
    planner = ExecutionPlanner()
    plan = planner.plan(intent, configured, context)
    assert plan.stop_loss == Decimal(expected)
    rebased = planner.rebase_market_plan(
        plan,
        fill_price=Decimal("100.01"),
        filled_quantity=plan.orders[0].quantity,
        context=context,
    )
    assert rebased.stop_loss == plan.stop_loss
    assert rebased.stop_loss_source == StopLossSource.POLICY
    assert rebased.planned_max_loss_usdt <= configured.risk_budget_usdt


@pytest.mark.parametrize("side,expected", [("LONG", "98"), ("SHORT", "102")])
def test_policy_stop_survives_storage_execution_and_restart(tmp_path, side, expected):
    from test_remaining_concerns import Exchange, executor_for

    from cautious_crypto_bro.domain import ApprovalMode, IntentStatus, Side
    from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
    from cautious_crypto_bro.position_supervisor import PositionSupervisor
    from cautious_crypto_bro.storage import IntentStore

    async def run():
        extraction = IntentExtraction.model_validate(
            dict(
                actionable=True,
                reason="Entry",
                intents=[
                    dict(
                        symbol="BTCUSDT",
                        side=side,
                        entry="MARKET",
                        summary="Entry now",
                        confidence=1,
                    )
                ],
            )
        )
        intent = _signals_from_extraction(source(), extraction).open_intents[0]
        exchange = Exchange(Side(side))
        with executor_for(exchange) as executor:
            context = InstrumentContext(
                Decimal("100"),
                Decimal("0.1"),
                Decimal("0.001"),
                Decimal("0.001"),
                Decimal("5"),
            )
            plan = ExecutionPlanner().plan(intent, policy(), context)
            path = tmp_path / "state.db"
            store = IntentStore(path)
            await store.initialize()
            claim = await store.claim_source(intent.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source(
                ((intent, plan),), (), claim
            )
            coordinator = ExecutionCoordinator(
                store=store,
                executor=executor,
                max_age_seconds=3600,
                execution_lock=asyncio.Lock(),
            )
            result = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert result.status is IntentStatus.EXECUTED
            assert exchange.stop == Decimal(expected)
            restarted = IntentStore(path)
            persisted = await restarted.get_execution_plan(intent.intent_id)
            assert persisted.stop_loss_source == StopLossSource.POLICY
            assert persisted.stop_loss == Decimal(expected)
            await PositionSupervisor(
                store=restarted, executor=executor, mutation_lock=asyncio.Lock()
            ).reconcile_once()
            assert exchange.stop == Decimal(expected)
            assert len(exchange.open_entries()) == 2

    asyncio.run(run())
