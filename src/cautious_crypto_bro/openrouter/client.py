from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx

from ..llm_provider import (
    LLMProviderFailure,
    LLMRequest,
    LLMResponse,
    LLMResponseValidationError,
)
from ..runtime_store import ProviderCooldownStore
from ._shared import STATIC_IGNORED_PROVIDERS, OpenRouterProviderFailure

logger = logging.getLogger(__name__)


def _response_provider(
    response_data: dict[str, object],
) -> str | None:
    provider = response_data.get("provider")
    if not isinstance(provider, str):
        return None
    return provider.strip() or None


def _completion_content(
    response_data: dict[str, object],
) -> str:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenRouter response contains no completion choices")

    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("OpenRouter completion choice is invalid")

    provider = _response_provider(response_data)

    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        message = (
            "OpenRouter completion was truncated: "
            f"provider={provider or 'unknown'}, "
            "finish_reason=length"
        )
        if provider is not None:
            raise OpenRouterProviderFailure(provider, message)
        raise ValueError(message)

    error = choice.get("error")
    if error is not None or finish_reason == "error":
        error_code = error.get("code") if isinstance(error, dict) else None
        error_message = error.get("message") if isinstance(error, dict) else None
        message = (
            "OpenRouter provider failure: "
            f"provider={provider or 'unknown'}, "
            f"code={error_code}, "
            f"message={error_message}"
        )
        if provider is not None:
            raise OpenRouterProviderFailure(provider, message)
        raise ValueError(message)

    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("OpenRouter response contains no assistant message")

    content = message.get("content")
    if not isinstance(content, str):
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
