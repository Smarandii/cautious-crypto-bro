"""Discover source-owned lifecycle targets across verified Demo stores."""

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from .domain import (
    ApprovalMode,
    ExecutionPlan,
    IntentStatus,
    PositionActionIntent,
    PositionStrategy,
    StrategyStatus,
    TradingIntent,
)


class LifecycleOwnerStore(Protocol):
    async def get_active_position_strategies(
        self,
    ) -> tuple[tuple[PositionStrategy, ExecutionPlan], ...]: ...
    async def get_intent(self, intent_id: UUID) -> TradingIntent | None: ...


def bind_lifecycle_target(
    action: PositionActionIntent, targets: tuple[tuple[int, UUID], ...] | None
) -> PositionActionIntent:
    if targets == ():
        return action
    if targets is not None and len(targets) == 1 and action.expected_side is not None:
        uid, strategy_id = targets[0]
        return action.model_copy(
            update={
                "target_demo_uid": uid,
                "target_strategy_id": strategy_id,
                "approval_mode": ApprovalMode.MANUAL,
                "status": IntentStatus.PENDING,
                "contract_review_reason": None,
            }
        )
    return action.model_copy(
        update={
            "approval_mode": ApprovalMode.MANUAL,
            "status": IntentStatus.SKIPPED,
            "contract_review_reason": "Demo worker ownership is unavailable or ambiguous. Review the source and account before acting.",
        }
    )


async def find_lifecycle_targets(
    action: PositionActionIntent,
    accounts: Mapping[int, LifecycleOwnerStore],
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
