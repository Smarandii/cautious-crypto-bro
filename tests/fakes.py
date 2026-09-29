from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from cautious_crypto_bro.domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    ApprovalMode,
    ClosedPnlRecord,
    Entry,
    EntryType,
    ExecutionPlan,
    ExecutionPolicy,
    MarketPrimaryExecutionResult,
    PositionActionExecutionResult,
    PositionActionIntent,
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
from cautious_crypto_bro.ports import (
    AccountGateway,
    PositionSupervisorStore,
)


def _now() -> datetime:
    return datetime.now(UTC)


def make_account_position(
    symbol: str = "BTCUSDT",
    *,
    side: Side = Side.LONG,
    size: Decimal = Decimal("4.08"),
    avg_price: Decimal = Decimal("100"),
    mark_price: Decimal = Decimal("106"),
    stop_loss: Decimal | None = Decimal("90"),
    trailing_stop: Decimal | None = None,
) -> AccountPosition:
    return AccountPosition(
        symbol=symbol,
        side=side,
        size=size,
        avg_price=avg_price,
        mark_price=mark_price,
        unrealised_pnl=Decimal("0"),
        status="Normal",
        take_profit=None,
        stop_loss=stop_loss,
        break_even_price=avg_price + Decimal("0.2"),
        trailing_stop=trailing_stop,
    )


def make_account_state(
    position: AccountPosition | None = None,
    open_orders: Sequence[AccountOrder] = (),
) -> AccountStateSummary:
    return AccountStateSummary(
        as_of=_now(),
        positions=(position,) if position else (),
        open_orders=tuple(open_orders),
    )


def make_execution_plan(
    symbol: str = "BTCUSDT",
    *,
    market_price: Decimal = Decimal("100"),
    stop_loss: Decimal = Decimal("90"),
) -> ExecutionPlan:
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=1,
            published_at=_now(),
            received_at=_now(),
            text="test",
        ),
        symbol=symbol,
        side=Side.LONG,
        entry=Entry(type=EntryType.MARKET),
        stop_loss=stop_loss,
        take_profit=None,
        summary="test",
        confidence=1,
    )

    return ExecutionPlanner().plan(
        intent,
        ExecutionPolicy(
            trading_capital_usdt=Decimal("6800"),
            risk_per_trade_pct=Decimal("1"),
            range_order_count=3,
        ),
        InstrumentContext(
            market_price=market_price,
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )


def make_position_strategy(
    plan: ExecutionPlan,
    *,
    status: StrategyStatus = StrategyStatus.OPEN_RISK,
) -> PositionStrategy:
    return PositionStrategy(
        strategy_id=plan.intent_id,
        symbol=plan.symbol,
        side=plan.side,
        status=status,
    )


class FakeAccountGateway:
    """In-memory AccountGateway for unit tests.

    The implementation records interactions and mutates ``state`` to reflect
    the calls that change market/account state.
    """

    def __init__(
        self,
        state: AccountStateSummary | None = None,
        market: InstrumentContext | None = None,
    ) -> None:
        self.state = state or make_account_state(make_account_position())
        self.market = market or InstrumentContext(
            market_price=Decimal("106"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        self.account_state_calls = 0
        self.cancelled_entries = 0
        self.cancelled_exits = 0
        self.cancelled_orders: list[str] = []
        self.protection: list[tuple[str, Decimal, Decimal | None]] = []
        self.exits: list[dict[str, Any]] = []

    async def market_context(self, symbol: str) -> InstrumentContext:
        assert (
            symbol == self.state.positions[0].symbol if self.state.positions else True
        )
        return self.market

    async def account_state(self) -> AccountStateSummary:
        self.account_state_calls += 1
        return self.state

    async def wallet_balance_usdt(self) -> Decimal:
        return Decimal("10000")

    async def closed_pnl_history(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[ClosedPnlRecord, ...]:
        return ()

    async def execute(self, plan: ExecutionPlan) -> tuple[str, ...]:
        raise NotImplementedError

    async def execute_market_primary(
        self,
        plan: ExecutionPlan,
    ) -> MarketPrimaryExecutionResult:
        raise NotImplementedError

    async def execute_remaining_entries(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        raise NotImplementedError

    async def execute_position_action(
        self,
        action: PositionActionIntent,
    ) -> PositionActionExecutionResult:
        raise NotImplementedError

    async def cancel_pending_entries(self, symbol: str) -> int:
        self.cancelled_entries += 1
        return 2

    async def cancel_strategy_exits(self, symbol: str) -> int:
        self.cancelled_exits += 1
        return 0

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        self.cancelled_orders.append(order_id)
        self.state = replace(
            self.state,
            open_orders=tuple(
                order for order in self.state.open_orders if order.order_id != order_id
            ),
        )

    async def set_position_protection(
        self,
        symbol: str,
        stop_loss: Decimal,
        *,
        trailing_distance: Decimal | None = None,
    ) -> None:
        self.protection.append((symbol, stop_loss, trailing_distance))

        if not self.state.positions:
            return

        position = self.state.positions[0]
        self.state = replace(
            self.state,
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
        )

    async def place_reduce_only_exit(
        self,
        *,
        symbol: str,
        position_side: Side,
        quantity: Decimal,
        price: Decimal,
        order_link_id: str,
    ) -> str:
        self.exits.append(
            {
                "symbol": symbol,
                "position_side": position_side,
                "quantity": quantity,
                "price": price,
                "order_link_id": order_link_id,
            }
        )
        order_id = f"exit-{len(self.exits)}"
        self.state = replace(
            self.state,
            open_orders=(
                *self.state.open_orders,
                AccountOrder(
                    symbol=symbol,
                    side=Side.SHORT if position_side is Side.LONG else Side.LONG,
                    order_type="Limit",
                    status="New",
                    quantity=quantity,
                    remaining_quantity=quantity,
                    price=price,
                    avg_price=None,
                    order_id=order_id,
                    order_link_id=order_link_id,
                    reduce_only=True,
                    updated_at=_now(),
                ),
            ),
        )
        return order_id

    async def strategy_order(self, symbol: str, link_id: str) -> AccountOrder | None:
        return next(
            (
                order
                for order in self.state.open_orders
                if order.symbol == symbol and order.order_link_id == link_id
            ),
            None,
        )

    def close(self) -> None:
        pass


class FakePositionSupervisorStore:
    """In-memory store satisfying PositionSupervisorStore."""

    def __init__(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
    ) -> None:
        self.state = state
        self.plan = plan
        self.saved_states: list[PositionStrategy] = []

    async def ensure_position_strategy(self, plan: ExecutionPlan) -> None:
        pass

    async def get_active_position_strategies(
        self,
    ) -> tuple[tuple[PositionStrategy, ExecutionPlan], ...]:
        return ((self.state, self.plan),)

    async def save_position_strategy(self, state: PositionStrategy) -> None:
        self.state = state
        self.saved_states.append(state)

    async def set_position_strategy_status(
        self,
        strategy_id: UUID,
        status: StrategyStatus,
    ) -> None:
        assert strategy_id == self.state.strategy_id
        self.state = self.state.model_copy(update={"status": status})

    async def request_strategy_rebalance(self, symbol: str) -> None:
        pass

    async def request_strategy_close(self, symbol: str) -> None:
        pass

    # PositionActionRepository methods required by the combined protocol.

    async def get_position_action(
        self,
        action_id: UUID,
    ) -> PositionActionIntent | None:
        return None

    async def claim_position_action_for_execution(
        self,
        action_id: UUID,
        user_id: int | None,
        *,
        expected_approval_mode: ApprovalMode | None = None,
    ) -> bool:
        return True

    async def mark_position_action_skipped(
        self,
        action_id: UUID,
        user_id: int,
    ) -> bool:
        return True

    async def complete_position_action(
        self,
        action: PositionActionIntent,
        order_id: str,
    ) -> None:
        pass

    async def mark_position_action_failed(
        self,
        action_id: UUID,
        error: str,
    ) -> None:
        pass

    async def mark_position_action_uncertain(
        self,
        action_id: UUID,
        symbol: str,
        order_id: str | None,
        error: str,
    ) -> None:
        pass

    async def get_recent_executed_closes(
        self,
        *,
        limit: int = 100,
    ) -> tuple[PositionActionIntent, ...]:
        return ()

    async def entry_cancellation_targets(
        self,
        action: PositionActionIntent,
    ) -> tuple[UUID, ...]:
        return ()

    async def record_entry_cancellation(
        self,
        action: PositionActionIntent,
        targets: tuple[UUID, ...],
        *,
        complete: bool = False,
        flat: bool = False,
        error: str | None = None,
    ) -> None:
        pass

    # IntentRepository methods required by the combined protocol.

    async def get_intent(self, intent_id: UUID) -> TradingIntent | None:
        return None

    async def get_execution_plan(
        self,
        intent_id: UUID,
    ) -> ExecutionPlan | None:
        return None

    async def update_execution_plan(self, plan: ExecutionPlan) -> None:
        pass

    async def claim_for_execution(
        self,
        intent_id: UUID,
        user_id: int | None,
        *,
        expected_approval_mode: ApprovalMode | None = None,
    ) -> bool:
        return True

    async def mark_skipped(
        self,
        intent_id: UUID,
        user_id: int,
    ) -> bool:
        return True

    async def mark_executed(
        self,
        intent_id: UUID,
        order_ids: tuple[str, ...],
    ) -> None:
        pass

    async def mark_failed(
        self,
        intent_id: UUID,
        error: str,
    ) -> None:
        pass

    async def get_recent_source_intents(
        self,
        channel_id: int,
        *,
        limit: int = 50,
    ) -> tuple[TradingIntent, ...]:
        return ()


# Structural protocol conformance is checked by passing the fakes to functions
# typed with the protocols below. These are intentionally tiny: they prove the
# fakes can stand in for the real infrastructure without importing concrete
# implementations.


def _require_account_gateway(gateway: AccountGateway) -> AccountGateway:
    return gateway


def _require_position_supervisor_store(
    store: PositionSupervisorStore,
) -> PositionSupervisorStore:
    return store
