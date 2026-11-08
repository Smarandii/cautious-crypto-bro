from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cautious_crypto_bro.domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    PlannedOrder,
    Side,
)
from cautious_crypto_bro.portfolio_risk import (
    PortfolioRiskUnavailable,
    open_stop_risk_usdt,
    policy_with_remaining_portfolio_risk,
)


def _plan():
    intent_id = uuid4()
    return ExecutionPlan(
        strategy_version=1,
        intent_id=intent_id,
        symbol="BTCUSDT",
        side=Side.LONG,
        orders=(
            PlannedOrder(
                name="E1",
                order_type=ExecutionOrderType.LIMIT,
                quantity=Decimal("1"),
                price=Decimal("95"),
                reference_price=Decimal("95"),
            ),
        ),
        stop_loss=Decimal("90"),
        take_profit=Decimal("120"),
        policy=ExecutionPolicy(
            trading_capital_usdt=Decimal("10000"),
            risk_per_trade_pct=Decimal("1"),
        ),
        planned_max_loss_usdt=Decimal("5"),
    )


def test_open_stop_risk_includes_positions_and_unfilled_v2_entry_orders() -> None:
    now = datetime.now(UTC)
    plan = _plan()
    state = AccountStateSummary(
        as_of=now,
        positions=(
            AccountPosition(
                symbol="ETHUSDT",
                side=Side.LONG,
                size=Decimal("1"),
                avg_price=Decimal("100"),
                mark_price=Decimal("100"),
                unrealised_pnl=Decimal("0"),
                status="Open",
                take_profit=None,
                stop_loss=Decimal("90"),
            ),
        ),
        open_orders=(
            AccountOrder(
                symbol="BTCUSDT",
                side=Side.LONG,
                order_type="Limit",
                status="New",
                quantity=Decimal("1"),
                remaining_quantity=Decimal("2"),
                price=Decimal("95"),
                avg_price=None,
                order_id="order-1",
                order_link_id=(f"ccb-v2-{plan.intent_id.hex[:20]}-e1"),
                reduce_only=False,
                updated_at=now,
            ),
            AccountOrder(
                symbol="ETHUSDT",
                side=Side.SHORT,
                order_type="Limit",
                status="New",
                quantity=Decimal("1"),
                remaining_quantity=Decimal("1"),
                price=Decimal("90"),
                avg_price=None,
                order_id="tp-1",
                order_link_id="external-tp",
                reduce_only=True,
                updated_at=now,
            ),
        ),
    )

    assert open_stop_risk_usdt(state, ((SimpleNamespace(), plan),)) == Decimal("20")


def test_open_stop_risk_fails_closed_for_unprotected_position_or_unknown_entry() -> (
    None
):
    now = datetime.now(UTC)
    unprotected = AccountStateSummary(
        as_of=now,
        positions=(
            AccountPosition(
                symbol="ETHUSDT",
                side=Side.LONG,
                size=Decimal("1"),
                avg_price=Decimal("100"),
                mark_price=Decimal("100"),
                unrealised_pnl=Decimal("0"),
                status="Open",
                take_profit=None,
                stop_loss=None,
            ),
        ),
        open_orders=(),
    )
    with pytest.raises(PortfolioRiskUnavailable, match="no exchange stop"):
        open_stop_risk_usdt(unprotected, ())

    unknown_entry = AccountStateSummary(
        as_of=now,
        positions=(),
        open_orders=(
            AccountOrder(
                symbol="ETHUSDT",
                side=Side.LONG,
                order_type="Limit",
                status="New",
                quantity=Decimal("1"),
                remaining_quantity=Decimal("1"),
                price=Decimal("100"),
                avg_price=None,
                order_id="external-entry",
                order_link_id="manual-entry",
                reduce_only=False,
                updated_at=now,
            ),
        ),
    )
    with pytest.raises(PortfolioRiskUnavailable, match="without a known V2"):
        open_stop_risk_usdt(unknown_entry, ())


def test_portfolio_budget_can_only_reduce_per_trade_budget() -> None:
    policy = ExecutionPolicy(
        trading_capital_usdt=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
    )

    reduced = policy_with_remaining_portfolio_risk(policy, Decimal("25"))
    unchanged = policy_with_remaining_portfolio_risk(policy, Decimal("200"))

    assert reduced.risk_budget_usdt == Decimal("25")
    assert reduced.risk_per_trade_pct == Decimal("0.25")
    assert unchanged is policy
    with pytest.raises(ValueError, match="No portfolio stop-risk capacity"):
        policy_with_remaining_portfolio_risk(policy, Decimal("0"))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_open_stop_risk_accepts_verified_partial_stop_before_handoff(
    side: Side,
) -> None:
    now = datetime.now(UTC)
    plan = _plan().model_copy(
        update={
            "side": side,
            "stop_loss": Decimal("90") if side is Side.LONG else Decimal("110"),
        }
    )
    closing_side = Side.SHORT if side is Side.LONG else Side.LONG
    parent_link = f"ccb-v2-{plan.intent_id.hex[:20]}-e1"

    def make_state(
        *,
        qty: Decimal = Decimal("2"),
        trigger: Decimal = plan.stop_loss,
        parent: str = parent_link,
    ) -> AccountStateSummary:
        return AccountStateSummary(
            as_of=now,
            positions=(
                AccountPosition(
                    symbol="BTCUSDT",
                    side=side,
                    size=Decimal("2"),
                    avg_price=Decimal("100"),
                    mark_price=Decimal("100"),
                    unrealised_pnl=Decimal("0"),
                    status="Open",
                    take_profit=None,
                    stop_loss=None,
                ),
            ),
            open_orders=(
                AccountOrder(
                    symbol="BTCUSDT",
                    side=closing_side,
                    order_type="Market",
                    status="Untriggered",
                    quantity=qty,
                    remaining_quantity=qty,
                    price=None,
                    avg_price=None,
                    order_id="partial-sl-1",
                    order_link_id="",
                    reduce_only=True,
                    updated_at=now,
                    stop_order_type="PartialStopLoss",
                    trigger_price=trigger,
                    close_on_trigger=True,
                    parent_order_link_id=parent,
                ),
            ),
        )

    assert open_stop_risk_usdt(make_state(), ((SimpleNamespace(), plan),)) == Decimal(
        "20"
    )

    for invalid_state, active in (
        (make_state(qty=Decimal("1")), ((SimpleNamespace(), plan),)),
        (make_state(trigger=Decimal("85")), ((SimpleNamespace(), plan),)),
        (make_state(parent="other-parent"), ((SimpleNamespace(), plan),)),
        (
            make_state(),
            ((SimpleNamespace(), plan), (SimpleNamespace(), plan)),
        ),
    ):
        with pytest.raises(PortfolioRiskUnavailable, match="no exchange stop"):
            open_stop_risk_usdt(invalid_state, active)


def test_signal_planning_refreshes_account_state_when_pre_llm_snapshot_caught_in_flight_stop(
    tmp_path,
) -> None:
    import asyncio

    from cautious_crypto_bro.domain import (
        AutoApprovalMode,
        Entry,
        EntryType,
        InstrumentContext,
        SignalContextSnapshot,
        SignalExtraction,
        SignalPositionContext,
        SourceMessage,
        TradingIntent,
    )
    from cautious_crypto_bro.execution import ExecutionPlanner
    from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
    from cautious_crypto_bro.signals.planning import SignalPlanning
    from cautious_crypto_bro.storage import IntentStore

    async def run() -> None:
        now = datetime.now(UTC)
        store = IntentStore(tmp_path / "refresh.db")
        await store.initialize()
        plan = _plan()
        stale_unprotected = AccountStateSummary(
            as_of=now,
            positions=(
                AccountPosition(
                    symbol="SANDUSDT",
                    side=Side.LONG,
                    size=Decimal("100"),
                    avg_price=Decimal("0.042"),
                    mark_price=Decimal("0.042"),
                    unrealised_pnl=Decimal("0"),
                    status="Open",
                    take_profit=None,
                    stop_loss=None,
                ),
            ),
            open_orders=(),
        )
        fresh_protected = AccountStateSummary(
            as_of=now,
            positions=(
                AccountPosition(
                    symbol="SANDUSDT",
                    side=Side.LONG,
                    size=Decimal("100"),
                    avg_price=Decimal("0.042"),
                    mark_price=Decimal("0.042"),
                    unrealised_pnl=Decimal("0"),
                    status="Open",
                    take_profit=None,
                    stop_loss=Decimal("0.039"),
                ),
            ),
            open_orders=(),
        )

        class _RefreshGateway:
            def __init__(self, refreshed: AccountStateSummary) -> None:
                self.refreshed = refreshed
                self.account_reads = 0

            async def account_state(self) -> AccountStateSummary:
                self.account_reads += 1
                return self.refreshed

            async def wallet_balance_usdt(self) -> Decimal:
                return Decimal("7000")

            async def market_context(self, _symbol: str) -> InstrumentContext:
                return InstrumentContext(
                    market_price=Decimal("100"),
                    tick_size=Decimal("0.1"),
                    qty_step=Decimal("0.01"),
                    min_qty=Decimal("0.01"),
                    min_notional=Decimal("5"),
                )

            async def prepare_entry_plan(
                self, candidate_plan: ExecutionPlan
            ) -> ExecutionPlan:
                return candidate_plan

        gateway = _RefreshGateway(fresh_protected)
        planner = ExecutionPlanner()
        coordinator = ExecutionCoordinator(
            store=store,
            executor=gateway,  # type: ignore[arg-type]
            planner=planner,
            max_age_seconds=900,
            execution_lock=asyncio.Lock(),
            portfolio_stop_risk_cap_usdt=Decimal("4000"),
        )
        signal_planning = SignalPlanning(
            store=store,
            planner=planner,
            executor=gateway,  # type: ignore[arg-type]
            coordinator=coordinator,
            auto_approval_mode=AutoApprovalMode.OPEN_ONLY,
            intent_max_age_seconds=900,
            demo_long_risk_multiplier=Decimal("1"),
            demo_long_exit_control_fraction=Decimal("0"),
            demo_long_participation_skip_fraction=Decimal("0"),
            demo_long_momentum_gate=False,
            demo_exit_profile="baseline",
            demo_portfolio_stop_risk_cap_usdt=Decimal("4000"),
            demo_entry_order_ttl_minutes=240,
        )
        intent = TradingIntent(
            intent_id=plan.intent_id,
            source=SourceMessage(
                channel_id=-1001,
                channel_title="Test",
                message_id=1,
                published_at=now,
                received_at=now,
                text="BTW LONG",
            ),
            symbol="BTWUSDT",
            side=Side.LONG,
            entry=Entry(type=EntryType.MARKET),
            stop_loss=90.0,
            summary="BTW",
            confidence=1.0,
        )
        snapshot = SignalContextSnapshot(
            account_state=stale_unprotected,
            account_state_error=None,
            position_context=SignalPositionContext(
                source_channel_id=-1001,
                account_state_available=True,
                source_open_history=(),
                account_positions=(),
            ),
        )
        result = await signal_planning.plan_open_intents(
            SignalExtraction(
                open_intents=(intent,),
                position_actions=(),
            ),
            snapshot,
        )
        assert len(result.planned) == 1
        assert not result.errors
        assert gateway.account_reads == 1

        # If the refreshed snapshot is still unprotected, fail closed.
        still_bad = _RefreshGateway(stale_unprotected)
        signal_planning._executor = still_bad  # type: ignore[assignment]
        bad_result = await signal_planning.plan_open_intents(
            SignalExtraction(
                open_intents=(intent,),
                position_actions=(),
            ),
            snapshot,
        )
        assert not bad_result.planned
        assert any(
            "SANDUSDT position has no exchange stop price" in e
            for e in bad_result.errors
        )

    asyncio.run(run())
