"""Discover source-owned lifecycle targets across verified Demo stores."""

from collections.abc import Mapping
from uuid import UUID

from .domain import PositionActionIntent, StrategyStatus
from .ports import ExecutionCoordinatorStore


async def find_lifecycle_targets(
    action: PositionActionIntent,
    accounts: Mapping[int, ExecutionCoordinatorStore],
) -> tuple[tuple[int, UUID], ...]:
    """Return eligible owners. A failed store read invalidates the result."""
    targets = []
    for uid, store in accounts.items():
        active = await store.get_active_position_strategies()
        matching = [state for state, _ in active if state.symbol == action.symbol]
        if not matching:
            continue
        if len(matching) != 1:
            raise ValueError("Demo lifecycle ownership is ambiguous")
        state = matching[0]
        if action.expected_side is not None and state.side is not action.expected_side:
            continue
        owner = await store.get_intent(state.strategy_id)
        if (
            owner is None
            or owner.symbol != state.symbol
            or owner.side is not state.side
        ):
            raise ValueError("Demo lifecycle ownership is incomplete")
        if (
            owner.source.channel_id != action.source.channel_id
            or owner.source.published_at > action.source.published_at
        ):
            continue
        if state.status in {
            StrategyStatus.MANUAL_OVERRIDE,
            StrategyStatus.UNCERTAIN,
            StrategyStatus.CLOSING,
        }:
            raise ValueError("Demo lifecycle ownership requires review")
        targets.append((uid, state.strategy_id))
    return tuple(targets)
