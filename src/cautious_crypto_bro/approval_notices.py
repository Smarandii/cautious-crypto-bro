from __future__ import annotations

import html

from .approval_presenter import (
    render,
    render_action_outcome,
    render_position_action,
    source_line,
)
from .domain import (
    EntryReview,
    ExecutionPlan,
    IntentExecutionOutcome,
    IntentStatus,
    PositionActionExecutionOutcome,
    PositionActionIntent,
    PositionStrategy,
    TradingIntent,
)

RECOVERED_OUTCOME_NOTICE = (
    "<b>RECOVERED EXECUTION RECORD</b>\n"
    "Stored outcome only; this is not a new trade or live confirmation.\n\n"
)


def render_strategy_pause_alert(state: PositionStrategy) -> str:
    assert state.pause_reason is not None
    return (
        "⚠️ <b>STRATEGY AUTOMATION PAUSED</b>\n"
        f"<b>{html.escape(state.symbol)}</b>\n"
        f"<code>{state.strategy_id}</code>\n"
        f"{html.escape(state.pause_reason)}\n\n"
        "Orders and protection were left untouched. "
        "Review the position and its exits on Bybit before resuming automation."
    )


def render_entry_review(review: EntryReview) -> str:
    ownership = (
        "Copied-position ownership could not be verified."
        if review.ownership_unknown
        else "No matching active copied/in-flight source position was found."
    )
    return (
        "<b>FILLED ENTRY — HUMAN REVIEW REQUIRED</b>\n"
        f"<b>{html.escape(review.symbol)}</b> · direction unavailable\n"
        "The source reports a filled entry; no new trade was authorized from "
        "this incomplete declaration. Review the original source manually.\n"
        f"{ownership}\n"
        f"Published: {review.source.published_at.isoformat()}\n"
        f"Trader: {html.escape(review.source.channel_title)}\n"
        f"{source_line(review.source.telegram_url)}"
    )


def render_unverified_position_action(action: PositionActionIntent) -> str:
    return (
        "<b>LIFECYCLE CONTRACT NEEDS REVIEW — HUMAN REVIEW REQUIRED</b>\n\n"
        f"{html.escape(action.action.value)} {html.escape(action.symbol)}\n"
        f"{html.escape(action.contract_review_reason or '')}\n"
        "This instruction was recorded without execution. "
        "No exchange action was sent.\n\n"
        f"Trader: <b>{html.escape(action.source.channel_title)}</b>\n"
        f"{source_line(action.source.telegram_url)}"
    )


def render_expired_position_action(action: PositionActionIntent) -> str:
    return (
        "<b>EXPIRED LIFECYCLE INSTRUCTION — RECORDED ONLY</b>\n\n"
        f"{html.escape(action.action.value)} {html.escape(action.symbol)}\n"
        f"Published: {html.escape(action.source.published_at.isoformat())}\n"
        "The lifecycle approval deadline passed before delivery. "
        "No exchange action was sent.\n\n"
        f"Trader: <b>{html.escape(action.source.channel_title)}</b>\n"
        f"{source_line(action.source.telegram_url)}"
    )


def render_manual_intent_outcome(
    intent: TradingIntent,
    plan: ExecutionPlan,
    outcome: IntentExecutionOutcome,
) -> str:
    if outcome.status is IntentStatus.EXECUTED:
        ids_text = "\n".join(
            f"<code>{html.escape(order_id)}</code>" for order_id in outcome.order_ids
        )
        suffix = f"\n\n<b>EXECUTED ON BYBIT DEMO</b>\n{ids_text}"
    else:
        suffix = (
            "\n\n"
            f"<b>{html.escape(outcome.status.value)}</b>"
            "\n<code>"
            f"{html.escape(outcome.message)}"
            "</code>"
        )
    return render(intent, plan) + suffix


def render_auto_intent_outcome(outcome: IntentExecutionOutcome) -> str:
    notice = RECOVERED_OUTCOME_NOTICE if outcome.recovered else ""
    if outcome.intent is None or outcome.plan is None:
        return (
            f"{notice}<b>AUTO EXECUTION ERROR</b>\n"
            f"<code>{html.escape(outcome.message)}</code>"
        )
    if outcome.status is IntentStatus.EXECUTED:
        ids_text = "\n".join(
            f"<code>{html.escape(order_id)}</code>" for order_id in outcome.order_ids
        )
        suffix = f"\n\n<b>AUTO-EXECUTED ON BYBIT DEMO</b>\n{ids_text}"
    else:
        suffix = (
            "\n\n"
            f"<b>AUTO EXECUTION {html.escape(outcome.status.value)}</b>\n"
            f"<code>{html.escape(outcome.message)}</code>"
        )
    return notice + render(outcome.intent, outcome.plan) + suffix


def render_auto_action_outcome(outcome: PositionActionExecutionOutcome) -> str:
    notice = RECOVERED_OUTCOME_NOTICE if outcome.recovered else ""
    if outcome.action is None:
        return (
            f"{notice}<b>AUTO EXECUTION ERROR</b>\n"
            f"<code>{html.escape(outcome.message)}</code>"
        )
    return (
        notice
        + render_position_action(outcome.action)
        + render_action_outcome(outcome, auto=True)
    )


def render_recovery_warning(
    *,
    uncertain_intents: int,
    uncertain_actions: int,
) -> str:
    return (
        "⚠️ <b>INTERRUPTED EXECUTION RECOVERY WARNING</b>\n\n"
        "Previous process stopped while execution was in progress. "
        "These records were quarantined instead of retried automatically."
        f"\nOPEN intents: <b>{uncertain_intents}</b>"
        f"\nPosition actions: <b>{uncertain_actions}</b>"
    )
