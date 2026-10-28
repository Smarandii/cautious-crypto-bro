import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from cautious_crypto_bro.domain import (
    AccountPosition,
    AccountStateSummary,
    ApprovalMode,
    Entry,
    EntryPreflightError,
    EntryType,
    ExecutionPlan,
    ExecutionPolicy,
    InstrumentContext,
    IntentStatus,
    OpenRelation,
    Side,
    SourceMessage,
    StrategyStatus,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator


class LegacyStore:
    def __init__(self, intent: TradingIntent, plan: ExecutionPlan) -> None:
        self.intent = intent
        self.plan = plan
        self.claimed = False
        self.failure: str | None = None

    async def get_intent(self, intent_id: UUID) -> TradingIntent:
        assert intent_id == self.intent.intent_id
        return self.intent

    async def get_execution_plan(self, intent_id: UUID) -> ExecutionPlan:
        assert intent_id == self.intent.intent_id
        return self.plan

    async def claim_for_execution(
        self,
        intent_id: UUID,
        user_id: int | None,
        *,
        expected_approval_mode: ApprovalMode,
    ) -> bool:
        assert intent_id == self.intent.intent_id
        assert expected_approval_mode is self.intent.approval_mode
        self.claimed = True
        return True

    async def mark_failed(self, intent_id: UUID, error: str) -> None:
        assert intent_id == self.intent.intent_id
        self.failure = error


class NoExchangeCalls:
    def __getattr__(self, name: str) -> None:
        raise AssertionError(f"Legacy plan reached exchange method: {name}")


@pytest.mark.parametrize(
    "approval_mode",
    [ApprovalMode.MANUAL, ApprovalMode.AUTO],
)
def test_historical_v1_approval_is_rejected_before_exchange(
    approval_mode: ApprovalMode,
) -> None:
    now = datetime.now(UTC)
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=1,
            published_at=now,
            received_at=now,
            text="BTC long",
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(type=EntryType.LIMIT, price=100),
        stop_loss=90,
        take_profit=120,
        summary="Historical trade",
        confidence=1,
        approval_mode=approval_mode,
    )
    plan = ExecutionPlan.model_validate(
        {
            "intent_id": str(intent.intent_id),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "orders": [
                {
                    "order_type": "LIMIT",
                    "quantity": "0.1",
                    "price": "100",
                    "reference_price": "100",
                }
            ],
            "stop_loss": "90",
            "take_profit": "120",
            "policy": {
                "trading_capital_usdt": "6800",
                "risk_per_trade_pct": "1",
                "range_order_count": 3,
            },
            "planned_max_loss_usdt": "1",
        }
    )
    assert plan.strategy_version == 1
    assert plan.policy.risk_budget_usdt == Decimal("68")

    store = LegacyStore(intent, plan)
    coordinator = ExecutionCoordinator(
        store=store,
        executor=NoExchangeCalls(),
        planner=ExecutionPlanner(),
        max_age_seconds=300,
    )

    outcome = asyncio.run(
        coordinator.execute_intent(
            intent.intent_id,
            approval_mode=approval_mode,
        )
    )

    assert store.claimed
    assert outcome.status is IntentStatus.FAILED
    assert outcome.plan is plan
    assert store.failure == outcome.message
    assert "read-only" in outcome.message


def test_portfolio_cap_is_rechecked_against_live_stops_before_execution() -> None:
    now = datetime.now(UTC)
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=90,
            published_at=now,
            received_at=now,
            text="BTC long",
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(type=EntryType.LIMIT, price=100),
        stop_loss=90,
        take_profit=120,
        summary="New candidate",
        confidence=1,
        relation=OpenRelation.NEW,
    )
    plan = ExecutionPlan.model_validate(
        {
            "intent_id": str(intent.intent_id),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "orders": [
                {
                    "order_type": "LIMIT",
                    "quantity": "1",
                    "price": "100",
                    "reference_price": "100",
                }
            ],
            "stop_loss": "90",
            "take_profit": "120",
            "policy": {
                "trading_capital_usdt": "10000",
                "risk_per_trade_pct": "1",
            },
            "planned_max_loss_usdt": "95",
        }
    )
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
                stop_loss=Decimal("10"),
            ),
        ),
        open_orders=(),
    )

    class Store:
        async def get_active_position_strategies(self):
            return ()

    class Executor:
        async def account_state(self):
            return state

    coordinator = ExecutionCoordinator(
        store=Store(),
        executor=Executor(),
        planner=ExecutionPlanner(),
        max_age_seconds=300,
        portfolio_stop_risk_cap_usdt=Decimal("100"),
    )

    with pytest.raises(EntryPreflightError, match="Portfolio stop-risk cap exceeded"):
        asyncio.run(
            coordinator._assert_open_preflight(
                intent,
                plan,
                ApprovalMode.MANUAL,
            )
        )


@pytest.mark.parametrize("approval_mode", [ApprovalMode.MANUAL, ApprovalMode.AUTO])
def test_old_entry_executes_and_persists_under_mutation_lock(approval_mode) -> None:
    now = datetime.now(UTC)
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=2,
            published_at=now,
            received_at=now,
            text="BTC long",
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(type=EntryType.LIMIT, price=100),
        stop_loss=90,
        take_profit=120,
        summary="Test",
        confidence=1,
        relation=OpenRelation.NEW,
        approval_mode=approval_mode,
        created_at=now - timedelta(days=2),
    )
    plan = ExecutionPlanner().plan(
        intent,
        ExecutionPolicy(trading_capital_usdt=Decimal("1000")),
        InstrumentContext(
            market_price=Decimal("105"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )

    async def run(error: Exception | None) -> None:
        lock = asyncio.Lock()

        class Store:
            async def get_active_position_strategies(self):
                return ()

            async def get_intent(self, intent_id):
                return intent

            async def get_execution_plan(self, intent_id):
                return plan

            async def claim_for_execution(self, *args, **kwargs):
                return True

            async def ensure_position_strategy(self, plan):
                assert lock.locked()

            async def mark_executed(self, intent_id, order_ids):
                assert lock.locked()
                assert order_ids == ("order-1",)

            async def register_zone(self, group_id, intent_id):
                assert lock.locked()

            async def bind_zone_order(self, order):
                assert lock.locked()

            async def record_entry_failure(
                self, intent_id, message, *, strategy_status
            ):
                assert lock.locked()
                assert strategy_status is (
                    StrategyStatus.CLOSED
                    if isinstance(error, EntryPreflightError)
                    else StrategyStatus.UNCERTAIN
                )

        class Executor:
            async def account_state(self):
                from types import SimpleNamespace

                return SimpleNamespace(positions=(), open_orders=())

            async def execute(self, plan):
                assert lock.locked()
                if error:
                    raise error
                return ("order-1",)

        coordinator = ExecutionCoordinator(
            store=Store(),
            executor=Executor(),
            planner=ExecutionPlanner(),
            max_age_seconds=3600,
            execution_lock=lock,
        )
        outcome = await coordinator.execute_intent(
            intent.intent_id,
            approval_mode=approval_mode,
        )
        assert outcome.status is (
            IntentStatus.FAILED if error else IntentStatus.EXECUTED
        )
        assert "stale" not in outcome.message

    asyncio.run(run(None))
    asyncio.run(run(RuntimeError("Exchange rejected order")))
    asyncio.run(run(EntryPreflightError("Entry price was passed through")))


def test_executed_plan_enrolls_its_zone_allocation_durably() -> None:
    """#123: a filled execution freezes its zone and binds entry identities."""
    from cautious_crypto_bro.domain import Entry, EntryType, OpenRelation, Side
    from cautious_crypto_bro.zone_ledger import zone_group_id

    now = datetime.now(UTC)
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=9,
            published_at=now,
            received_at=now,
            text="BTC long",
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(type=EntryType.LIMIT, price=100),
        stop_loss=90,
        take_profit=120,
        summary="Test",
        confidence=1,
        relation=OpenRelation.NEW,
        approval_mode=ApprovalMode.AUTO,
        created_at=now,
    )
    plan = ExecutionPlanner().plan(
        intent,
        ExecutionPolicy(trading_capital_usdt=Decimal("1000")),
        InstrumentContext(
            market_price=Decimal("105"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )

    enrolled: list[tuple] = []
    bound: list = []

    class LedgerStore:
        async def get_active_position_strategies(self):
            return ()

        async def get_intent(self, intent_id):
            return intent

        async def get_execution_plan(self, intent_id):
            return plan

        async def claim_for_execution(self, *args, **kwargs):
            return True

        async def ensure_position_strategy(self, plan):
            return None

        async def mark_executed(self, intent_id, order_ids):
            return None

        async def register_zone(self, group_id, intent_id):
            enrolled.append((group_id, intent_id))

        async def bind_zone_order(self, order):
            bound.append(order)

    class LedgerExecutor:
        async def account_state(self):
            from types import SimpleNamespace

            return SimpleNamespace(positions=(), open_orders=())

        async def execute(self, plan):
            return tuple(f"order-{index}" for index in range(len(plan.orders)))

    coordinator = ExecutionCoordinator(
        store=LedgerStore(),
        executor=LedgerExecutor(),
        planner=ExecutionPlanner(),
        max_age_seconds=3600,
        execution_lock=asyncio.Lock(),
    )

    outcome = asyncio.run(
        coordinator.execute_intent(
            intent.intent_id,
            approval_mode=ApprovalMode.AUTO,
        )
    )

    assert outcome.status is IntentStatus.EXECUTED
    assert enrolled == [(zone_group_id("BTCUSDT", Side.LONG), intent.intent_id)]
    assert len(bound) == len(plan.orders)
    prefix = f"ccb-v2-{intent.intent_id.hex[:20]}-"
    for order, planned, order_id in zip(
        bound, plan.orders, (f"order-{i}" for i in range(len(plan.orders))), strict=True
    ):
        assert order.zone_id == intent.intent_id
        assert order.order_id == order_id
        assert order.order_link_id == prefix + planned.name.lower()
        assert order.role == "ENTRY"
        assert order.quantity == planned.quantity


def test_zone_enrollment_failure_never_fails_a_filled_execution() -> None:
    from cautious_crypto_bro.domain import (
        Entry,
        EntryType,
        OpenRelation,
        Side,
        StoreError,
    )

    now = datetime.now(UTC)
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=11,
            published_at=now,
            received_at=now,
            text="BTC long",
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(type=EntryType.LIMIT, price=100),
        stop_loss=90,
        take_profit=120,
        summary="Test",
        confidence=1,
        relation=OpenRelation.NEW,
        approval_mode=ApprovalMode.AUTO,
        created_at=now,
    )
    plan = ExecutionPlanner().plan(
        intent,
        ExecutionPolicy(trading_capital_usdt=Decimal("1000")),
        InstrumentContext(
            market_price=Decimal("105"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )

    failures: list[str] = []

    class FailingLedgerStore:
        async def get_active_position_strategies(self):
            return ()

        async def get_intent(self, intent_id):
            return intent

        async def get_execution_plan(self, intent_id):
            return plan

        async def claim_for_execution(self, *args, **kwargs):
            return True

        async def ensure_position_strategy(self, plan):
            return None

        async def mark_executed(self, intent_id, order_ids):
            return None

        async def register_zone(self, group_id, intent_id):
            raise StoreError("Zone enrollment evidence store unavailable")

        async def bind_zone_order(self, order):
            raise AssertionError("Enrollment must stop at its first failure")

        async def record_entry_failure(self, *args, **kwargs):
            failures.append("record_entry_failure")

    class WorkingExecutor:
        async def account_state(self):
            from types import SimpleNamespace

            return SimpleNamespace(positions=(), open_orders=())

        async def execute(self, plan):
            return ("order-0",)

    coordinator = ExecutionCoordinator(
        store=FailingLedgerStore(),
        executor=WorkingExecutor(),
        planner=ExecutionPlanner(),
        max_age_seconds=3600,
        execution_lock=asyncio.Lock(),
    )

    outcome = asyncio.run(
        coordinator.execute_intent(
            intent.intent_id,
            approval_mode=ApprovalMode.AUTO,
        )
    )

    assert outcome.status is IntentStatus.EXECUTED
    assert failures == []


def test_zone_group_identity_is_deterministic_per_symbol_and_side() -> None:
    from cautious_crypto_bro.domain import Side
    from cautious_crypto_bro.zone_ledger import zone_group_id

    assert zone_group_id("BTCUSDT", Side.LONG) == zone_group_id("btcusdt", Side.LONG)
    assert zone_group_id("BTCUSDT", Side.LONG) != zone_group_id("BTCUSDT", Side.SHORT)
    assert zone_group_id("BTCUSDT", Side.LONG) != zone_group_id("ETHUSDT", Side.LONG)
