import asyncio
import logging
from dataclasses import replace
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from cautious_crypto_bro.domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    Entry,
    EntryType,
    ExecutionPolicy,
    InstrumentContext,
    PositionStrategy,
    Side,
    SourceMessage,
    StoreError,
    StrategyStatus,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner
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
        self.cancelled_orders = []
        self.protection = []
        self.exits = []

    async def strategy_order(self, symbol, link_id):
        return next(
            (
                o
                for o in self.state.open_orders
                if o.symbol == symbol and o.order_link_id == link_id
            ),
            None,
        )

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
                    trailing_stop=(
                        trailing_distance
                        if trailing_distance is not None
                        else position.trailing_stop
                    ),
                ),
            ),
            open_orders=(self.state.open_orders),
        )

    async def cancel_order(
        self,
        symbol,
        order_id,
    ):
        self.cancelled_orders.append(order_id)
        self.state = replace(
            self.state,
            open_orders=tuple(
                order for order in self.state.open_orders if order.order_id != order_id
            ),
        )

    async def place_reduce_only_exit(
        self,
        **kwargs,
    ):
        self.exits.append(kwargs)
        self.state = replace(
            self.state,
            open_orders=(
                *self.state.open_orders,
                AccountOrder(
                    symbol=kwargs["symbol"],
                    side=(
                        Side.SHORT
                        if kwargs["position_side"] is Side.LONG
                        else Side.LONG
                    ),
                    order_type="Limit",
                    status="New",
                    quantity=kwargs["quantity"],
                    remaining_quantity=kwargs["quantity"],
                    price=kwargs["price"],
                    avg_price=None,
                    order_id=f"exit-{len(self.exits)}",
                    order_link_id=kwargs["order_link_id"],
                    reduce_only=True,
                    updated_at=datetime.now(UTC),
                ),
            ),
        )
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
    assert executor.cancelled_exits == 0

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


def test_active_strategy_replaces_legacy_partial_stops_once() -> None:
    strategy_plan = plan()
    state = PositionStrategy(
        strategy_id=strategy_plan.intent_id,
        symbol="BTCUSDT",
        side=Side.LONG,
        status=StrategyStatus.OPEN_RISK,
        entry_frozen=True,
        last_position_qty=Decimal("4.08"),
        last_avg_price=Decimal("100"),
        exit_revision=1,
    )
    store = Store(state, strategy_plan)
    executor = Executor()
    executor.state = replace(
        executor.state,
        positions=(
            replace(
                executor.state.positions[0],
                stop_loss=None,
                mark_price=Decimal("102"),
            ),
        ),
        open_orders=tuple(
            AccountOrder(
                symbol="BTCUSDT",
                side=Side.SHORT,
                order_type="Market",
                status="Untriggered",
                quantity=Decimal("1.36"),
                remaining_quantity=Decimal("1.36"),
                price=None,
                avg_price=None,
                order_id=f"partial-{index}",
                order_link_id="",
                parent_order_link_id=(
                    f"ccb-v2-{strategy_plan.intent_id.hex[:20]}-e{index}"
                    if index <= 3
                    else ""
                ),
                reduce_only=False,
                updated_at=datetime.now(UTC),
                stop_order_type="PartialStopLoss",
                trigger_price=price,
            )
            for index, price in enumerate(
                (Decimal("90"), Decimal("90"), Decimal("90"), Decimal("85")),
                start=1,
            )
        ),
    )
    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
    )
    asyncio.run(supervisor.reconcile_once())
    asyncio.run(supervisor.reconcile_once())

    assert executor.protection == [("BTCUSDT", Decimal("90"), None)]
    assert executor.cancelled_orders == ["partial-1", "partial-2", "partial-3"]
    assert [o.order_id for o in executor.state.open_orders] == ["partial-4"]
    assert store.state.status is StrategyStatus.OPEN_RISK


def test_failed_full_stop_verification_does_not_cancel_partials() -> None:
    strategy_plan = plan()
    state = PositionStrategy(
        strategy_id=strategy_plan.intent_id,
        symbol="BTCUSDT",
        side=Side.LONG,
        status=StrategyStatus.OPEN_RISK,
        entry_frozen=True,
        exit_revision=1,
    )
    store = Store(state, strategy_plan)
    executor = Executor()
    executor.state = replace(
        executor.state,
        positions=(replace(executor.state.positions[0], stop_loss=None),),
        open_orders=(
            AccountOrder(
                symbol="BTCUSDT",
                side=Side.SHORT,
                order_type="Market",
                status="Untriggered",
                quantity=Decimal("4.08"),
                remaining_quantity=Decimal("4.08"),
                price=None,
                avg_price=None,
                order_id="legacy-sl",
                order_link_id="",
                parent_order_link_id=(f"ccb-v2-{strategy_plan.intent_id.hex[:20]}-e1"),
                reduce_only=False,
                updated_at=datetime.now(UTC),
                stop_order_type="PartialStopLoss",
                trigger_price=Decimal("90"),
            ),
        ),
    )

    async def no_confirmation(*args, **kwargs):
        return None

    executor.set_position_protection = no_confirmation
    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
    )

    import pytest

    with pytest.raises(RuntimeError, match="did not confirm expected"):
        asyncio.run(supervisor.reconcile_once())

    assert executor.cancelled_orders == []
    assert executor.state.open_orders[0].order_id == "legacy-sl"


def test_unattributed_partial_stop_requires_manual_review() -> None:
    strategy_plan = plan()
    state = PositionStrategy(
        strategy_id=strategy_plan.intent_id,
        symbol="BTCUSDT",
        side=Side.LONG,
        status=StrategyStatus.OPEN_RISK,
        entry_frozen=True,
        exit_revision=1,
    )
    store = Store(state, strategy_plan)
    executor = Executor()
    executor.state = replace(
        executor.state,
        open_orders=(
            AccountOrder(
                symbol="BTCUSDT",
                side=Side.SHORT,
                order_type="Market",
                status="Untriggered",
                quantity=Decimal("4.08"),
                remaining_quantity=Decimal("4.08"),
                price=None,
                avg_price=None,
                order_id="manual-stop",
                order_link_id="",
                reduce_only=False,
                updated_at=datetime.now(UTC),
                stop_order_type="PartialStopLoss",
                trigger_price=Decimal("90"),
            ),
        ),
    )
    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
    )
    asyncio.run(supervisor.reconcile_once())

    assert store.state.status is StrategyStatus.MANUAL_OVERRIDE
    assert executor.protection == []
    assert executor.cancelled_orders == []
    assert [order.order_id for order in executor.state.open_orders] == ["manual-stop"]


def test_interrupted_exit_install_reuses_matching_order() -> None:
    strategy_plan = plan()
    store = Store(
        PositionStrategy(
            strategy_id=strategy_plan.intent_id,
            symbol="BTCUSDT",
            side=Side.LONG,
        ),
        strategy_plan,
    )
    executor = Executor()
    executor.state = replace(
        executor.state,
        positions=(replace(executor.state.positions[0], mark_price=Decimal("102")),),
    )
    original_place = executor.place_reduce_only_exit
    failed_once = False

    async def fail_second_exit(**kwargs):
        nonlocal failed_once
        if kwargs["order_link_id"].endswith("-t2r1") and not failed_once:
            failed_once = True
            raise RuntimeError("Temporary exchange failure")
        return await original_place(**kwargs)

    executor.place_reduce_only_exit = fail_second_exit
    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=asyncio.Lock(),
    )

    import pytest

    with pytest.raises(RuntimeError, match="Temporary exchange failure"):
        asyncio.run(supervisor.reconcile_once())

    assert len(executor.exits) == 1
    asyncio.run(supervisor.reconcile_once())
    assert len(executor.exits) == 3
    assert store.state.exit_revision == 1
    assert len({item.order_link_id for item in executor.state.open_orders}) == 3


def test_supervisor_never_uses_prelock_strategy_snapshot() -> None:
    strategy_plan = plan()
    state = PositionStrategy(
        strategy_id=strategy_plan.intent_id,
        symbol="BTCUSDT",
        side=Side.LONG,
    )
    store = Store(state, strategy_plan)
    executor = Executor()
    mutation_lock = asyncio.Lock()
    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=mutation_lock,
    )

    async def scenario() -> None:
        await mutation_lock.acquire()
        reconciliation = asyncio.create_task(supervisor.reconcile_once())
        await asyncio.sleep(0)
        store.state = store.state.model_copy(
            update={"status": StrategyStatus.UNCERTAIN}
        )
        mutation_lock.release()
        await reconciliation

    asyncio.run(scenario())
    assert store.state.status is StrategyStatus.UNCERTAIN
    assert executor.account_state_calls == 0
    assert executor.exits == []


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


def test_failed_strategy_does_not_starve_other_symbols(monkeypatch, caplog) -> None:
    plans = [plan(symbol) for symbol in ("BTCUSDT", "ETHUSDT")]
    states = [
        PositionStrategy(
            strategy_id=item.intent_id,
            symbol=item.symbol,
            side=Side.LONG,
            status=StrategyStatus.CLOSING,
        )
        for item in plans
    ]
    store = Store(states[0], plans[0])
    store.get_active_position_strategies = AsyncMock(
        return_value=list(zip(states, plans, strict=True))
    )
    store.save_position_strategy = AsyncMock()
    executor = Executor()
    executor.state = replace(
        executor.state,
        positions=(replace(executor.state.positions[0], symbol="ETHUSDT"),),
    )
    supervisor = PositionSupervisor(
        store=store, executor=executor, mutation_lock=asyncio.Lock()
    )
    reconcile = supervisor._reconcile
    visited = []

    async def fail_after_mutation(state, strategy_plan, account):
        visited.append(state.symbol)
        if state.symbol == "BTCUSDT":
            # An exchange mutation can succeed before its response is lost.
            position = executor.state.positions[0]
            executor.state = replace(
                executor.state,
                positions=(replace(position, size=position.size / 2),),
            )
            raise RuntimeError("BTC reconciliation failed")
        return await reconcile(state, strategy_plan, account)

    monkeypatch.setattr(supervisor, "_reconcile", fail_after_mutation)
    for _ in range(2):
        # Callers such as startup still receive the failure, after healthy work.
        with pytest.raises(RuntimeError, match="BTC reconciliation failed"):
            asyncio.run(supervisor.reconcile_once())

    assert visited == ["BTCUSDT", "ETHUSDT", "BTCUSDT", "ETHUSDT"]
    assert executor.account_state_calls == 4
    assert [
        call.args[0].last_position_qty
        for call in store.save_position_strategy.await_args_list
    ] == [Decimal("2.04"), Decimal("1.02")]
    assert str(states[0].strategy_id) in caplog.text
    assert "BTCUSDT" in caplog.text
    assert not supervisor._mutation_lock.locked()


@pytest.mark.parametrize(
    "failure", ["account_read", "cancelled", "store_read", "store_write"]
)
def test_supervisor_does_not_continue_after_global_failure(
    monkeypatch, failure
) -> None:
    strategy_plan = plan()
    state = PositionStrategy(
        strategy_id=strategy_plan.intent_id,
        symbol="BTCUSDT",
        side=Side.LONG,
        status=StrategyStatus.CLOSING,
    )
    store = Store(state, strategy_plan)
    other_plan = plan("ETHUSDT")
    store.get_active_position_strategies = AsyncMock(
        return_value=[
            (state, strategy_plan),
            (
                state.model_copy(
                    update={"strategy_id": other_plan.intent_id, "symbol": "ETHUSDT"}
                ),
                other_plan,
            ),
        ]
    )
    executor = Executor()
    supervisor = PositionSupervisor(
        store=store, executor=executor, mutation_lock=asyncio.Lock()
    )
    reconcile = AsyncMock(wraps=supervisor._reconcile)
    monkeypatch.setattr(supervisor, "_reconcile", reconcile)
    error = (
        asyncio.CancelledError()
        if failure == "cancelled"
        else RuntimeError("unavailable")
    )
    if failure == "account_read":
        monkeypatch.setattr(executor, "account_state", AsyncMock(side_effect=error))
    elif failure == "store_read":
        monkeypatch.setattr(
            store, "get_active_position_strategies", AsyncMock(side_effect=error)
        )
    elif failure == "store_write":
        error = StoreError("database or disk is full")
        monkeypatch.setattr(
            store, "save_position_strategy", AsyncMock(side_effect=error)
        )
    else:
        reconcile.side_effect = error

    with pytest.raises(type(error)):
        asyncio.run(supervisor.reconcile_once())
    assert reconcile.await_count == (
        1 if failure in {"cancelled", "store_write"} else 0
    )
    assert not supervisor._mutation_lock.locked()


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
