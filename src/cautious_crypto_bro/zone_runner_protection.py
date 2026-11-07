"""Derive per-zone runner activation, locked-profit stop, and trailing distance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from .domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    ExecutionPlan,
    InstrumentContext,
    Side,
    TradeExecutionError,
)
from .entry_ownership import recover_entry_bindings
from .position_management.trailing_stop_manager import TrailingStopManager
from .zone_ledger import ZoneExecution, ZoneOrder, zone_group_id

if TYPE_CHECKING:
    from .ports import (
        AccountGateway,
        ExecutionCoordinatorStore,
        PositionSupervisorStore,
    )


@dataclass(frozen=True, slots=True)
class ZoneRunnerState:
    zone_id: UUID
    entered_quantity: Decimal
    closed_quantity: Decimal
    remaining_quantity: Decimal
    entry_average_price: Decimal | None
    entry_break_even_price: Decimal | None
    live_r: Decimal | None
    trailing_active: bool
    protected_stop_loss: Decimal | None
    trailing_distance: Decimal | None


def _zone_entry_economics(
    side: Side,
    stop_loss: Decimal,
    entry_fills: Sequence[ZoneExecution],
    entered_quantity: Decimal,
) -> tuple[Decimal, Decimal]:
    notional = sum((fill.quantity * fill.price for fill in entry_fills), Decimal("0"))
    average = notional / entered_quantity
    fees = max(Decimal("0"), sum((fill.fee for fill in entry_fills), Decimal("0")))
    break_even = (
        (notional + fees) / entered_quantity
        if side is Side.LONG
        else (notional - fees) / entered_quantity
    )
    valid_stop = stop_loss < average if side is Side.LONG else average < stop_loss
    if not valid_stop or break_even <= 0:
        raise ValueError("Zone entry average conflicts with frozen stop geometry")
    return average, break_even


def derive_zone_runner_state(
    plan: ExecutionPlan,
    orders: Sequence[ZoneOrder],
    executions: Sequence[ZoneExecution],
    *,
    mark_price: Decimal,
    context: InstrumentContext,
    previous_protected_stop: Decimal | None = None,
    already_trailing: bool = False,
) -> ZoneRunnerState:
    """Compute one zone's runner protection state without reading net-position averages."""
    if mark_price <= 0:
        raise ValueError("Zone runner evaluation requires a positive mark price")
    owned = {
        order.order_id: order for order in orders if order.zone_id == plan.intent_id
    }
    fills = [fill for fill in executions if fill.order_id in owned]
    entry_fills = [fill for fill in fills if owned[fill.order_id].role == "ENTRY"]
    close_fills = [fill for fill in fills if owned[fill.order_id].role != "ENTRY"]
    entered = sum((fill.quantity for fill in entry_fills), Decimal("0"))
    closed = sum((fill.quantity for fill in close_fills), Decimal("0"))
    if closed > entered:
        raise ValueError("Zone closed quantity exceeds entered quantity")
    remaining = entered - closed
    if entered == 0:
        return ZoneRunnerState(
            zone_id=plan.intent_id,
            entered_quantity=Decimal("0"),
            closed_quantity=Decimal("0"),
            remaining_quantity=Decimal("0"),
            entry_average_price=None,
            entry_break_even_price=None,
            live_r=None,
            trailing_active=False,
            protected_stop_loss=None,
            trailing_distance=None,
        )
    average, break_even = _zone_entry_economics(
        plan.side, plan.stop_loss, entry_fills, entered
    )
    if remaining == 0:
        return ZoneRunnerState(
            zone_id=plan.intent_id,
            entered_quantity=entered,
            closed_quantity=closed,
            remaining_quantity=Decimal("0"),
            entry_average_price=average,
            entry_break_even_price=break_even,
            live_r=None,
            trailing_active=False,
            protected_stop_loss=None,
            trailing_distance=None,
        )
    risk_distance = abs(average - plan.stop_loss)
    favorable = mark_price - average if plan.side is Side.LONG else average - mark_price
    live_r = favorable / risk_distance
    strategy = plan.policy.strategy_v2
    trailing_active = already_trailing or (live_r >= strategy.trailing_activation_r)
    if not trailing_active:
        return ZoneRunnerState(
            zone_id=plan.intent_id,
            entered_quantity=entered,
            closed_quantity=closed,
            remaining_quantity=remaining,
            entry_average_price=average,
            entry_break_even_price=break_even,
            live_r=live_r,
            trailing_active=False,
            protected_stop_loss=previous_protected_stop or plan.stop_loss,
            trailing_distance=None,
        )
    zone_position = AccountPosition(
        symbol=plan.symbol,
        side=plan.side,
        size=remaining,
        avg_price=average,
        mark_price=mark_price,
        unrealised_pnl=remaining * favorable,
        status="Open",
        take_profit=None,
        stop_loss=previous_protected_stop or plan.stop_loss,
        break_even_price=break_even,
    )
    protected_stop = TrailingStopManager.protected_stop(
        plan, zone_position, context, previous=previous_protected_stop
    )
    trailing_distance = TrailingStopManager.trailing_distance(
        plan, zone_position, context
    )
    return ZoneRunnerState(
        zone_id=plan.intent_id,
        entered_quantity=entered,
        closed_quantity=closed,
        remaining_quantity=remaining,
        entry_average_price=average,
        entry_break_even_price=break_even,
        live_r=live_r,
        trailing_active=True,
        protected_stop_loss=protected_stop,
        trailing_distance=trailing_distance,
    )


def _matches_zone_entry(
    plan: ExecutionPlan,
    order: AccountOrder,
    by_order_id: dict[str, ZoneOrder],
    bound_links: set[str],
    prefix: str,
) -> bool:
    if order.symbol != plan.symbol or order.kind != "ENTRY":
        return False
    bound = by_order_id.get(order.order_id)
    if bound is None:
        if order.order_link_id in bound_links or order.order_link_id.startswith(prefix):
            raise TradeExecutionError("Unbound or mismatched open entry order for zone")
        return False
    if order.order_link_id != bound.order_link_id or order.side is not plan.side:
        raise TradeExecutionError("Open entry order conflicts with zone binding")
    return True


def zone_pending_entry_orders(
    plan: ExecutionPlan,
    orders: Sequence[ZoneOrder],
    open_orders: Sequence[AccountOrder],
) -> tuple[AccountOrder, ...]:
    """Return open entry orders bound to one zone, rejecting unbound prefix matches."""
    zone_entries = [
        order
        for order in orders
        if order.zone_id == plan.intent_id and order.role == "ENTRY"
    ]
    by_order_id = {order.order_id: order for order in zone_entries}
    bound_links = {order.order_link_id for order in zone_entries}
    prefix = f"ccb-v2-{plan.intent_id.hex[:20]}-e"
    return tuple(
        order
        for order in open_orders
        if _matches_zone_entry(plan, order, by_order_id, bound_links, prefix)
    )


async def cancel_zone_pending_entries(
    plan: ExecutionPlan,
    store: ExecutionCoordinatorStore | PositionSupervisorStore,
    executor: AccountGateway,
) -> tuple[AccountStateSummary, tuple[str, ...]]:
    """Cancel one zone's pending entries and confirm across two account snapshots."""
    await recover_entry_bindings(plan, store, executor)
    group = zone_group_id(plan.symbol, plan.side)
    orders = await store.zone_bound_orders(group)
    account = await executor.account_state()
    pending = zone_pending_entry_orders(plan, orders, account.open_orders)
    cancelled: list[str] = []
    for order in pending:
        await executor.cancel_order(plan.symbol, order.order_id)
        cancelled.append(order.order_id)
    for _ in range(2):
        account = await executor.account_state()
        if zone_pending_entry_orders(plan, orders, account.open_orders):
            raise TradeExecutionError(
                "Bybit zone entry cancellation is not confirmed; refusing to freeze zone"
            )
    return account, tuple(cancelled)
