from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain import (
    AccountPosition,
    AccountStateSummary,
    ExecutionPlan,
    PositionStrategy,
    StrategyStatus,
)

if TYPE_CHECKING:
    from ..ports import AccountGateway

logger = logging.getLogger(__name__)


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
    if state.status in {
        StrategyStatus.CLOSING,
        StrategyStatus.MANUAL_OVERRIDE,
        StrategyStatus.UNCERTAIN,
    }:
        return account, False
    ttl_minutes = plan.policy.strategy_v2.entry_order_ttl_minutes
    if ttl_minutes == 0:
        # Plans persisted before the entry-order TTL existed have no stored
        # value and load as zero. Adopt the configured TTL so a legacy
        # ladder cannot own a symbol forever. A configured value of zero
        # still disables expiry.
        ttl_minutes = legacy_entry_ttl_minutes
    expires_at = plan.created_at + timedelta(minutes=ttl_minutes)
    if ttl_minutes == 0 or datetime.now(UTC) < expires_at:
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
