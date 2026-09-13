from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path

import httpx

from pydantic import ValidationError

from .domain import (
    IncomingPost,
    IntentExtraction,
    TradingIntent,
)
from .runtime_store import (
    ProviderCooldownStore,
)

logger = logging.getLogger(__name__)

STATIC_IGNORED_PROVIDERS = (
    "nextbit",
    "parasail",
)


class OpenRouterProviderFailure(
    ValueError
):
    def __init__(
        self,
        provider: str,
        message: str,
    ) -> None:
        super().__init__(
            message
        )
        self.provider = provider


def _response_provider(
    response_data: dict[
        str,
        object,
    ],
) -> str | None:
    provider = response_data.get(
        "provider"
    )

    if not isinstance(
        provider,
        str,
    ):
        return None

    provider = provider.strip()

    return (
        provider
        if provider
        else None
    )


SYSTEM_PROMPT = """
You are a conservative crypto trading-signal parser.
Do not give trading advice and do not invent missing information.

Decide whether ONE Telegram post, including any attached images,
contains one complete, immediately actionable trade proposal
representable by the supplied schema.

Context rules:
- use the text/caption and attached images together
- global guidance contains interpretation rules that apply to all traders
- channel-specific guidance explains conventions used by this trader
- channel-specific guidance is more specific than global guidance when
  interpreting trader terminology or chart conventions
- the current post and image are always the primary evidence
- guidance explains conventions only; never copy example prices from
  guidance into the current trade
- a derived numeric value may be used only when guidance provides an
  explicit deterministic rule and every required numeric input is clearly
  available in the current post/image
- otherwise do not guess

MVP rules:
- only USDT linear/perpetual-style symbols
- required: symbol, LONG/SHORT, entry semantics, and stop loss
- take profit is optional; if the trader does not provide one, return take_profit=null and let deterministic execution policy derive fallback targets
- MARKET only if the author clearly says enter now/at market, clearly states they entered now, or an exchange position screenshot clearly shows that the position is already open
- LIMIT when there is one explicit numeric entry price
- RANGE when the author clearly defines an entry area/zone and both numeric
  boundaries are explicit and clearly attributable to that entry area
- for RANGE set range_low to the lower numeric boundary and range_high to
  the higher numeric boundary; do not collapse a range into one price
- exactly one stop; use at most one trader-provided target
- never invent a trader target; missing take profit is allowed
- values visible in an image may be used only when they are explicit
  and clearly legible
- never estimate prices from chart geometry, line position, vague levels,
  or unlabeled visual elements
- never invent a range boundary from the visual size of a rectangle
- never assign a visible price to stop loss or take profit merely because
  the schema requires one
- if the post is commentary, an update to an older idea, incomplete,
  ambiguous, or contains multiple conflicting setups, actionable=false
- a missing take profit alone does not make an otherwise complete trade setup non-actionable
- confidence is extraction confidence, not probability of profit
- summary is one short sentence describing the trader's stated thesis
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
        encoded = base64.b64encode(
            image.data
        ).decode("ascii")

        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{image.media_type};"
                        f"base64,{encoded}"
                    ),
                },
            }
        )

    return content


def _completion_content(
    response_data: dict[str, object],
) -> str:
    choices = response_data.get(
        "choices"
    )

    if (
        not isinstance(choices, list)
        or not choices
    ):
        raise ValueError(
            "OpenRouter response contains "
            "no completion choices"
        )

    choice = choices[0]

    if not isinstance(
        choice,
        dict,
    ):
        raise ValueError(
            "OpenRouter completion choice "
            "is invalid"
        )

    provider = (
        _response_provider(
            response_data
        )
    )

    provider_label = (
        provider
        or "unknown"
    )

    provider_error = (
        choice.get("error")
    )

    finish_reason = choice.get(
        "finish_reason"
    )

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

        raise ValueError(
            message
        )

    if (
        provider_error is not None
        or finish_reason == "error"
    ):
        error_code = None
        error_message = None

        if isinstance(
            provider_error,
            dict,
        ):
            error_code = (
                provider_error.get("code")
            )
            error_message = (
                provider_error.get(
                    "message"
                )
            )

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

        raise ValueError(
            message
        )

    message = choice.get(
        "message"
    )

    if not isinstance(
        message,
        dict,
    ):
        raise ValueError(
            "OpenRouter response contains "
            "no assistant message"
        )

    content = message.get(
        "content"
    )

    if not isinstance(
        content,
        str,
    ):
        raise ValueError(
            "OpenRouter response content "
            "is not a string"
        )

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
        provider_cooldown_store: (
            ProviderCooldownStore
            | None
        ) = None,
        provider_cooldown_seconds: int = (
            12 * 60 * 60
        ),
    ) -> None:
        if provider_cooldown_seconds <= 0:
            raise ValueError(
                "provider_cooldown_seconds "
                "must be positive"
            )

        self._model = model
        self._inference_timeout_seconds = (
            inference_timeout_seconds
        )
        self._max_attempts = max_attempts
        self._provider_cooldown_store = (
            provider_cooldown_store
        )
        self._provider_cooldown_seconds = (
            provider_cooldown_seconds
        )

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                inference_timeout_seconds + 15
            ),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _active_provider_cooldowns(
        self,
    ) -> tuple[str, ...]:
        if (
            self._provider_cooldown_store
            is None
        ):
            return ()

        try:
            return await (
                self._provider_cooldown_store
                .get_openrouter_provider_cooldowns()
            )
        except Exception:
            logger.exception(
                "Failed to read OpenRouter "
                "provider cooldowns"
            )
            return ()

    async def _cooldown_provider(
        self,
        failure: OpenRouterProviderFailure,
        ignored_providers: set[str],
    ) -> None:
        provider = (
            failure.provider
            .strip()
            .casefold()
        )

        if not provider:
            return

        # Always exclude it for this extraction,
        # even if Redis temporarily fails.
        ignored_providers.add(
            provider
        )

        if (
            self._provider_cooldown_store
            is None
        ):
            return

        try:
            await (
                self._provider_cooldown_store
                .cooldown_openrouter_provider(
                    provider,
                    str(failure),
                    self._provider_cooldown_seconds,
                )
            )
        except Exception:
            logger.exception(
                "Failed to persist OpenRouter "
                "provider cooldown for %s",
                provider,
            )
            return

        logger.warning(
            "OpenRouter provider %s cooled "
            "down for %.1f hour(s): %s",
            provider,
            (
                self._provider_cooldown_seconds
                / 3600
            ),
            failure,
        )

    async def extract(
        self,
        post: IncomingPost,
        *,
        global_guidance: str | None = None,
        channel_guidance: str | None = None,
        debug_dir: Path | None = None,
    ) -> TradingIntent | None:
        source = post.source

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
            "max_tokens": 512,
            "reasoning": {
                "effort": "none",
            },
            "provider": {
                "sort": "latency",
                "require_parameters": True,
                "allow_fallbacks": True,
                "ignore": list(
                    STATIC_IGNORED_PROVIDERS
                ),
            },
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "trading_intent_extraction",
                    "strict": True,
                    "schema": (
                        IntentExtraction.model_json_schema()
                    ),
                },
            },
        }

        last_error: Exception | None = None

        ignored_providers = {
            provider.casefold()
            for provider
            in STATIC_IGNORED_PROVIDERS
        }

        for attempt in range(
            1,
            self._max_attempts + 1,
        ):
            ignored_providers.update(
                provider.casefold()
                for provider
                in (
                    await self
                    ._active_provider_cooldowns()
                )
            )

            provider_options = (
                payload["provider"]
            )

            assert isinstance(
                provider_options,
                dict,
            )

            provider_options["ignore"] = (
                sorted(
                    ignored_providers
                )
            )

            response_provider: (
                str | None
            ) = None

            started = time.monotonic()

            try:
                async with asyncio.timeout(
                    self._inference_timeout_seconds
                ):
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
                    raise ValueError(
                        "OpenRouter response "
                        "is not a JSON object"
                    )

                response_provider = (
                    _response_provider(
                        response_data
                    )
                )

                content = _completion_content(
                    response_data
                )

                if debug_dir is not None:
                    debug_dir.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    stem = (
                        f"{source.channel_id}_"
                        f"{source.message_id}_"
                        f"attempt{attempt}"
                    )

                    response_path = (
                        debug_dir
                        / f"{stem}.response.json"
                    )

                    content_path = (
                        debug_dir
                        / f"{stem}.content.txt"
                    )

                    metadata_path = (
                        debug_dir
                        / f"{stem}.meta.json"
                    )

                    response_path.write_text(
                        response.text,
                        encoding="utf-8",
                    )

                    content_path.write_text(
                        content,
                        encoding="utf-8",
                    )

                    image_bytes = [
                        len(image.data)
                        for image in post.images
                    ]

                    estimated_base64_chars = [
                        (
                            (size + 2)
                            // 3
                            * 4
                        )
                        for size in image_bytes
                    ]

                    metadata = {
                        "channel_id": (
                            source.channel_id
                        ),
                        "message_id": (
                            source.message_id
                        ),
                        "attempt": attempt,
                        "model": self._model,
                        "http_status": (
                            response.status_code
                        ),
                        "image_count": (
                            len(post.images)
                        ),
                        "image_bytes": (
                            image_bytes
                        ),
                        "estimated_base64_chars": (
                            estimated_base64_chars
                        ),
                        "response_body_chars": (
                            len(response.text)
                        ),
                        "content_chars": (
                            len(content)
                        ),
                        "elapsed_seconds": (
                            time.monotonic()
                            - started
                        ),
                    }

                    metadata_path.write_text(
                        json.dumps(
                            metadata,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    logger.info(
                        "Saved OpenRouter debug "
                        "capture to %s",
                        debug_dir,
                    )

                if not isinstance(
                    content,
                    str,
                ):
                    raise ValueError(
                        "OpenRouter response content "
                        "is not a string"
                    )

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

                    if (
                        response_provider
                        is not None
                    ):
                        raise (
                            OpenRouterProviderFailure(
                                response_provider,
                                message,
                            )
                        )

                    raise ValueError(
                        message
                    )

                extraction = (
                    IntentExtraction.model_validate_json(
                        content
                    )
                )

            except TimeoutError:
                elapsed = (
                    time.monotonic()
                    - started
                )

                last_error = RuntimeError(
                    "OpenRouter inference exceeded "
                    f"{self._inference_timeout_seconds:g}s "
                    f"({elapsed:.1f}s)"
                )

            except ValidationError as exc:
                message = (
                    "OpenRouter returned invalid "
                    "structured output"
                )

                if (
                    response_provider
                    is not None
                ):
                    failure = (
                        OpenRouterProviderFailure(
                            response_provider,
                            message,
                        )
                    )

                    last_error = failure

                    await self._cooldown_provider(
                        failure,
                        ignored_providers,
                    )
                else:
                    last_error = RuntimeError(
                        message
                    )

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
                status = (
                    exc.response.status_code
                )

                if (
                    status != 429
                    and status < 500
                ):
                    raise

                last_error = exc

                if status >= 500:
                    try:
                        error_data = (
                            exc.response.json()
                        )
                    except ValueError:
                        error_data = None

                    if isinstance(
                        error_data,
                        dict,
                    ):
                        provider = (
                            _response_provider(
                                error_data
                            )
                        )

                        if provider is not None:
                            failure = (
                                OpenRouterProviderFailure(
                                    provider,
                                    (
                                        "OpenRouter HTTP "
                                        f"{status} provider "
                                        "failure: "
                                        f"provider={provider}"
                                    ),
                                )
                            )

                            last_error = failure

                            await (
                                self._cooldown_provider(
                                    failure,
                                    ignored_providers,
                                )
                            )

            except (
                KeyError,
                TypeError,
                ValueError,
                httpx.RequestError,
            ) as exc:
                last_error = exc

            else:
                elapsed = (
                    time.monotonic()
                    - started
                )

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

            if (
                attempt
                < self._max_attempts
            ):
                logger.warning(
                    "OpenRouter attempt %d/%d failed "
                    "for %s/%s: %s; retrying",
                    attempt,
                    self._max_attempts,
                    source.channel_id,
                    source.message_id,
                    last_error,
                )

                await asyncio.sleep(
                    0.5 * attempt
                )

        else:
            raise RuntimeError(
                "OpenRouter inference failed after "
                f"{self._max_attempts} attempt(s): "
                f"{last_error}"
            ) from last_error

        if (
            not extraction.actionable
            or extraction.intent is None
        ):
            logger.info(
                "No actionable intent for %s/%s: %s",
                source.channel_id,
                source.message_id,
                extraction.reason,
            )
            return None

        raw = extraction.intent

        return TradingIntent(
            source=source,
            symbol=raw.symbol,
            side=raw.side,
            entry=raw.entry,
            stop_loss=raw.stop_loss,
            take_profit=raw.take_profit,
            summary=raw.summary,
            confidence=raw.confidence,
        )
