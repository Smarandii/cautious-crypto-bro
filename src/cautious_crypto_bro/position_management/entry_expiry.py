from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    ExecutionOrderType,
    ExecutionPlan,
    PositionStrategy,
    StrategyStatus,
)

if TYPE_CHECKING:
    from ..ports import AccountGateway

logger = logging.getLogger(__name__)

EXPIRY_DISABLED_STATES = {
    StrategyStatus.CLOSING,
    StrategyStatus.MANUAL_OVERRIDE,
    StrategyStatus.UNCERTAIN,
}


def pending_entry_link_ids(
    state: PositionStrategy,
    account: AccountStateSummary,
) -> tuple[str, ...]:
    prefix = f"ccb-v2-{state.strategy_id.hex[:20]}-e"
    return tuple(
        order.order_link_id
        for order in account.open_orders
        if (
            order.symbol == state.symbol
            and order.kind == "ENTRY"
            and order.order_link_id.startswith(prefix)
        )
    )


def planned_entry_link_ids(
    state: PositionStrategy,
    plan: ExecutionPlan,
) -> set[str]:
    prefix = f"ccb-v2-{state.strategy_id.hex[:20]}-"
    return {f"{prefix}{planned.name.lower()}" for planned in plan.entry_orders}


async def expire_stale_entries(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    position: AccountPosition | None,
    *,
    executor: AccountGateway,
    legacy_entry_ttl_minutes: int,
) -> tuple[AccountStateSummary, bool]:
    ttl_minutes = _expired_ttl(state, plan, legacy_entry_ttl_minutes)
    if not ttl_minutes:
        return account, False

    expected_link_ids = planned_entry_link_ids(state, plan)
    stale_entries = tuple(
        order
        for order in account.open_orders
        if (
            order.symbol == state.symbol
            and not order.reduce_only
            and order.order_link_id in expected_link_ids
        )
    )
    if stale_entries:
        logger.info(
            "Expiring %d Strategy V2 entry leg(s) for %s after %d minutes",
            len(stale_entries),
            state.symbol,
            ttl_minutes,
        )
        for order in stale_entries:
            await executor.cancel_order(state.symbol, order.order_id)
        account = await executor.account_state()
    elif position is None:
        logger.info(
            "Closing expired unfilled Strategy V2 plan for %s after %d minutes",
            state.symbol,
            ttl_minutes,
        )

    return account, True


def _expired_ttl(
    state: PositionStrategy, plan: ExecutionPlan, legacy_entry_ttl_minutes: int
) -> int:
    if state.status in EXPIRY_DISABLED_STATES:
        return 0
    # Older plans load without a TTL and adopt the configured fallback.
    ttl = plan.policy.strategy_v2.entry_order_ttl_minutes or legacy_entry_ttl_minutes
    if not ttl or datetime.now(UTC) < plan.created_at + timedelta(minutes=ttl):
        return 0
    return ttl


async def expire_mirror_entry(
    state: PositionStrategy,
    plan: ExecutionPlan,
    account: AccountStateSummary,
    verified_entry: AccountOrder,
    *,
    executor: AccountGateway,
    legacy_entry_ttl_minutes: int,
) -> AccountStateSummary:
    ttl = _expired_ttl(state, plan, legacy_entry_ttl_minutes)
    if (
        not ttl
        or plan.orders[0].order_type is not ExecutionOrderType.LIMIT
        or verified_entry.status not in {"New", "PartiallyFilled"}
    ):
        return account
    if not any(
        order.order_id == verified_entry.order_id
        and order.order_link_id == verified_entry.order_link_id
        and order.symbol == plan.symbol
        and order.side is plan.side
        and order.kind == "ENTRY"
        and order.remaining_quantity > 0
        and order.executed_quantity == verified_entry.executed_quantity
        for order in account.open_orders
    ):
        # Wait for the order and position snapshots to agree again.
        return account
    logger.info(
        "Expiring mirrored entry remainder for %s after %d minutes", plan.symbol, ttl
    )
    await executor.cancel_order(plan.symbol, verified_entry.order_id)
    return await executor.account_state()
