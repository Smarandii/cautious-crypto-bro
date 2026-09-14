from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from pathlib import Path

import httpx
from pydantic import ValidationError

from .domain import (
    IncomingPost,
    IntentExtraction,
    PositionActionIntent,
    SignalExtraction,
    SourceMessage,
    TradingIntent,
)
from .runtime_store import (
    OpenRouterEvaluationCache,
    ProviderCooldownStore,
)

logger = logging.getLogger(__name__)

STATIC_IGNORED_PROVIDERS = (
    "nextbit",
    "parasail",
)


class OpenRouterProviderFailure(ValueError):
    def __init__(
        self,
        provider: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.provider = provider


def _response_provider(
    response_data: dict[
        str,
        object,
    ],
) -> str | None:
    provider = response_data.get("provider")

    if not isinstance(
        provider,
        str,
    ):
        return None

    provider = provider.strip()

    return provider if provider else None


SYSTEM_PROMPT = """
You are a conservative crypto trading-signal parser.
Do not give trading advice and do not invent missing information.

Inspect ONE Telegram post, including all attached images, and extract:
1. zero or more independent new OPEN trade proposals;
2. zero or more executable actions on EXISTING positions.

Output rules:
- use the exact supplied JSON schema field names
- for OPEN intents use side and entry; never use aliases such as
  direction or entry_semantics
- actionable=true if at least one valid OPEN intent or position action exists
- actionable=false only when both intents and position_actions are empty
- maximum five OPEN intents and five position actions
- omit ambiguous candidates without discarding unrelated valid candidates

OPEN trade rules:
- only USDT linear/perpetual-style symbols
- each OPEN candidate requires symbol, LONG/SHORT, entry semantics,
  and stop loss
- take profit is optional
- MARKET when the author clearly says enter now/at market, clearly states
  they entered now, or an exchange screenshot clearly shows the position
  is already open
- for an already-open position screenshot, MARKET takes precedence over
  historical/average entry prices shown in the screenshot
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
- REDUCE requires an explicit deterministic amount in close_pct
- explicit percentages are allowed
- exact fractions are allowed when unambiguous:
  half = 50%, quarter = 25%
- vague phrases such as "take some profit", "fix a part", "trim a little",
  or equivalent wording without a deterministic amount are NOT executable;
  omit them from position_actions
- CLOSE means fully close the existing position
- for CLOSE set close_pct=null
- HOLD, keep holding, wait, do nothing, and similar instructions are
  informational and must be omitted
- do not convert CLOSE or REDUCE into an opposite-side OPEN trade
- expected_side is optional; populate it only when LONG/SHORT is clearly
  stated or clearly visible in the current post/image
- do not infer expected_side merely from old context
- an action requires a clearly attributable symbol

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


def _build_user_content(
    post: IncomingPost,
    *,
    global_guidance: str | None = None,
    channel_guidance: str | None = None,
) -> str | list[dict[str, object]]:
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

    parts.extend(
        [
            "",
            "Telegram post text/caption:",
            source.text or "(no caption)",
        ]
    )

    text = "\n".join(parts)

    if not post.images:
        return text

    content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": text,
        }
    ]

    for image in post.images:
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


def _evaluation_fingerprint(
    post: IncomingPost,
    *,
    model: str,
    global_guidance: str | None,
    channel_guidance: str | None,
) -> str:
    source = post.source

    fingerprint_payload = {
        "cache_version": 3,
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
    }

    canonical = json.dumps(
        fingerprint_payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _signals_from_extraction(
    source: SourceMessage,
    extraction: IntentExtraction,
) -> SignalExtraction:
    if not extraction.actionable:
        return SignalExtraction()

    opens = tuple(
        TradingIntent(
            source=source,
            symbol=raw.symbol,
            side=raw.side,
            entry=raw.entry,
            stop_loss=raw.stop_loss,
            take_profit=raw.take_profit,
            summary=raw.summary,
            confidence=raw.confidence,
        )
        for raw in extraction.intents
    )

    actions = tuple(
        PositionActionIntent(
            source=source,
            symbol=raw.symbol,
            action=raw.action,
            close_pct=raw.close_pct,
            expected_side=raw.expected_side,
            summary=raw.summary,
            confidence=raw.confidence,
        )
        for raw in extraction.position_actions
    )

    return SignalExtraction(
        open_intents=opens,
        position_actions=actions,
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


class OpenRouterIntentExtractor:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        inference_timeout_seconds: float = 45,
        max_attempts: int = 2,
        provider_cooldown_store: (ProviderCooldownStore | None) = None,
        provider_cooldown_seconds: int = (12 * 60 * 60),
        evaluation_cache: (OpenRouterEvaluationCache | None) = None,
        evaluation_cache_seconds: int = (6 * 60 * 60),
    ) -> None:
        if provider_cooldown_seconds <= 0:
            raise ValueError("provider_cooldown_seconds must be positive")

        if evaluation_cache_seconds <= 0:
            raise ValueError("evaluation_cache_seconds must be positive")

        self._model = model
        self._inference_timeout_seconds = inference_timeout_seconds
        self._max_attempts = max_attempts
        self._provider_cooldown_store = provider_cooldown_store
        self._provider_cooldown_seconds = provider_cooldown_seconds
        self._evaluation_cache = evaluation_cache
        self._evaluation_cache_seconds = evaluation_cache_seconds

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

        # Always exclude it for this extraction,
        # even if Redis temporarily fails.
        ignored_providers.add(provider)

        if self._provider_cooldown_store is None:
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

    async def _read_cached_evaluation(
        self,
        fingerprint: str,
    ) -> IntentExtraction | None:
        if self._evaluation_cache is None:
            return None

        try:
            payload = await self._evaluation_cache.get_openrouter_evaluation(
                fingerprint
            )
        except Exception:
            logger.exception("Failed to read OpenRouter evaluation cache")
            return None

        if payload is None:
            return None

        try:
            return IntentExtraction.model_validate_json(payload)
        except ValidationError:
            logger.warning("Ignoring invalid cached OpenRouter evaluation")
            return None

    async def _cache_evaluation(
        self,
        fingerprint: str,
        extraction: IntentExtraction,
    ) -> None:
        if self._evaluation_cache is None:
            return

        try:
            await self._evaluation_cache.cache_openrouter_evaluation(
                fingerprint,
                (extraction.model_dump_json()),
                self._evaluation_cache_seconds,
            )
        except Exception:
            logger.exception("Failed to persist OpenRouter evaluation cache")

    async def extract(
        self,
        post: IncomingPost,
        *,
        global_guidance: str | None = None,
        channel_guidance: str | None = None,
        debug_dir: Path | None = None,
    ) -> SignalExtraction:
        source = post.source

        evaluation_fingerprint = _evaluation_fingerprint(
            post,
            model=self._model,
            global_guidance=(global_guidance),
            channel_guidance=(channel_guidance),
        )

        if debug_dir is None:
            cached_extraction = await self._read_cached_evaluation(
                evaluation_fingerprint
            )

            if cached_extraction is not None:
                logger.info(
                    "OpenRouter evaluation cache hit for %s/%s",
                    source.channel_id,
                    source.message_id,
                )

                if not cached_extraction.actionable:
                    logger.info(
                        "No actionable intent for %s/%s: %s",
                        source.channel_id,
                        source.message_id,
                        cached_extraction.reason,
                    )

                return _signals_from_extraction(
                    source,
                    cached_extraction,
                )

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": _build_user_content(
                        post,
                        global_guidance=global_guidance,
                        channel_guidance=channel_guidance,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 1536,
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
                    "name": "trading_intent_extraction",
                    "strict": True,
                    "schema": (IntentExtraction.model_json_schema()),
                },
            },
        }

        last_error: Exception | None = None

        ignored_providers = {
            provider.casefold() for provider in STATIC_IGNORED_PROVIDERS
        }

        for attempt in range(
            1,
            self._max_attempts + 1,
        ):
            ignored_providers.update(
                provider.casefold()
                for provider in (await self._active_provider_cooldowns())
            )

            provider_options = payload["provider"]

            assert isinstance(
                provider_options,
                dict,
            )

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

                if not isinstance(
                    response_data,
                    dict,
                ):
                    raise ValueError("OpenRouter response is not a JSON object")

                response_provider = _response_provider(response_data)

                content = _completion_content(response_data)

                if debug_dir is not None:
                    debug_dir.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    stem = f"{source.channel_id}_{source.message_id}_attempt{attempt}"

                    response_path = debug_dir / f"{stem}.response.json"

                    content_path = debug_dir / f"{stem}.content.txt"

                    metadata_path = debug_dir / f"{stem}.meta.json"

                    response_path.write_text(
                        response.text,
                        encoding="utf-8",
                    )

                    content_path.write_text(
                        content,
                        encoding="utf-8",
                    )

                    image_bytes = [len(image.data) for image in post.images]

                    estimated_base64_chars = [
                        ((size + 2) // 3 * 4) for size in image_bytes
                    ]

                    metadata = {
                        "channel_id": (source.channel_id),
                        "message_id": (source.message_id),
                        "attempt": attempt,
                        "model": self._model,
                        "http_status": (response.status_code),
                        "image_count": (len(post.images)),
                        "image_bytes": (image_bytes),
                        "estimated_base64_chars": (estimated_base64_chars),
                        "response_body_chars": (len(response.text)),
                        "content_chars": (len(content)),
                        "elapsed_seconds": (time.monotonic() - started),
                    }

                    metadata_path.write_text(
                        json.dumps(
                            metadata,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    logger.info(
                        "Saved OpenRouter debug capture to %s",
                        debug_dir,
                    )

                if not isinstance(
                    content,
                    str,
                ):
                    raise ValueError("OpenRouter response content is not a string")

                # This schema normally produces only a
                # small JSON object. A very large result
                # indicates a broken structured-output
                # response rather than a useful intent.
                if len(content) > 20_000:
                    message = (
                        "OpenRouter returned unexpectedly "
                        "large structured output "
                        f"({len(content)} characters)"
                    )

                    if response_provider is not None:
                        raise (
                            OpenRouterProviderFailure(
                                response_provider,
                                message,
                            )
                        )

                    raise ValueError(message)

                extraction = IntentExtraction.model_validate_json(content)

            except TimeoutError:
                elapsed = time.monotonic() - started

                last_error = RuntimeError(
                    "OpenRouter inference exceeded "
                    f"{self._inference_timeout_seconds:g}s "
                    f"({elapsed:.1f}s)"
                )

            except ValidationError as exc:
                message = "OpenRouter returned invalid structured output"

                if response_provider is not None:
                    provider = response_provider.strip().casefold()

                    if provider:
                        # Schema non-conformance may be
                        # model/provider-specific for this
                        # particular request. Exclude it
                        # from this extraction retry only;
                        # do not poison the global pool.
                        ignored_providers.add(provider)

                    last_error = OpenRouterProviderFailure(
                        response_provider,
                        message,
                    )
                else:
                    last_error = RuntimeError(message)

                logger.warning(
                    "Invalid OpenRouter structured output "
                    "for %s/%s on attempt %d/%d: %s",
                    source.channel_id,
                    source.message_id,
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

                    if isinstance(
                        error_data,
                        dict,
                    ):
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
                    "OpenRouter inference for %s/%s "
                    "(%d image(s)) completed in %.2fs "
                    "on attempt %d/%d",
                    source.channel_id,
                    source.message_id,
                    len(post.images),
                    elapsed,
                    attempt,
                    self._max_attempts,
                )

                break

            if attempt < self._max_attempts:
                logger.warning(
                    "OpenRouter attempt %d/%d failed for %s/%s: %s; retrying",
                    attempt,
                    self._max_attempts,
                    source.channel_id,
                    source.message_id,
                    last_error,
                )

                await asyncio.sleep(0.5 * attempt)

        else:
            raise RuntimeError(
                "OpenRouter inference failed after "
                f"{self._max_attempts} attempt(s): "
                f"{last_error}"
            ) from last_error

        if debug_dir is None:
            await self._cache_evaluation(
                evaluation_fingerprint,
                extraction,
            )

        if not extraction.actionable:
            logger.info(
                "No actionable intent for %s/%s: %s",
                source.channel_id,
                source.message_id,
                extraction.reason,
            )

        return _signals_from_extraction(
            source,
            extraction,
        )
