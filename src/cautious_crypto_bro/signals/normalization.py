"""Normalize model transport fields into actionable domain signals."""

from __future__ import annotations

import logging

from pydantic import ValidationError

from ..domain import (
    Entry,
    EntryType,
    ExtractedIntent,
    ExtractedPositionAction,
    IntentExtraction,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    Side,
    SignalExtraction,
    SourceMessage,
    TradingIntent,
)
from .evidence import (
    _SHORTHAND_REDUCE_PATTERN,
    _caption_is_market_update,
    _current_post_action_evidence,
    _normalize_evidence_text,
    _reduction_pct_from_evidence,
)

logger = logging.getLogger(__name__)


def _side_from_transport(
    value: str | None,
) -> Side | None:
    if value is None:
        return None
    try:
        return Side(value.strip().upper())
    except ValueError:
        return None


def _entry_from_transport(
    raw: ExtractedIntent,
) -> Entry | None:
    entry = raw.entry
    if entry is None:
        return None

    entry_name = entry if isinstance(entry, str) else entry.type
    if not entry_name:
        return None

    try:
        entry_type = EntryType(entry_name.strip().upper())
    except ValueError:
        return None

    if entry_type is EntryType.MARKET:
        # Historical/average price attached to a MARKET signal
        # is intentionally ignored.
        return Entry(type=EntryType.MARKET)

    price = raw.price if raw.price is not None else getattr(entry, "price", None)

    if entry_type is EntryType.LIMIT:
        if price is None:
            return None
        return Entry(type=EntryType.LIMIT, price=price)

    range_low = (
        raw.range_low
        if raw.range_low is not None
        else getattr(entry, "range_low", None)
    )
    range_high = (
        raw.range_high
        if raw.range_high is not None
        else getattr(entry, "range_high", None)
    )

    if range_low is None or range_high is None:
        return None

    try:
        return Entry(
            type=EntryType.RANGE,
            range_low=range_low,
            range_high=range_high,
        )
    except ValidationError:
        return None


def _signals_from_extraction(
    source: SourceMessage,
    extraction: IntentExtraction,
) -> SignalExtraction:
    return SignalExtraction(
        open_intents=_open_intents_from_extraction(source, extraction),
        position_actions=_position_actions_from_extraction(source, extraction),
    )


def _open_intents_from_extraction(
    source: SourceMessage,
    extraction: IntentExtraction,
) -> tuple[TradingIntent, ...]:
    opens: list[TradingIntent] = []

    for raw in extraction.intents:
        intent = _open_intent_from_candidate(source, raw)

        if intent is not None:
            opens.append(intent)

    return tuple(opens)


def _open_intent_from_candidate(
    source: SourceMessage,
    raw: ExtractedIntent,
) -> TradingIntent | None:
    relation = raw.relation or OpenRelation.UNCLASSIFIED

    if relation is OpenRelation.UPDATE_EXISTING:
        logger.info(
            "Ignoring UPDATE_EXISTING candidate from %s/%s for %s",
            source.channel_id,
            source.message_id,
            raw.symbol,
        )
        return None

    side = _side_from_transport(raw.side) or _side_from_transport(raw.direction)

    entry = _entry_from_transport(raw)

    if (
        entry is not None
        and entry.type is EntryType.MARKET
        and _caption_is_market_update(source.text)
    ):
        logger.warning(
            "Dropping recap-derived MARKET OPEN for %s from %s/%s",
            raw.symbol,
            source.channel_id,
            source.message_id,
        )
        return None

    if side is None or entry is None:
        logger.warning(
            "Dropping incomplete OPEN candidate from %s/%s for %s",
            source.channel_id,
            source.message_id,
            raw.symbol,
        )
        return None

    try:
        return TradingIntent(
            source=source,
            symbol=raw.symbol,
            side=side,
            entry=entry,
            stop_loss=raw.stop_loss,
            take_profit=raw.take_profit,
            leverage=raw.leverage,
            summary=raw.summary,
            confidence=raw.confidence,
            relation=relation,
            relation_evidence=(raw.relation_evidence),
        )

    except ValidationError as exc:
        logger.warning(
            "Dropping invalid OPEN candidate from %s/%s for %s: %s",
            source.channel_id,
            source.message_id,
            raw.symbol,
            exc,
        )
        return None


def _position_actions_from_extraction(
    source: SourceMessage,
    extraction: IntentExtraction,
) -> tuple[PositionActionIntent, ...]:
    actions: list[PositionActionIntent] = []

    for raw in extraction.position_actions:
        action_name = raw.action.strip().upper()

        # HOLD is useful model interpretation but is
        # deliberately non-executable.
        if action_name == "HOLD":
            continue

        try:
            action_type = PositionActionType(action_name)
        except ValueError:
            logger.warning(
                "Dropping unsupported position action %r from %s/%s",
                raw.action,
                source.channel_id,
                source.message_id,
            )
            continue

        action_evidence = _current_post_action_evidence(
            source,
            action_type,
            raw.symbol,
            raw.evidence_text,
        )

        if action_evidence is None:
            logger.warning(
                "Dropping %s position action "
                "for %s from %s/%s: no "
                "deterministic %s instruction "
                "in current post text/caption",
                action_type.value,
                raw.symbol,
                source.channel_id,
                source.message_id,
                action_type.value,
            )
            continue

        expected_side = _side_from_transport(raw.expected_side) or _side_from_transport(
            raw.side
        )

        close_pct = None
        if action_type is PositionActionType.REDUCE:
            close_pct = _reduce_close_pct(
                source,
                raw,
                action_evidence,
            )

            if close_pct is None:
                continue

        try:
            action = PositionActionIntent(
                source=source,
                symbol=raw.symbol,
                action=action_type,
                close_pct=close_pct,
                expected_side=expected_side,
                summary=raw.summary,
                confidence=raw.confidence,
            )

        except ValidationError as exc:
            logger.warning(
                "Dropping invalid position action from %s/%s for %s: %s",
                source.channel_id,
                source.message_id,
                raw.symbol,
                exc,
            )
            continue

        actions.append(action)

    return tuple(actions)


def _reduce_close_pct(
    source: SourceMessage,
    raw: ExtractedPositionAction,
    action_evidence: str,
) -> float | None:
    shorthand = _SHORTHAND_REDUCE_PATTERN.search(
        _normalize_evidence_text(action_evidence)
    )
    amount_evidence = shorthand.group(0) if shorthand else action_evidence
    close_pct = _reduction_pct_from_evidence(amount_evidence)

    if close_pct is None:
        logger.warning(
            "Dropping REDUCE position "
            "action for %s from %s/%s: "
            "no deterministic reduction "
            "amount in current caption "
            "evidence",
            raw.symbol,
            source.channel_id,
            source.message_id,
        )
        return None

    if raw.close_pct is not None and abs(raw.close_pct - close_pct) > 1e-9:
        logger.warning(
            "Normalizing REDUCE close_pct for %s from model=%s to evidence=%s",
            raw.symbol,
            raw.close_pct,
            close_pct,
        )

    return close_pct
