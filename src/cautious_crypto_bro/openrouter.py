from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import unicodedata

import httpx
from pydantic import ValidationError

from .domain import (
    Entry,
    EntryType,
    ExtractedEntryPayload,
    IncomingPost,
    IntentExtraction,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    Side,
    SignalExtraction,
    SignalPositionContext,
    SourceMessage,
    TradingIntent,
)
from .llm_provider import (
    LLMImage,
    LLMProvider,
    LLMProviderFailure,
    LLMRequest,
    LLMResponse,
    LLMResponseValidationError,
)
from .runtime_store import (
    EvaluationCache,
    ProviderCooldownStore,
)

logger = logging.getLogger(__name__)

STATIC_IGNORED_PROVIDERS = (
    "nextbit",
    "parasail",
)

DEFAULT_REDUCTION_PCT = 50.0


class OpenRouterProviderFailure(ValueError):
    def __init__(
        self,
        provider: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.provider = provider


def _response_provider(
    response_data: dict[str, object],
) -> str | None:
    provider = response_data.get("provider")
    if not isinstance(provider, str):
        return None
    return provider.strip() or None


SYSTEM_PROMPT = """
You are a conservative crypto trading-signal parser.
Do not give trading advice and do not invent missing information.

Inspect ONE Telegram post, including all attached images, and extract:
1. zero or more independent new OPEN trade proposals;
2. zero or more executable actions on EXISTING positions.

Output rules:
- use the exact supplied JSON schema field names
- for OPEN intents, entry should normally be the string
  MARKET, LIMIT, or RANGE
- for LIMIT put the numeric entry in top-level price
- for RANGE put the numeric boundaries in top-level range_low
  and range_high
- do not attach a historical/average price to MARKET
- actionable=true only when at least one NEW/ADD_OR_REENTRY OPEN candidate
  or executable position action exists
- if the post contains only UPDATE_EXISTING observations, actionable=false
- maximum five intents and five position actions
- omit ambiguous candidates without discarding unrelated valid candidates
- every item in intents MUST set relation to exactly one of:
  NEW, ADD_OR_REENTRY, UPDATE_EXISTING
- relation_evidence should briefly explain the current-post evidence plus
  trusted execution-state facts that support the relation

Position relationship rules:
- trusted execution context is application state, NOT trader-authored content
- NEW means this source has no existing copied or in-flight position for the
  same symbol and side
- ADD_OR_REENTRY means the trader explicitly adds, re-enters, or opens again
  despite an existing copied/in-flight position
- UPDATE_EXISTING means the current screenshot/text is only reporting,
  showing progress, or updating SL/TP/status for a position already copied
  from this source
- UPDATE_EXISTING must NOT become a new executable OPEN
- an account-wide Bybit position alone does NOT prove source attribution;
  source_open_history is authoritative for whether this trader was copied
- if account_state_available=false, treat relation classification
  conservatively

OPEN trade rules:
- only USDT linear/perpetual-style symbols
- NEW and ADD_OR_REENTRY candidates require symbol, LONG/SHORT,
  entry semantics, and stop loss
- UPDATE_EXISTING may omit entry or stop loss because it is informational
- take profit is optional
- MARKET when the author clearly says enter now/at market, clearly states
  they entered now, or an exchange screenshot clearly shows the position
  is already open
- for an already-open position screenshot, MARKET takes precedence over
  historical/average entry prices shown in the screenshot
- if one current exchange screenshot shows multiple distinct live positions,
  extract EACH valid position as its own independent OPEN candidate when its
  symbol, side, and stop loss are available
- do not emit a fake REDUCE/CLOSE placeholder merely because another live
  position is visible in the screenshot
- when no explicit lifecycle instruction exists for a visible position,
  position_actions must contain no action for that position
- LIMIT requires one explicit intended entry price
- RANGE requires two explicit numeric boundaries
- for RANGE set range_low to the lower boundary and range_high to the higher
  boundary
- never estimate prices from chart geometry
- never invent a stop, entry, target, or range boundary

Existing-position action rules:
- position actions are account-wide operations on the current position for
  the extracted symbol; they are NOT new opposite-side trades
- REDUCE means partially close an existing position
- an explicit partial-close instruction is executable even when the author
  does not specify an amount
- explicit percentages are authoritative
- exact fractions are authoritative when unambiguous:
  half = 50%, quarter = 25%
- when the current text clearly instructs a partial close but gives no
  percentage/fraction, use close_pct=50
- a take-profit ordinal such as "фиксируем 3 тейк" identifies a take-profit
  milestone; the number 3 is NOT a position fraction or percentage
- percentages describing profit, PnL, price movement, or market movement are
  NOT close_pct; for example "4.5% чистого движения" must not become 4.5%
- if the author gives optional follower advice to reduce but explicitly says
  they personally continue holding, do not emit REDUCE for the trader
- examples such as "take some profit", "fix a part", "trim the position",
  "фиксируем часть" and equivalent wording are REDUCE actions
- CLOSE means fully close the existing position
- for CLOSE set close_pct=null
- HOLD, keep holding, wait, do nothing, and similar instructions are
  informational and must be omitted
- do not convert CLOSE or REDUCE into an opposite-side OPEN trade
- expected_side is optional; populate it only when LONG/SHORT is clearly
  stated or clearly visible in the current post/image
- do not infer expected_side merely from old context
- an action requires a clearly attributable symbol
- REDUCE and CLOSE require an explicit instruction in the CURRENT Telegram
  post text/caption itself
- for every REDUCE/CLOSE, evidence_text must be an exact verbatim excerpt
  from the CURRENT post text/caption that explicitly instructs the action
- evidence_text must never be null for REDUCE/CLOSE
- if no exact current-caption instruction can be quoted, omit the action
- images may identify which symbol/side the text instruction refers to, but
  image text alone must NEVER authorize REDUCE/CLOSE
- NEVER create REDUCE/CLOSE from exchange UI controls such as a Close button
- NEVER create REDUCE/CLOSE from embedded old chat screenshots, testimonials,
  examples of previous trades, performance recaps, or promotional material
- an exchange screenshot merely showing an already-open position is NOT a
  lifecycle action
- never use REDUCE merely to represent that an existing position is visible
- if an already-open exchange position is being presented as the current
  trade and has the required stop, treat it as an OPEN MARKET candidate
  unless the current caption explicitly instructs REDUCE/CLOSE

Context rules:
- use caption/text and images together
- the current post/images are primary evidence
- global guidance applies to all traders
- channel guidance explains that trader's conventions
- guidance may explain deterministic conventions but must never supply
  prices or facts missing from the current post

General rules:
- confidence is extraction confidence, not probability of profit
- summary is one short sentence describing the candidate/action
""".strip()


def _build_user_text(
    post: IncomingPost,
    *,
    global_guidance: str | None = None,
    channel_guidance: str | None = None,
    position_context: SignalPositionContext | None = None,
) -> str:
    source = post.source

    parts = [
        f"Channel: {source.channel_title}",
        f"Published: {source.published_at.isoformat()}",
    ]

    if global_guidance:
        parts.extend(
            [
                "",
                "Global guidance:",
                global_guidance,
            ]
        )

    if channel_guidance:
        parts.extend(
            [
                "",
                "Channel-specific guidance:",
                channel_guidance,
            ]
        )

    if position_context is not None:
        parts.extend(
            [
                "",
                (
                    "Trusted execution context "
                    "(application state; not "
                    "trader-authored content):"
                ),
                position_context.model_dump_json(indent=2),
            ]
        )

    parts.extend(
        [
            "",
            "Telegram post text/caption:",
            source.text or "(no caption)",
        ]
    )

    return "\n".join(parts)


def _evaluation_fingerprint(
    post: IncomingPost,
    *,
    model: str,
    global_guidance: str | None,
    channel_guidance: str | None,
    position_context: (SignalPositionContext | None) = None,
) -> str:
    source = post.source

    fingerprint_payload = {
        "cache_version": 9,
        "model": model,
        "system_prompt": SYSTEM_PROMPT,
        "schema": (IntentExtraction.model_json_schema()),
        "request": {
            "temperature": 0,
            "max_tokens": 1536,
            "reasoning": {
                "effort": "none",
            },
        },
        "source": {
            "channel_id": (source.channel_id),
            "channel_title": (source.channel_title),
            "channel_username": (source.channel_username),
            "message_id": (source.message_id),
            "published_at": (source.published_at.isoformat()),
            "text": source.text,
        },
        "images": [
            {
                "media_type": (image.media_type),
                "sha256": (hashlib.sha256(image.data).hexdigest()),
            }
            for image in post.images
        ],
        "global_guidance": (global_guidance or ""),
        "channel_guidance": (channel_guidance or ""),
        "position_context": (
            position_context.model_dump(mode="json")
            if position_context is not None
            else None
        ),
    }

    canonical = json.dumps(
        fingerprint_payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    raw,
) -> Entry | None:
    nested: ExtractedEntryPayload | None = None
    entry_name: str | None = None

    if isinstance(raw.entry, str):
        entry_name = raw.entry

    elif isinstance(
        raw.entry,
        ExtractedEntryPayload,
    ):
        nested = raw.entry
        entry_name = nested.type

    if not entry_name:
        entry_name = raw.entry_semantics

    if not entry_name:
        entry_name = raw.entry_type

    if not entry_name:
        return None

    try:
        entry_type = EntryType(entry_name.strip().upper())
    except ValueError:
        return None

    price = (
        raw.price
        if raw.price is not None
        else (nested.price if nested is not None else None)
    )

    range_low = (
        raw.range_low
        if raw.range_low is not None
        else (nested.range_low if nested is not None else None)
    )

    range_high = (
        raw.range_high
        if raw.range_high is not None
        else (nested.range_high if nested is not None else None)
    )

    try:
        if entry_type is EntryType.MARKET:
            # Historical/average price attached to a
            # MARKET signal is intentionally ignored.
            return Entry(type=EntryType.MARKET)

        if entry_type is EntryType.LIMIT:
            if price is None:
                return None

            return Entry(
                type=EntryType.LIMIT,
                price=price,
            )

        if range_low is None or range_high is None:
            return None

        return Entry(
            type=EntryType.RANGE,
            range_low=range_low,
            range_high=range_high,
        )

    except ValidationError:
        return None


def _normalize_evidence_text(
    value: str,
) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


_LIFECYCLE_NEGATION_PATTERNS: tuple[
    re.Pattern[str],
    ...,
] = (
    re.compile(
        r"\bне\s+"
        r"(?:(?:надо|нужно|стоит|будем)\s+)?"
        r"(?:сейчас\s+)?"
        r"(?:"
        r"закрыва\w*|закрыть|"
        r"фиксир\w*|"
        r"тейк\w*|"
        r"выхож\w*|выход\w*"
        r")\b"
    ),
    re.compile(
        r"\b(?:do\s+not|don['’]?t)\s+"
        r"(?:close|exit|reduce|trim|take)\b"
    ),
    re.compile(
        r"\bnot\s+"
        r"(?:closing|exiting|reducing|trimming|taking)\b"
    ),
)


_REDUCE_INSTRUCTION_PATTERNS: tuple[
    re.Pattern[str],
    ...,
] = (
    # Russian take-profit milestone, unspecified size.
    # Example: "Фиксируем 3 тейк".
    # The ordinal identifies the TP milestone, not position size.
    re.compile(
        r"\b(?:"
        r"фиксируем|фиксирую|"
        r"зафиксируем|зафиксирую"
        r")\b"
        r".{0,20}"
        r"\b(?:\d+\s*)?тейк\w*\b"
    ),
    # Russian reversed partial-close wording.
    # Examples: "часть закройте", "половину закрой".
    re.compile(
        r"\b(?:"
        r"часть|половин\w*|четверт\w*"
        r")\b"
        r".{0,20}"
        r"\b(?:"
        r"закройте|закрой|закрывай|закрываем|"
        r"фиксируйте|фиксируй|фиксируем"
        r")\b"
    ),
    # Russian explicit partial-close wording:
    # "закрываем часть", "фиксируем половину", etc.
    re.compile(
        r"\b(?:"
        r"закрываем|закрываю|закрывай|закрыть|"
        r"фиксируем|фиксирую|фиксируй|"
        r"зафиксируем|зафиксирую"
        r")\b"
        r".{0,40}"
        r"\b(?:часть|половин\w*|четверт\w*)\b"
    ),
    re.compile(
        r"\bчастичн\w*\b"
        r".{0,25}"
        r"\b(?:закрыва\w*|фиксир\w*)\b"
    ),
    # Explicit numeric reduction:
    # "закрываем 30%", "close 1/3", etc.
    re.compile(
        r"\b(?:"
        r"закрываем|закрываю|закрывай|закрыть|"
        r"фиксируем|фиксирую|фиксируй|"
        r"тейкаем|тейкать|"
        r"close|reduce|trim"
        r")\b"
        r".{0,40}"
        r"(?:"
        r"\d+(?:[.,]\d+)?\s*"
        r"(?:"
        r"%|"
        r"percent(?:s)?\b|"
        r"процент(?:а|ов)?\b"
        r")"
        r"|"
        r"\d+\s*/\s*\d+"
        r")"
    ),
    # English partial-close wording.
    re.compile(
        r"\b(?:"
        r"close|closing|reduce|trim|take|taking"
        r")\b"
        r".{0,40}"
        r"\b(?:part|partial|some|half|quarter)\b"
    ),
    re.compile(
        r"\b(?:take|taking)\b"
        r".{0,20}"
        r"\b(?:some|partial)\b"
        r".{0,20}"
        r"\bprofit\b"
    ),
    # Observed MENSA wording:
    # "оставлю небольшую часть ... остальное ... тейкать"
    re.compile(
        r"\bостав\w*\b"
        r".{0,60}"
        r"\bчаст\w*\b"
        r".{0,100}"
        r"\b(?:тейк\w*|фиксир\w*|закрыва\w*)\b"
    ),
)


_CLOSE_INSTRUCTION_PATTERNS: tuple[
    re.Pattern[str],
    ...,
] = (
    re.compile(
        r"\b(?:"
        r"закрываем|закрываю|закрывай|закрыть"
        r")\b"
    ),
    re.compile(
        r"\b(?:выхожу|выходим|выйти)\b"
        r".{0,25}"
        r"\b(?:из\s+)?позици\w*\b"
    ),
    re.compile(
        r"\bclose\b"
        r"(?:"
        r"\s+(?:the\s+)?"
        r"(?:position|trade|long|short)\b"
        r"|"
        r"\s+(?:it|this|now)\b"
        r"|"
        r"(?=\s*(?:[.!?…]|$))"
        r")"
    ),
    re.compile(
        r"\bexit\b"
        r"(?:"
        r"\s+(?:the\s+)?"
        r"(?:position|trade|long|short)\b"
        r"|"
        r"\s+(?:it|this|now)\b"
        r"|"
        r"(?=\s*(?:[.!?…]|$))"
        r")"
    ),
)


def _first_action_match(
    patterns: tuple[
        re.Pattern[str],
        ...,
    ],
    text: str,
) -> re.Match[str] | None:
    for pattern in patterns:
        match = pattern.search(text)

        if match is not None:
            return match

    return None


def _all_action_matches(
    patterns: tuple[
        re.Pattern[str],
        ...,
    ],
    text: str,
) -> tuple[re.Match[str], ...]:
    return tuple(match for pattern in patterns for match in pattern.finditer(text))


def _matches_overlap(
    first: re.Match[str],
    second: re.Match[str],
) -> bool:
    return first.start() < second.end() and second.start() < first.end()


def _symbol_close_variants(
    symbol: str,
) -> tuple[str, ...]:
    compact = re.sub(
        r"[^a-z0-9]",
        "",
        symbol.casefold(),
    )

    if not compact:
        return ()

    variants = {compact}

    for quote in (
        "usdt",
        "usdc",
        "usd",
    ):
        if compact.endswith(quote) and len(compact) > len(quote):
            variants.add(compact[: -len(quote)])

    return tuple(
        sorted(
            variants,
            key=len,
            reverse=True,
        )
    )


def _symbol_specific_close_match(
    text: str,
    symbol: str,
) -> re.Match[str] | None:
    variants = _symbol_close_variants(symbol)

    if not variants:
        return None

    symbol_pattern = "|".join(re.escape(value) for value in variants)

    return re.search(
        (
            r"\b(?:close|exit)\b"
            r"\s+(?:the\s+)?"
            rf"(?:{symbol_pattern})\b"
            r"(?:\s+"
            r"(?:completely|fully|now)"
            r")?"
        ),
        text,
    )


def _optional_reduce_overridden_by_self_hold(
    text: str,
) -> bool:
    normalized = _normalize_evidence_text(text)

    optional_reduce = re.search(
        r"\bесли\b"
        r".{0,80}"
        r"\b(?:можете|можешь)\b"
        r".{0,40}"
        r"\b(?:закрыть|зафиксировать)\b"
        r".{0,20}"
        r"\b(?:часть|половин\w*)\b",
        normalized,
    )

    self_hold = re.search(
        r"\bя\b"
        r".{0,30}"
        r"\b(?:"
        r"подержу|держу|"
        r"пока\s+подержу|"
        r"пока\s+держу"
        r")\b",
        normalized,
    )

    return optional_reduce is not None and self_hold is not None


def _deterministic_action_evidence(
    text: str,
    action_type: PositionActionType,
    symbol: str,
) -> str | None:
    normalized = _normalize_evidence_text(text)

    if not normalized:
        return None

    if (
        _first_action_match(
            _LIFECYCLE_NEGATION_PATTERNS,
            normalized,
        )
        is not None
    ):
        return None

    reduce_matches = _all_action_matches(
        _REDUCE_INSTRUCTION_PATTERNS,
        normalized,
    )

    if action_type is PositionActionType.REDUCE:
        if not reduce_matches:
            return None

        return reduce_matches[0].group(0)

    # First try an explicit CLOSE naming the same symbol
    # Gemma identified, e.g. "Close POL" for POLUSDT.
    symbol_close = _symbol_specific_close_match(
        normalized,
        symbol,
    )

    if symbol_close is not None:
        if not any(
            _matches_overlap(
                symbol_close,
                reduce_match,
            )
            for reduce_match in reduce_matches
        ):
            return symbol_close.group(0)

    # Generic CLOSE remains valid, but only when that
    # particular close phrase is not part of a REDUCE
    # instruction such as "close half".
    for close_match in _all_action_matches(
        _CLOSE_INSTRUCTION_PATTERNS,
        normalized,
    ):
        if any(
            _matches_overlap(
                close_match,
                reduce_match,
            )
            for reduce_match in reduce_matches
        ):
            continue

        return close_match.group(0)

    return None


def _action_clauses(text: str) -> tuple[str, ...]:
    """Return sentence-like source clauses while preserving commas and amounts."""
    normalized = _normalize_evidence_text(text)
    if not normalized:
        return ()

    return tuple(
        clause.strip()
        for clause in re.split(r"[.!?…;\n]+", normalized)
        if clause.strip()
    )


def _clause_containing_evidence(
    post_text: str,
    evidence_text: str,
) -> str | None:
    evidence = _normalize_evidence_text(evidence_text)
    if not evidence:
        return None

    for clause in _action_clauses(post_text):
        if evidence in clause:
            return clause

    return None


def _current_post_action_evidence(
    source: SourceMessage,
    action_type: PositionActionType,
    symbol: str,
    model_evidence_text: str | None,
) -> str | None:
    post_text = _normalize_evidence_text(source.text)

    if not post_text:
        return None

    if (
        action_type is PositionActionType.REDUCE
        and _optional_reduce_overridden_by_self_hold(post_text)
    ):
        return None

    scopes: list[str] = []

    if model_evidence_text:
        clause = _clause_containing_evidence(
            post_text,
            model_evidence_text,
        )
        if clause is not None:
            scopes.append(clause)

    for clause in _action_clauses(post_text):
        if clause not in scopes:
            scopes.append(clause)

    for scope in scopes:
        evidence = _deterministic_action_evidence(
            scope,
            action_type,
            symbol,
        )

        if evidence is not None:
            # Return the complete source clause, not only the regex match.
            # This keeps authoritative percentages/fractions and surrounding
            # negation in the same deterministic scope.
            return scope

    return None


def _reduction_pct_from_evidence(
    evidence_text: str | None,
) -> float | None:
    if evidence_text is None:
        return None

    text = _normalize_evidence_text(evidence_text)

    # Explicit percentages are authoritative.
    # Examples: 30%, 0.5%, 30 percent,
    # 30 процентов.
    percent_match = re.search(
        r"(?<![\d.,])"
        r"(\d+(?:[.,]\d+)?)"
        r"\s*%",
        text,
    )

    if percent_match is None:
        percent_match = re.search(
            r"(?<![\d.,])"
            r"(\d+(?:[.,]\d+)?)"
            r"\s+"
            r"(?:percent(?:s)?|"
            r"процент(?:а|ов)?)"
            r"\b",
            text,
        )

    if percent_match is not None:
        value = float(percent_match.group(1).replace(",", "."))

        if 0 < value < 100:
            return value

        return None

    # Explicit mathematical fractions.
    fraction_match = re.search(
        r"(?<!\d)"
        r"(\d+)"
        r"\s*/\s*"
        r"(\d+)"
        r"(?!\d)",
        text,
    )

    if fraction_match is not None:
        numerator = int(fraction_match.group(1))
        denominator = int(fraction_match.group(2))

        if denominator > 0 and 0 < numerator < denominator:
            return numerator / denominator * 100

        return None

    # Common exact natural-language fractions.
    if re.search(r"\bhalf\b", text) or "половин" in text:
        return 50.0

    if re.search(r"\bquarter\b", text) or "четверт" in text:
        return 25.0

    # This helper is called only after the model has
    # classified the current caption as an explicit
    # REDUCE instruction and the exact caption evidence
    # has passed the destructive-action evidence gate.
    return DEFAULT_REDUCTION_PCT


def _signals_from_extraction(
    source: SourceMessage,
    extraction: IntentExtraction,
) -> SignalExtraction:
    opens: list[TradingIntent] = []

    for raw in extraction.intents:
        relation = raw.relation or OpenRelation.UNCLASSIFIED

        if relation is OpenRelation.UPDATE_EXISTING:
            logger.info(
                "Ignoring UPDATE_EXISTING candidate from %s/%s for %s",
                source.channel_id,
                source.message_id,
                raw.symbol,
            )
            continue

        side = _side_from_transport(raw.side) or _side_from_transport(raw.direction)

        entry = _entry_from_transport(raw)

        if side is None or entry is None or raw.stop_loss is None:
            logger.warning(
                "Dropping incomplete OPEN candidate from %s/%s for %s",
                source.channel_id,
                source.message_id,
                raw.symbol,
            )
            continue

        try:
            intent = TradingIntent(
                source=source,
                symbol=raw.symbol,
                side=side,
                entry=entry,
                stop_loss=raw.stop_loss,
                take_profit=raw.take_profit,
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
            continue

        opens.append(intent)

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

        if action_type is PositionActionType.CLOSE:
            close_pct = None
        else:
            close_pct = _reduction_pct_from_evidence(action_evidence)

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
                continue

            if raw.close_pct is not None and abs(raw.close_pct - close_pct) > 1e-9:
                logger.warning(
                    "Normalizing REDUCE close_pct for %s from model=%s to evidence=%s",
                    raw.symbol,
                    raw.close_pct,
                    close_pct,
                )

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

    return SignalExtraction(
        open_intents=tuple(opens),
        position_actions=tuple(actions),
    )


def _completion_content(
    response_data: dict[str, object],
) -> str:
    choices = response_data.get("choices")

    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenRouter response contains no completion choices")

    choice = choices[0]

    if not isinstance(
        choice,
        dict,
    ):
        raise ValueError("OpenRouter completion choice is invalid")

    provider = _response_provider(response_data)

    provider_label = provider or "unknown"

    provider_error = choice.get("error")

    finish_reason = choice.get("finish_reason")

    if finish_reason == "length":
        message = (
            "OpenRouter completion was truncated: "
            f"provider={provider_label}, "
            "finish_reason=length"
        )

        if provider is not None:
            raise OpenRouterProviderFailure(
                provider,
                message,
            )

        raise ValueError(message)

    if provider_error is not None or finish_reason == "error":
        error_code = None
        error_message = None

        if isinstance(
            provider_error,
            dict,
        ):
            error_code = provider_error.get("code")
            error_message = provider_error.get("message")

        message = (
            "OpenRouter provider failure: "
            f"provider={provider_label}, "
            f"code={error_code}, "
            f"message={error_message}"
        )

        if provider is not None:
            raise OpenRouterProviderFailure(
                provider,
                message,
            )

        raise ValueError(message)

    message = choice.get("message")

    if not isinstance(
        message,
        dict,
    ):
        raise ValueError("OpenRouter response contains no assistant message")

    content = message.get("content")

    if not isinstance(
        content,
        str,
    ):
        raise ValueError("OpenRouter response content is not a string")

    return content


class OpenRouterProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        inference_timeout_seconds: float = 45,
        max_attempts: int = 2,
        provider_cooldown_store: ProviderCooldownStore | None = None,
        persist_provider_cooldowns: bool = True,
        provider_cooldown_seconds: int = (12 * 60 * 60),
    ) -> None:
        if provider_cooldown_seconds <= 0:
            raise ValueError("provider_cooldown_seconds must be positive")

        self._model = model
        self._inference_timeout_seconds = inference_timeout_seconds
        self._max_attempts = max_attempts
        self._provider_cooldown_store = provider_cooldown_store
        self._persist_provider_cooldowns = persist_provider_cooldowns
        self._provider_cooldown_seconds = provider_cooldown_seconds

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(inference_timeout_seconds + 15),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _active_provider_cooldowns(
        self,
    ) -> tuple[str, ...]:
        if self._provider_cooldown_store is None:
            return ()

        try:
            return await (
                self._provider_cooldown_store.get_openrouter_provider_cooldowns()
            )
        except Exception:
            logger.exception("Failed to read OpenRouter provider cooldowns")
            return ()

    async def _cooldown_provider(
        self,
        failure: OpenRouterProviderFailure,
        ignored_providers: set[str],
    ) -> None:
        provider = failure.provider.strip().casefold()

        if not provider:
            return

        ignored_providers.add(provider)

        if (
            self._provider_cooldown_store is None
            or not self._persist_provider_cooldowns
        ):
            return

        try:
            await self._provider_cooldown_store.cooldown_openrouter_provider(
                provider,
                str(failure),
                self._provider_cooldown_seconds,
            )
        except Exception:
            logger.exception(
                "Failed to persist OpenRouter provider cooldown for %s",
                provider,
            )
            return

        logger.warning(
            "OpenRouter provider %s excluded after failure "
            "(cooldown policy %.1f hour(s)): %s",
            provider,
            (self._provider_cooldown_seconds / 3600),
            failure,
        )

    @staticmethod
    def _user_content(
        request: LLMRequest,
    ) -> str | list[dict[str, object]]:
        if not request.images:
            return request.user_text

        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": request.user_text,
            }
        ]

        for image in request.images:
            encoded = base64.b64encode(image.data).decode("ascii")

            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (f"data:{image.media_type};base64,{encoded}"),
                    },
                }
            )

        return content

    async def complete(
        self,
        request: LLMRequest,
    ) -> LLMResponse:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": request.system_prompt,
                },
                {
                    "role": "user",
                    "content": self._user_content(request),
                },
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "reasoning": {
                "effort": "none",
            },
            "provider": {
                "sort": "latency",
                "require_parameters": True,
                "allow_fallbacks": True,
                "ignore": list(STATIC_IGNORED_PROVIDERS),
            },
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_schema_name,
                    "strict": True,
                    "schema": request.response_schema,
                },
            },
        }

        last_error: Exception | None = None

        ignored_providers = {
            provider.casefold() for provider in STATIC_IGNORED_PROVIDERS
        }

        label = request.request_label or self._model

        for attempt in range(
            1,
            self._max_attempts + 1,
        ):
            ignored_providers.update(
                provider.casefold()
                for provider in (await self._active_provider_cooldowns())
            )

            provider_options = payload["provider"]

            assert isinstance(provider_options, dict)

            provider_options["ignore"] = sorted(ignored_providers)

            response_provider: str | None = None
            started = time.monotonic()

            try:
                async with asyncio.timeout(self._inference_timeout_seconds):
                    response = await self._client.post(
                        "/chat/completions",
                        json=payload,
                    )

                response.raise_for_status()

                response_data = response.json()

                if not isinstance(response_data, dict):
                    raise ValueError("OpenRouter response is not a JSON object")

                response_provider = _response_provider(response_data)

                content = _completion_content(response_data)

                if len(content) > 20_000:
                    message = (
                        "OpenRouter returned unexpectedly "
                        "large structured output "
                        f"({len(content)} characters)"
                    )

                    if response_provider is not None:
                        raise OpenRouterProviderFailure(
                            response_provider,
                            message,
                        )

                    raise ValueError(message)

                if request.response_validator is not None:
                    request.response_validator(content)

            except TimeoutError:
                elapsed = time.monotonic() - started

                last_error = RuntimeError(
                    "OpenRouter inference exceeded "
                    f"{self._inference_timeout_seconds:g}s "
                    f"({elapsed:.1f}s)"
                )

            except LLMResponseValidationError as exc:
                message = "OpenRouter returned invalid structured output"

                if response_provider is not None:
                    provider = response_provider.strip().casefold()

                    if provider:
                        ignored_providers.add(provider)

                    last_error = OpenRouterProviderFailure(
                        response_provider,
                        message,
                    )
                else:
                    last_error = RuntimeError(message)

                logger.warning(
                    "Invalid OpenRouter structured output for %s on attempt %d/%d: %s",
                    label,
                    attempt,
                    self._max_attempts,
                    exc,
                )

            except OpenRouterProviderFailure as exc:
                last_error = exc

                await self._cooldown_provider(
                    exc,
                    ignored_providers,
                )

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code

                if status != 429 and status < 500:
                    raise

                last_error = exc

                if status >= 500:
                    try:
                        error_data = exc.response.json()
                    except ValueError:
                        error_data = None

                    if isinstance(error_data, dict):
                        provider = _response_provider(error_data)

                        if provider is not None:
                            failure = OpenRouterProviderFailure(
                                provider,
                                (
                                    "OpenRouter HTTP "
                                    f"{status} provider "
                                    "failure: "
                                    f"provider={provider}"
                                ),
                            )

                            last_error = failure

                            await self._cooldown_provider(
                                failure,
                                ignored_providers,
                            )

            except (
                KeyError,
                TypeError,
                ValueError,
                httpx.RequestError,
            ) as exc:
                last_error = exc

            else:
                elapsed = time.monotonic() - started

                logger.info(
                    "OpenRouter inference for %s "
                    "(%d image(s)) completed in %.2fs "
                    "on attempt %d/%d",
                    label,
                    len(request.images),
                    elapsed,
                    attempt,
                    self._max_attempts,
                )

                return LLMResponse(
                    content=content,
                )

            if attempt < self._max_attempts:
                logger.warning(
                    "OpenRouter attempt %d/%d failed for %s: %s; retrying",
                    attempt,
                    self._max_attempts,
                    label,
                    last_error,
                )

                await asyncio.sleep(0.5 * attempt)

        raise LLMProviderFailure(
            "OpenRouter inference failed after "
            f"{self._max_attempts} attempt(s): "
            f"{last_error}"
        ) from last_error


def _validate_intent_extraction_response(
    content: str,
) -> None:
    try:
        IntentExtraction.model_validate_json(content)
    except ValidationError as exc:
        raise LLMResponseValidationError(str(exc)) from exc


class IntentExtractor:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        cache_identity: str,
        evaluation_cache: EvaluationCache | None = None,
        evaluation_cache_seconds: int = (6 * 60 * 60),
    ) -> None:
        if evaluation_cache_seconds <= 0:
            raise ValueError("evaluation_cache_seconds must be positive")

        cache_identity = cache_identity.strip()

        if not cache_identity:
            raise ValueError("cache_identity must not be empty")

        self._provider = provider
        self._cache_identity = cache_identity
        self._evaluation_cache = evaluation_cache
        self._evaluation_cache_seconds = evaluation_cache_seconds

    @property
    def cache_identity(self) -> str:
        return self._cache_identity

    async def close(self) -> None:
        await self._provider.close()

    async def _read_cached_evaluation(
        self,
        fingerprint: str,
    ) -> IntentExtraction | None:
        if self._evaluation_cache is None:
            return None

        try:
            payload = await self._evaluation_cache.get_evaluation(fingerprint)
        except Exception:
            logger.exception("Failed to read evaluation cache")
            return None

        if payload is None:
            return None

        try:
            return IntentExtraction.model_validate_json(payload)
        except ValidationError:
            logger.warning("Ignoring invalid cached evaluation")
            return None

    async def _cache_evaluation(
        self,
        fingerprint: str,
        extraction: IntentExtraction,
    ) -> None:
        if self._evaluation_cache is None:
            return

        try:
            await self._evaluation_cache.cache_evaluation(
                fingerprint,
                (extraction.model_dump_json()),
                self._evaluation_cache_seconds,
            )
        except Exception:
            logger.exception("Failed to persist evaluation cache")

    async def extract(
        self,
        post: IncomingPost,
        *,
        global_guidance: str | None = None,
        channel_guidance: str | None = None,
        position_context: SignalPositionContext | None = None,
        bypass_evaluation_cache: bool = False,
    ) -> SignalExtraction:
        source = post.source

        evaluation_fingerprint = _evaluation_fingerprint(
            post,
            model=self._cache_identity,
            global_guidance=(global_guidance),
            channel_guidance=(channel_guidance),
            position_context=position_context,
        )

        if not bypass_evaluation_cache:
            cached_extraction = await self._read_cached_evaluation(
                evaluation_fingerprint
            )

            if cached_extraction is not None:
                logger.info(
                    "Evaluation cache hit for %s/%s",
                    source.channel_id,
                    source.message_id,
                )

                cached_signals = _signals_from_extraction(
                    source,
                    cached_extraction,
                )

                if not cached_signals.actionable:
                    logger.info(
                        "No actionable intent for %s/%s: %s",
                        source.channel_id,
                        source.message_id,
                        cached_extraction.reason,
                    )

                return cached_signals

        response = await self._provider.complete(
            LLMRequest(
                system_prompt=SYSTEM_PROMPT,
                user_text=_build_user_text(
                    post,
                    global_guidance=global_guidance,
                    channel_guidance=channel_guidance,
                    position_context=position_context,
                ),
                response_schema_name=("trading_intent_extraction"),
                response_schema=(IntentExtraction.model_json_schema()),
                images=tuple(
                    LLMImage(
                        media_type=image.media_type,
                        data=image.data,
                    )
                    for image in post.images
                ),
                response_validator=(_validate_intent_extraction_response),
                request_label=(f"{source.channel_id}/{source.message_id}"),
            )
        )

        extraction = IntentExtraction.model_validate_json(response.content)

        await self._cache_evaluation(
            evaluation_fingerprint,
            extraction,
        )

        signals = _signals_from_extraction(
            source,
            extraction,
        )

        if not signals.actionable:
            logger.info(
                "No actionable intent for %s/%s: %s",
                source.channel_id,
                source.message_id,
                extraction.reason,
            )

        return signals
