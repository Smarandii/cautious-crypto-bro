"""Pure position, owned-entry, partial-stop, and trailing-stop drift inspection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain import Side
from .fill_detector import FillDetector

if TYPE_CHECKING:
    from ..domain import (
        AccountOrder,
        AccountPosition,
        AccountStateSummary,
        ExecutionPlan,
        PlannedOrder,
        PositionStrategy,
    )
    from .exit_installer import ExitInstaller


def pre_install_position_drift_reason(
    state: PositionStrategy,
    plan: ExecutionPlan,
    position: AccountPosition,
) -> str | None:
    if position.side is not state.side:
        return "position side mismatch"
    if position.take_profit is not None:
        return (
            "position take profit enabled outside Strategy V2; manual review required"
        )
    if not state.trailing_active and position.trailing_stop is not None:
        return (
            "position trailing stop enabled outside Strategy V2; manual review required"
        )
    if abs(position.avg_price - plan.stop_loss) <= 0:
        return "invalid stop loss"
    return None


def manual_override_reason(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    position: AccountPosition,
    *,
    exit_installer: ExitInstaller,
) -> str | None:
    for check in (
        unattributed_partial_stop,
        owned_entry_drift,
        size_increased_after_freeze,
        average_changed_after_freeze,
    ):
        reason = check(state, plan, account, position)
        if reason is not None:
            return reason
    reason = protection_drift(
        state, plan, account, position, exit_installer=exit_installer
    )
    if reason is not None:
        return reason
    return trailing_drift(state, plan, account, position)


def unattributed_partial_stop(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    position: AccountPosition,
) -> str | None:
    # Bybit carries the originating entry's orderLinkId on attached
    # TP/SL orders. Stop price and side alone cannot prove ownership.
    expected_parents = {
        FillDetector.entry_link_id(state, order.name) for order in plan.entry_orders
    }
    expected_side = Side.SHORT if state.side is Side.LONG else Side.LONG
    for order in account.open_orders:
        if (
            order.symbol == state.symbol
            and order.stop_order_type == "PartialStopLoss"
            and order.parent_order_link_id in expected_parents
            and (
                order.trigger_price != plan.stop_loss or order.side is not expected_side
            )
        ):
            return (
                f"owned partial stop changed outside Strategy V2: {order.order_id}; "
                "manual review required"
            )
    if any(
        order.symbol == state.symbol
        and order.stop_order_type == "PartialStopLoss"
        and order.trigger_price == plan.stop_loss
        and order.parent_order_link_id not in expected_parents
        for order in account.open_orders
    ):
        return "unattributed partial stop-loss at the strategy stop; manual review required"

    return None


def owned_entry_drift(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    position: AccountPosition,
) -> str | None:
    if state.entry_frozen:
        return None

    expected = {
        FillDetector.entry_link_id(
            state,
            planned.name,
        ): planned
        for planned in plan.entry_orders
    }

    prefix = f"ccb-v2-{state.strategy_id.hex[:20]}-e"

    for order in account.open_orders:
        if (
            order.symbol != state.symbol
            or order.kind != "ENTRY"
            or not order.order_link_id.startswith(prefix)
        ):
            continue

        reason = entry_order_drift(state, order, expected)

        if reason is not None:
            return reason

    return None


def entry_order_drift(
    state: PositionStrategy,
    order: AccountOrder,
    expected: dict[str, PlannedOrder],
) -> str | None:
    planned = expected.get(order.order_link_id)

    if planned is None:
        return f"unexpected owned V2 entry {order.order_link_id}"

    if order.side is not state.side:
        return f"owned entry side changed for {order.order_link_id}"

    if order.quantity != planned.quantity:
        return f"owned entry quantity changed for {order.order_link_id}"

    if planned.price is not None and order.price != planned.price:
        return (
            "owned entry price changed "
            f"for {order.order_link_id}: "
            f"expected={planned.price}, "
            f"live={order.price}"
        )

    return None


def size_increased_after_freeze(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    position: AccountPosition,
) -> str | None:
    if (
        state.entry_frozen
        and not state.rebalance_needed
        and state.last_position_qty is not None
        and position.size > state.last_position_qty
    ):
        return "position size increased after entry freeze"

    return None


def average_changed_after_freeze(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    position: AccountPosition,
) -> str | None:
    if (
        state.entry_frozen
        and not state.rebalance_needed
        and state.last_avg_price is not None
        and position.avg_price != state.last_avg_price
    ):
        return "average entry changed after entry freeze"

    return None


def protection_drift(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    position: AccountPosition,
    *,
    exit_installer: ExitInstaller,
) -> str | None:
    if (
        state.protected_stop_loss is not None
        and position.stop_loss != state.protected_stop_loss
        and not (
            position.stop_loss is None
            and exit_installer.partial_stops(state, plan, account)
        )
    ):
        return (
            "position stop changed outside "
            "Strategy V2: "
            f"expected="
            f"{state.protected_stop_loss}, "
            f"live={position.stop_loss}"
        )

    return None


def trailing_drift(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    position: AccountPosition,
) -> str | None:
    if state.trailing_active:
        if state.trailing_distance is None:
            return "persisted trailing state is inconsistent"

        if position.trailing_stop != state.trailing_distance:
            return (
                "position trailing stop changed "
                "outside Strategy V2: "
                f"expected="
                f"{state.trailing_distance}, "
                f"live={position.trailing_stop}"
            )

    return None
