"""Provider-neutral model requests, evaluation caching and extraction."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace

from pydantic import ValidationError

from ..domain import (
    IncomingPost,
    IntentExtraction,
    SignalExtraction,
    SignalPositionContext,
    SourceMessage,
)
from ..llm_provider import (
    LLMImage,
    LLMProvider,
    LLMRequest,
    LLMResponseValidationError,
)
from ..ports import EvaluationCache
from .normalization import _signals_from_extraction

logger = logging.getLogger(__name__)
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
  explicit entry semantics; stop loss and take profit are optional
- UPDATE_EXISTING may omit entry or stop loss because it is informational
- omit missing stop loss and take profit; the execution planner supplies policy defaults
- leverage is the trader's explicitly stated multiplier (20x means leverage=20),
  from current text/caption or the matching position in an exchange screenshot
- omit leverage when missing, ambiguous, conflicting, or given as a range;
  never infer it from ROI, PnL, margin, position size, or another position
- symbol and direction alone, market commentary, or possible future areas without
  a clear entry instruction are not executable OPEN candidates
- MARKET when the author clearly says enter now/at market, clearly states
  they entered now, or an exchange screenshot clearly shows the position
  is already open
- for an already-open position screenshot, MARKET takes precedence over
  historical/average entry prices shown in the screenshot
- Caption lifecycle/status instructions take precedence over screenshot copying.
  A profit recap, "not closing yet", take-profit update, or move-to-breakeven
  instruction is NOT a new OPEN, even when this source was never copied.
  An image with an exit price or realized ROI is not evidence of a live entry.
  "Take profit after re-entry" describes an existing trade, not a new re-entry.
  Only extract a separate OPEN from such a post when it explicitly instructs
  a new entry now; never replace an unsupported stop update with OPEN.
- Promotional and performance-recap cards are NOT live exchange evidence.
  Treat ROI/PnL percentage badges, "results" or profit tables, multiple past
  symbols with historical open/current prices, QR codes, referral/partner
  codes and broker marketing branding as non-actionable marketing. Do not emit
  an OPEN from them even when they visually resemble a position panel.
- A screenshot may authorize a NEW OPEN only when it is an actual exchange
  position/order panel for a current position (with current entry/mark/liq/size
  or position controls), or when the current caption explicitly instructs an
  entry. Do not treat testimonials, promo posters or historical recap tables as
  live exchange panels.
- If the caption is empty and the images are only promotional recaps, return
  actionable=false; do not infer leverage or entry from marketing badges.
- if one current exchange screenshot shows multiple distinct live positions,
  extract EACH valid position as its own independent OPEN candidate when its
  symbol and side are available
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
- CANCEL_ENTRIES cancels pending entry orders, never positions or protection.
  Emit it for explicit current-caption cancellation of a named symbol's limit/
  entry orders (including "INJ улетела. Отменяем лимитку"). Include the symbol
  and cancellation instruction in evidence_text. Do not emit CLOSE instead.
  Separate canceled symbols from retained orders: "cancel INJ, keep AVAX" cancels
  INJ only. Scope is the current source channel's earlier entries for that symbol.
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
        "cache_version": 12,
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


def _validate_intent_extraction_response(
    content: str,
) -> None:
    try:
        IntentExtraction.model_validate_json(content)
    except ValidationError as exc:
        raise LLMResponseValidationError(str(exc)) from exc


def _log_if_not_actionable(
    source: SourceMessage,
    signals: SignalExtraction,
    extraction: IntentExtraction,
) -> None:
    if signals.actionable:
        return

    logger.info(
        "No actionable intent for %s/%s: %s",
        source.channel_id,
        source.message_id,
        extraction.reason,
    )


def _extraction_request(
    post: IncomingPost,
    *,
    global_guidance: str | None,
    channel_guidance: str | None,
    position_context: SignalPositionContext | None,
) -> LLMRequest:
    source = post.source

    return LLMRequest(
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
        if post.rejection_reason is not None:
            raise ValueError(post.rejection_reason)
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

                _log_if_not_actionable(
                    source,
                    cached_signals,
                    cached_extraction,
                )

                return replace(
                    cached_signals,
                    raw_extraction=cached_extraction,
                    evaluation_fingerprint=evaluation_fingerprint,
                    evaluation_model=self._cache_identity,
                )

        response = await self._provider.complete(
            _extraction_request(
                post,
                global_guidance=global_guidance,
                channel_guidance=channel_guidance,
                position_context=position_context,
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

        _log_if_not_actionable(
            source,
            signals,
            extraction,
        )

        return replace(
            signals,
            raw_extraction=extraction,
            evaluation_fingerprint=evaluation_fingerprint,
            evaluation_model=self._cache_identity,
        )
