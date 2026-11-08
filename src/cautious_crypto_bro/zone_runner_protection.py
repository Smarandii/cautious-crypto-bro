"""Derive per-zone runner activation, locked-profit stop, and trailing distance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
from .portfolio_risk import PortfolioRiskUnavailable
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


@dataclass(frozen=True, slots=True)
class ZoneGroupRunnerContract:
    states: tuple[ZoneRunnerState, ...]
    active_zone_count: int
    trailing_zone_count: int
    native_trailing_status: str
    operator_tradeoff_required: bool
    protection_verified: bool
    trading_enabled: bool


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


def _reconcile_parent_fills(
    orders: Sequence[ZoneOrder],
    executions: Sequence[ZoneExecution],
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    known = {order.order_id: order for order in orders}
    entries = {order.order_link_id: order for order in orders if order.role == "ENTRY"}
    entered = {order.order_id: Decimal("0") for order in entries.values()}
    closed = {order.order_id: Decimal("0") for order in entries.values()}
    for fill in executions:
        binding = known.get(fill.order_id)
        if binding is None:
            raise PortfolioRiskUnavailable("Execution has no bound zone order")
        if binding.role == "ENTRY":
            entered[binding.order_id] += fill.quantity
            if entered[binding.order_id] > binding.quantity:
                raise PortfolioRiskUnavailable("Zone entry order is overfilled")
            continue
        parent = entries.get(binding.parent_order_link_id)
        if parent is None or parent.zone_id != binding.zone_id:
            raise PortfolioRiskUnavailable(
                "Closing execution lacks a same-zone parent entry"
            )
        closed[parent.order_id] += fill.quantity
        if closed[parent.order_id] > entered[parent.order_id]:
            raise PortfolioRiskUnavailable("Zone parent entry is overclosed")
    return entered, closed


def _valid_child_stop(
    live: AccountOrder,
    expected_side: Side,
    stop_loss: Decimal,
) -> bool:
    return (
        live.side is expected_side
        and live.kind == "SL"
        and live.status == "Untriggered"
        and live.order_type == "Market"
        and (live.reduce_only or live.close_on_trigger)
        and live.trigger_price == stop_loss
        and live.executed_quantity == 0
    )


def _verify_parent_stop_coverage(
    parent: ZoneOrder,
    remaining: Decimal,
    stop_loss: Decimal,
    side: Side,
    orders: Sequence[ZoneOrder],
    observed_by_id: dict[str, AccountOrder],
) -> None:
    if remaining == 0:
        return
    expected_side = Side.SHORT if side is Side.LONG else Side.LONG
    children = [
        child
        for child in orders
        if child.zone_id == parent.zone_id
        and child.parent_order_link_id == parent.order_link_id
        and child.role == "STOP"
    ]
    covered = Decimal("0")
    for child in children:
        live = observed_by_id.get(child.order_id)
        if live is None:
            continue
        if not _valid_child_stop(live, expected_side, stop_loss):
            raise PortfolioRiskUnavailable("Invalid native child stop order")
        covered += live.remaining_quantity
    if covered != remaining:
        raise PortfolioRiskUnavailable(
            "Zone parent fill is not covered by verified child stop"
        )


def _zone_filled_stop_risk(
    plan: ExecutionPlan,
    stop_loss: Decimal,
    orders: Sequence[ZoneOrder],
    executions: Sequence[ZoneExecution],
    entered: dict[str, Decimal],
    closed: dict[str, Decimal],
    observed_by_id: dict[str, AccountOrder],
) -> tuple[Decimal, Decimal]:
    parents = [
        order
        for order in orders
        if order.zone_id == plan.intent_id and order.role == "ENTRY"
    ]
    zone_rem = Decimal("0")
    for parent in parents:
        rem = entered[parent.order_id] - closed[parent.order_id]
        _verify_parent_stop_coverage(
            parent, rem, stop_loss, plan.side, orders, observed_by_id
        )
        zone_rem += rem
    if zone_rem == 0:
        return Decimal("0"), Decimal("0")
    parent_ids = {parent.order_id for parent in parents}
    entry_fills = [fill for fill in executions if fill.order_id in parent_ids]
    total_entered = sum((fill.quantity for fill in entry_fills), Decimal("0"))
    avg_price, _ = _zone_entry_economics(
        plan.side, plan.stop_loss, entry_fills, total_entered
    )
    loss_per_unit = max(
        Decimal("0"),
        avg_price - stop_loss if plan.side is Side.LONG else stop_loss - avg_price,
    )
    return zone_rem, zone_rem * loss_per_unit


def _pending_zone_entry_risk(
    plan: ExecutionPlan,
    order: AccountOrder,
    binding: ZoneOrder,
    entered_qty: Decimal,
) -> Decimal:
    if (
        order.side is not plan.side
        or order.order_link_id != binding.order_link_id
        or order.remaining_quantity <= 0
        or order.remaining_quantity + entered_qty > binding.quantity
    ):
        raise PortfolioRiskUnavailable("Open entry order conflicts with zone binding")
    entry_name = order.order_link_id.rsplit("-", 1)[-1].upper()
    planned = next(
        (item for item in plan.entry_orders if item.name.upper() == entry_name),
        None,
    )
    entry_price = order.price or (
        planned.reference_price if planned is not None else None
    )
    if entry_price is None:
        raise PortfolioRiskUnavailable("Zone entry order has no measurable price")
    return order.remaining_quantity * abs(entry_price - plan.stop_loss)


def _verify_group_position(
    account_state: AccountStateSummary,
    symbol: str,
    side: Side,
    total_remaining: Decimal,
) -> None:
    positions = [p for p in account_state.positions if p.symbol == symbol]
    if len(positions) > 1 or any(p.side is not side for p in positions):
        raise PortfolioRiskUnavailable("Zone group position side or count mismatch")
    if any(p.stop_loss or p.trailing_stop or p.take_profit for p in positions):
        raise PortfolioRiskUnavailable(
            "Unscoped whole-position protection on zone group"
        )
    live_qty = sum((p.size for p in positions), Decimal("0"))
    if live_qty != total_remaining:
        raise PortfolioRiskUnavailable("Zone group position quantity mismatch")


def zone_group_open_stop_risk_usdt(
    plans: Sequence[ExecutionPlan],
    orders: Sequence[ZoneOrder],
    executions: Sequence[ZoneExecution],
    account_state: AccountStateSummary,
    *,
    protected_stops: Mapping[UUID, Decimal] | None = None,
) -> Decimal:
    """Compute open stop risk across a zone group without cross-zone offset."""
    if not plans:
        return Decimal("0")
    symbol, side = plans[0].symbol, plans[0].side
    if any(p.symbol != symbol or p.side is not side for p in plans):
        raise PortfolioRiskUnavailable("Zone group plans must share symbol and side")
    entered, closed = _reconcile_parent_fills(orders, executions)
    observed = [o for o in account_state.open_orders if o.symbol == symbol]
    known = {o.order_id: o for o in orders}
    if any(o.order_id not in known for o in observed):
        raise PortfolioRiskUnavailable("Unattributed open order on zone symbol")
    observed_by_id = {o.order_id: o for o in observed}
    stops = protected_stops or {}
    total_remaining = Decimal("0")
    total_risk = Decimal("0")
    for plan in plans:
        stop_loss = stops.get(plan.intent_id, plan.stop_loss)
        rem, risk = _zone_filled_stop_risk(
            plan, stop_loss, orders, executions, entered, closed, observed_by_id
        )
        total_remaining += rem
        total_risk += risk
    _verify_group_position(account_state, symbol, side, total_remaining)
    plans_by_id = {p.intent_id: p for p in plans}
    for order in observed:
        binding = known[order.order_id]
        if binding.role != "ENTRY":
            continue
        plan = plans_by_id.get(binding.zone_id)
        if plan is None:
            raise PortfolioRiskUnavailable(
                "Open entry order belongs to unknown zone plan"
            )
        total_risk += _pending_zone_entry_risk(
            plan, order, binding, entered[order.order_id]
        )
    return total_risk


def evaluate_zone_group_runner_contract(
    plans: Sequence[ExecutionPlan],
    orders: Sequence[ZoneOrder],
    executions: Sequence[ZoneExecution],
    *,
    mark_price: Decimal,
    context: InstrumentContext,
    previous_protected_stops: Mapping[UUID, Decimal] | None = None,
    already_trailing_zones: frozenset[UUID] = frozenset(),
) -> ZoneGroupRunnerContract:
    """Evaluate per-zone runner states and native broker trailing compatibility."""
    if not plans:
        raise ValueError("Zone group runner evaluation requires at least one plan")
    symbol, side = plans[0].symbol, plans[0].side
    if any(p.symbol != symbol or p.side is not side for p in plans):
        raise ValueError("Zone group plans must share symbol and side")
    prev_stops = previous_protected_stops or {}
    states = tuple(
        derive_zone_runner_state(
            plan,
            orders,
            executions,
            mark_price=mark_price,
            context=context,
            previous_protected_stop=prev_stops.get(plan.intent_id),
            already_trailing=plan.intent_id in already_trailing_zones,
        )
        for plan in plans
    )
    active_count = sum(1 for state in states if state.remaining_quantity > 0)
    trailing_count = sum(
        1 for state in states if state.remaining_quantity > 0 and state.trailing_active
    )
    if active_count <= 1:
        status = "SINGLE_ACTIVE_ZONE_FULL_TRAILING_COMPATIBLE"
        tradeoff = False
    elif trailing_count == 0:
        status = "MULTI_ZONE_PRE_TRAIL_PARTIAL_STOP_ONLY"
        tradeoff = False
    else:
        status = "UNSUPPORTED_SAME_ACCOUNT_PARTIAL_TRAILING"
        tradeoff = True
    return ZoneGroupRunnerContract(
        states=states,
        active_zone_count=active_count,
        trailing_zone_count=trailing_count,
        native_trailing_status=status,
        operator_tradeoff_required=tradeoff,
        protection_verified=False,
        trading_enabled=False,
    )
