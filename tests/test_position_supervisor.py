import asyncio
import logging
from dataclasses import replace
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

from cautious_crypto_bro.bybit import (
    AccountPosition,
    AccountStateSummary,
)
from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionPolicy,
    PositionStrategy,
    Side,
    SourceMessage,
    StrategyStatus,
    TradingIntent,
)
from cautious_crypto_bro.execution import (
    ExecutionPlanner,
    InstrumentContext,
)
from cautious_crypto_bro.position_supervisor import (
    PositionSupervisor,
)


def plan(symbol: str = "BTCUSDT"):
    now = datetime.now(UTC)

    intent = TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=1,
            published_at=now,
            received_at=now,
            text="test",
        ),
        symbol=symbol,
        side=Side.LONG,
        entry=Entry(
            type=EntryType.MARKET,
        ),
        stop_loss=90,
        take_profit=None,
        summary="test",
        confidence=1,
    )

    return ExecutionPlanner().plan(
        intent,
        ExecutionPolicy(
            trading_capital_usdt=(Decimal("6800")),
            risk_per_trade_pct=(Decimal("1")),
            range_order_count=3,
        ),
        InstrumentContext(
            market_price=Decimal("100"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )


class Store:
    def __init__(
        self,
        state,
        strategy_plan,
    ) -> None:
        self.state = state
        self.strategy_plan = strategy_plan

    async def get_active_position_strategies(
        self,
    ):
        return (
            (
                self.state,
                self.strategy_plan,
            ),
        )

    async def save_position_strategy(
        self,
        state,
    ) -> None:
        self.state = state

    async def set_position_strategy_status(
        self,
        strategy_id,
        status,
    ) -> None:
        assert strategy_id == self.state.strategy_id

        self.state = self.state.model_copy(
            update={
                "status": status,
            }
        )


class Executor:
    def __init__(self) -> None:
        now = datetime.now(UTC)

        self.state = AccountStateSummary(
            as_of=now,
            positions=(
                AccountPosition(
                    symbol="BTCUSDT",
                    side=Side.LONG,
                    size=Decimal("4.08"),
                    avg_price=Decimal("100"),
                    mark_price=Decimal("106"),
                    unrealised_pnl=Decimal("0"),
                    status="Normal",
                    take_profit=None,
                    stop_loss=Decimal("90"),
                    break_even_price=(Decimal("100.2")),
                    trailing_stop=None,
                ),
            ),
            open_orders=(),
        )

        self.account_state_calls = 0
        self.cancelled_entries = 0
        self.cancelled_exits = 0
        self.protection = []
        self.exits = []

    async def account_state(self):
        self.account_state_calls += 1
        return self.state

    async def cancel_pending_entries(
        self,
        symbol,
    ):
        assert symbol == "BTCUSDT"
        self.cancelled_entries += 1
        return 2

    async def cancel_strategy_exits(
        self,
        symbol,
    ):
        assert symbol == "BTCUSDT"
        self.cancelled_exits += 1
        return 0

    async def market_context(
        self,
        symbol,
    ):
        assert symbol == "BTCUSDT"

        return InstrumentContext(
            market_price=Decimal("106"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

    async def set_position_protection(
        self,
        symbol,
        stop_loss,
        *,
        trailing_distance=None,
    ):
        self.protection.append(
            (
                symbol,
                stop_loss,
                trailing_distance,
            )
        )

        position = self.state.positions[0]

        self.state = AccountStateSummary(
            as_of=datetime.now(UTC),
            positions=(
                replace(
                    position,
                    stop_loss=stop_loss,
                    trailing_stop=(trailing_distance),
                ),
            ),
            open_orders=(self.state.open_orders),
        )

    async def cancel_order(
        self,
        symbol,
        order_id,
    ):
        raise AssertionError("No partial SL exists in this fixture")

    async def place_reduce_only_exit(
        self,
        **kwargs,
    ):
        self.exits.append(kwargs)
        return f"exit-{len(self.exits)}"


def test_supervisor_freezes_entries_and_protects_profit() -> None:
    strategy_plan = plan()

    state = PositionStrategy(
        strategy_id=(strategy_plan.intent_id),
        symbol="BTCUSDT",
        side=Side.LONG,
    )

    store = Store(
        state,
        strategy_plan,
    )

    executor = Executor()

    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
        poll_interval_seconds=1,
    )

    asyncio.run(supervisor.reconcile_once())

    assert executor.cancelled_entries == 1
    assert executor.cancelled_exits == 1

    assert len(executor.exits) == 3

    assert [item["price"] for item in executor.exits] == [
        Decimal("105.0"),
        Decimal("110.0"),
        Decimal("115.0"),
    ]

    assert [item["quantity"] for item in executor.exits] == [
        Decimal("1.020"),
        Decimal("1.020"),
        Decimal("1.020"),
    ]

    assert executor.protection == [
        (
            "BTCUSDT",
            Decimal("100.7"),
            Decimal("3.0"),
        )
    ]

    assert store.state.entry_frozen is True
    assert store.state.trailing_active is True

    assert store.state.protected_stop_loss == Decimal("100.7")
    assert store.state.trailing_distance == Decimal("3.0")

    assert store.state.status is StrategyStatus.PROFIT_PROTECTED

    assert store.state.exit_revision == 1


def test_supervisor_installs_exits_before_profit_threshold() -> None:
    strategy_plan = plan()

    state = PositionStrategy(
        strategy_id=(strategy_plan.intent_id),
        symbol="BTCUSDT",
        side=Side.LONG,
    )

    store = Store(
        state,
        strategy_plan,
    )

    executor = Executor()

    position = executor.state.positions[0]

    executor.state = AccountStateSummary(
        as_of=datetime.now(UTC),
        positions=(
            replace(
                position,
                mark_price=Decimal("102"),
                break_even_price=(Decimal("100.2")),
            ),
        ),
        open_orders=(),
    )

    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
        poll_interval_seconds=1,
    )

    asyncio.run(supervisor.reconcile_once())

    # Still accumulating: E2/E3 remain live.
    assert executor.cancelled_entries == 0

    # Nothing existed to cancel on first install.
    assert executor.cancelled_exits == 0

    # Fixed exits must exist immediately,
    # well before +0.5R protection activation.
    assert len(executor.exits) == 3

    assert [item["price"] for item in executor.exits] == [
        Decimal("105.0"),
        Decimal("110.0"),
        Decimal("115.0"),
    ]

    assert [item["quantity"] for item in executor.exits] == [
        Decimal("1.020"),
        Decimal("1.020"),
        Decimal("1.020"),
    ]

    # Catastrophe stop is already correct on
    # Bybit, so reconciliation must not submit
    # an identical trading-stop mutation.
    assert executor.protection == []

    assert store.state.entry_frozen is False
    assert store.state.trailing_active is False

    assert store.state.protected_stop_loss == Decimal("90.0")
    assert store.state.trailing_distance is None

    assert store.state.status is StrategyStatus.OPEN_RISK

    assert store.state.exit_revision == 1


def test_uncertain_strategy_is_quarantined(caplog) -> None:
    strategy_plan = plan()

    state = PositionStrategy(
        strategy_id=(strategy_plan.intent_id),
        symbol="BTCUSDT",
        side=Side.LONG,
        status=StrategyStatus.UNCERTAIN,
        entry_frozen=True,
        last_position_qty=Decimal("4.08"),
        last_avg_price=Decimal("100"),
    )

    store = Store(
        state,
        strategy_plan,
    )

    executor = Executor()

    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
        poll_interval_seconds=1,
    )

    with caplog.at_level(
        logging.WARNING, logger="cautious_crypto_bro.position_supervisor"
    ):
        asyncio.run(supervisor.reconcile_once())
        asyncio.run(supervisor.reconcile_once())

    assert executor.account_state_calls == 0
    assert sum("is UNCERTAIN" in record.message for record in caplog.records) == 1
    assert executor.cancelled_entries == 0
    assert executor.cancelled_exits == 0
    assert executor.protection == []
    assert executor.exits == []

    assert store.state.status is StrategyStatus.UNCERTAIN


def test_uncertain_strategy_does_not_block_other_symbols() -> None:
    uncertain_plan = plan()
    active_plan = plan("ETHUSDT")
    uncertain = PositionStrategy(
        strategy_id=uncertain_plan.intent_id,
        symbol="BTCUSDT",
        side=Side.LONG,
        status=StrategyStatus.UNCERTAIN,
    )
    active = PositionStrategy(
        strategy_id=active_plan.intent_id,
        symbol="ETHUSDT",
        side=Side.LONG,
        status=StrategyStatus.CLOSING,
    )

    class MixedStore(Store):
        def __init__(self) -> None:
            super().__init__(uncertain, uncertain_plan)
            self.active = active

        async def get_active_position_strategies(self):
            return ((self.state, self.strategy_plan), (self.active, active_plan))

        async def save_position_strategy(self, state) -> None:
            assert state.strategy_id == active.strategy_id
            self.active = state

    store = MixedStore()
    executor = Executor()
    executor.state = replace(
        executor.state,
        positions=(replace(executor.state.positions[0], symbol="ETHUSDT"),),
    )
    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
    )

    asyncio.run(supervisor.reconcile_once())

    assert executor.account_state_calls == 1
    assert store.state.status is StrategyStatus.UNCERTAIN
    assert store.active.status is StrategyStatus.CLOSING
    assert store.active.last_position_qty == Decimal("4.08")


def test_reduce_rebalance_skips_unchanged_protection() -> None:
    strategy_plan = plan()

    state = PositionStrategy(
        strategy_id=(strategy_plan.intent_id),
        symbol="BTCUSDT",
        side=Side.LONG,
        status=StrategyStatus.OPEN_RISK,
        entry_frozen=True,
        base_position_qty=Decimal("4.08"),
        last_position_qty=Decimal("4.08"),
        last_avg_price=Decimal("100"),
        protected_stop_loss=Decimal("90"),
        exit_revision=1,
        rebalance_needed=True,
    )

    store = Store(
        state,
        strategy_plan,
    )

    executor = Executor()

    position = executor.state.positions[0]

    executor.state = AccountStateSummary(
        as_of=datetime.now(UTC),
        positions=(
            replace(
                position,
                size=Decimal("3.06"),
                mark_price=Decimal("102"),
                stop_loss=Decimal("90"),
                trailing_stop=None,
            ),
        ),
        open_orders=(),
    )

    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
        poll_interval_seconds=1,
    )

    asyncio.run(supervisor.reconcile_once())

    assert executor.protection == []

    assert len(executor.exits) == 3

    assert [item["quantity"] for item in executor.exits] == [
        Decimal("0.765"),
        Decimal("0.765"),
        Decimal("0.765"),
    ]

    assert store.state.entry_frozen is True
    assert store.state.rebalance_needed is False
    assert store.state.exit_revision == 2

    assert store.state.last_position_qty == Decimal("3.06")

    assert store.state.protected_stop_loss == Decimal("90")

    assert store.state.status is StrategyStatus.OPEN_RISK
