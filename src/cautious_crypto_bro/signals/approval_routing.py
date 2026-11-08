from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..contract_symbols import owned_action_symbol
from ..domain import (
    AccountStateSummary,
    ApprovalMode,
    AutoApprovalMode,
    IntentStatus,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    SignalExtraction,
    SignalPositionContext,
    TradingIntent,
)

if TYPE_CHECKING:
    from .planning import OpenApprovalSafety, PlanningExchange

logger = logging.getLogger(__name__)


def _manual(message: str, *args: object) -> ApprovalMode:
    logger.warning(message, *args)
    return ApprovalMode.MANUAL


def open_approval_mode(
    intent: TradingIntent,
    *,
    auto_approval_mode: AutoApprovalMode,
    coordinator: OpenApprovalSafety,
    position_context: SignalPositionContext,
    account_state: AccountStateSummary | None,
    duplicate_in_batch: bool,
) -> ApprovalMode:
    if intent.source.recovery_review_reason:
        return ApprovalMode.MANUAL
    if auto_approval_mode is AutoApprovalMode.DISABLED:
        return ApprovalMode.MANUAL

    if intent.relation not in {
        OpenRelation.NEW,
        OpenRelation.ADD_OR_REENTRY,
    }:
        return _manual(
            "OPEN %s from %s/%s remains MANUAL because relation is %s",
            intent.symbol,
            intent.source.channel_id,
            intent.source.message_id,
            intent.relation.value,
        )

    if duplicate_in_batch:
        return _manual(
            "OPEN %s from %s/%s remains "
            "MANUAL because the same symbol/"
            "side appears multiple times in "
            "the current post",
            intent.symbol,
            intent.source.channel_id,
            intent.source.message_id,
        )

    if account_state is None or not position_context.account_state_available:
        return _manual(
            "OPEN %s from %s/%s remains "
            "MANUAL because trusted live "
            "account state is unavailable",
            intent.symbol,
            intent.source.channel_id,
            intent.source.message_id,
        )

    if intent.relation is OpenRelation.NEW and position_context.has_existing_copy(
        intent.symbol,
        intent.side,
    ):
        return _manual(
            "OPEN %s from %s/%s remains "
            "MANUAL: model classified NEW "
            "but trusted source history "
            "already has a copied/in-flight "
            "position",
            intent.symbol,
            intent.source.channel_id,
            intent.source.message_id,
        )

    safety_reason = coordinator.auto_open_safety_reason(
        intent,
        account_state,
    )

    if safety_reason is not None:
        return _manual(
            "OPEN %s from %s/%s remains MANUAL: %s",
            intent.symbol,
            intent.source.channel_id,
            intent.source.message_id,
            safety_reason,
        )

    return ApprovalMode.AUTO


def position_action_approval_mode(
    action: PositionActionIntent,
    *,
    auto_approval_mode: AutoApprovalMode,
    account_state: AccountStateSummary | None,
) -> ApprovalMode:
    if action.source.recovery_review_reason:
        return ApprovalMode.MANUAL
    if account_state is None:
        return _manual(
            "Position action %s %s remains "
            "MANUAL because live account "
            "state is unavailable",
            action.action.value,
            action.symbol,
        )

    if action.action is PositionActionType.CANCEL_ENTRIES:
        executable = True
    else:
        positions = tuple(
            position
            for position in account_state.positions
            if position.symbol == action.symbol
        )
        executable = len(positions) == 1 and (
            action.expected_side is None or action.expected_side is positions[0].side
        )

    if not executable:
        logger.info(
            "Position action %s %s skipped; no matching live position",
            action.action.value,
            action.symbol,
        )
        return ApprovalMode.SKIPPED

    if auto_approval_mode is not AutoApprovalMode.ALL:
        return ApprovalMode.MANUAL

    return ApprovalMode.AUTO


async def plan_position_actions(
    signals: SignalExtraction,
    *,
    executor: PlanningExchange,
    auto_approval_mode: AutoApprovalMode,
    intent_max_age_seconds: int,
    account_state: AccountStateSummary | None,
    position_context: SignalPositionContext | None = None,
) -> tuple[PositionActionIntent, ...]:
    history = position_context.source_open_history if position_context else ()
    actions = tuple(
        action.model_copy(
            update={
                "symbol": owned_action_symbol(
                    action.symbol,
                    history
                    if position_context is not None
                    and position_context.source_channel_id == action.source.channel_id
                    else (),
                )
            }
        )
        for action in signals.position_actions
    )
    planned = tuple(
        action.model_copy(
            update={
                "approval_mode": position_action_approval_mode(
                    action,
                    auto_approval_mode=auto_approval_mode,
                    account_state=account_state,
                )
            }
        )
        for action in actions
    )
    reviewed = []
    for action in planned:
        held_symbol = account_state is not None and any(
            p.symbol == action.symbol for p in account_state.positions
        )
        expired = action.age_seconds(datetime.now(UTC)) > intent_max_age_seconds
        if action.action is PositionActionType.CANCEL_ENTRIES or expired or held_symbol:
            reviewed.append(action)
            continue
        try:
            await executor.market_context(action.symbol)
        except Exception as exc:
            logger.warning(
                "Unmatched lifecycle contract %s needs review (%s)",
                action.symbol,
                type(exc).__name__,
            )
            action = action.model_copy(
                update={
                    "approval_mode": ApprovalMode.MANUAL,
                    "status": IntentStatus.SKIPPED,
                    "contract_review_reason": (
                        "Exchange contract verification unavailable "
                        f"({type(exc).__name__}). Verify the original source ticker "
                        "and account directly; no replacement symbol was chosen."
                    ),
                }
            )
        reviewed.append(action)
    return tuple(reviewed)
