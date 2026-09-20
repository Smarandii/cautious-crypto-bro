from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time

import httpx

from .llm_provider import (
    LLMRequest,
    LLMResponse,
    LLMResponseValidationError,
)

logger = logging.getLogger(__name__)

SUPPORTED_OPENCODE_GO_MODELS = {
    "gpt-5.6-luna",
}


def _strict_response_schema(
    schema: dict[str, object],
) -> dict[str, object]:
    """Normalize Pydantic JSON Schema for strict Responses output."""

    def normalize(
        value: object,
    ) -> object:
        if isinstance(value, list):
            return [normalize(item) for item in value]

        if not isinstance(value, dict):
            return value

        result: dict[str, object] = {}

        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON Schema keys must be strings")

            if key == "default":
                continue

            result[key] = normalize(item)

        properties = result.get("properties")

        if isinstance(properties, dict):
            required: list[str] = []

            for key in properties:
                if not isinstance(key, str):
                    raise TypeError("JSON Schema property names must be strings")

                required.append(key)

            result["required"] = required
            result["additionalProperties"] = False

        return result

    normalized = normalize(schema)

    if not isinstance(normalized, dict):
        raise TypeError("Response schema must normalize to an object")

    result: dict[str, object] = {}

    for key, value in normalized.items():
        if not isinstance(key, str):
            raise TypeError("JSON Schema keys must be strings")

        result[key] = value

    return result


def _response_text(
    response_data: dict[str, object],
) -> str:
    error = response_data.get("error")

    if error is not None:
        if isinstance(error, dict):
            message = error.get("message")
        else:
            message = error

        raise ValueError(f"OpenCode Go response error: {message}")

    status = response_data.get("status")

    if status in {
        "failed",
        "cancelled",
        "incomplete",
    }:
        details = response_data.get("incomplete_details")

        raise ValueError(
            f"OpenCode Go response did not complete: status={status}, details={details}"
        )

    output_text = response_data.get("output_text")

    if isinstance(output_text, str) and output_text:
        return output_text

    output = response_data.get("output")

    if not isinstance(output, list):
        raise ValueError("OpenCode Go response contains no output")

    parts: list[str] = []

    for item in output:
        if not isinstance(item, dict):
            continue

        if item.get("type") != "message":
            continue

        content = item.get("content")

        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            if block.get("type") != "output_text":
                continue

            text = block.get("text")

            if isinstance(text, str):
                parts.append(text)

    if not parts:
        raise ValueError("OpenCode Go response contains no output text")

    return "".join(parts)


class OpenCodeGoProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5.6-luna",
        base_url: str = "https://opencode.ai/zen/go/v1",
        inference_timeout_seconds: float = 60,
        max_attempts: int = 3,
    ) -> None:
        if model not in SUPPORTED_OPENCODE_GO_MODELS:
            raise ValueError(
                "OpenCode Go provider currently supports only: "
                + ", ".join(sorted(SUPPORTED_OPENCODE_GO_MODELS))
            )

        if inference_timeout_seconds <= 0:
            raise ValueError("inference_timeout_seconds must be positive")

        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        api_key = api_key.strip()

        if not api_key:
            raise ValueError("OpenCode Go API key must not be empty")

        self._model = model
        self._inference_timeout_seconds = inference_timeout_seconds
        self._max_attempts = max_attempts

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "cautious-crypto-bro/0.3.1",
            },
            timeout=httpx.Timeout(inference_timeout_seconds + 15),
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _session_id(
        request: LLMRequest,
    ) -> str:
        value = request.request_label or "cautious-crypto-bro"

        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _input_content(
        request: LLMRequest,
    ) -> list[dict[str, object]]:
        content: list[dict[str, object]] = [
            {
                "type": "input_text",
                "text": request.user_text,
            }
        ]

        for image in request.images:
            encoded = base64.b64encode(image.data).decode("ascii")

            content.append(
                {
                    "type": "input_image",
                    "image_url": (f"data:{image.media_type};base64,{encoded}"),
                }
            )

        return content

    def _payload(
        self,
        request: LLMRequest,
    ) -> dict[str, object]:
        return {
            "model": self._model,
            "instructions": request.system_prompt,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": self._input_content(request),
                }
            ],
            "max_output_tokens": request.max_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": (request.response_schema_name),
                    "schema": _strict_response_schema(request.response_schema),
                    "strict": True,
                }
            },
        }

    async def complete(
        self,
        request: LLMRequest,
    ) -> LLMResponse:
        payload = self._payload(request)

        label = request.request_label or self._model

        session_id = self._session_id(request)

        last_error: Exception | None = None

        for attempt in range(
            1,
            self._max_attempts + 1,
        ):
            started = time.monotonic()

            try:
                async with asyncio.timeout(self._inference_timeout_seconds):
                    response = await self._client.post(
                        "/responses",
                        json=payload,
                        headers={
                            "x-opencode-session": (session_id),
                        },
                    )

                response.raise_for_status()

                response_data = response.json()

                if not isinstance(
                    response_data,
                    dict,
                ):
                    raise ValueError("OpenCode Go response is not a JSON object")

                content = _response_text(response_data)

                if len(content) > 20_000:
                    raise ValueError(
                        "OpenCode Go returned "
                        "unexpectedly large structured "
                        f"output ({len(content)} "
                        "characters)"
                    )

                if request.response_validator is not None:
                    request.response_validator(content)

            except TimeoutError:
                elapsed = time.monotonic() - started

                last_error = RuntimeError(
                    "OpenCode Go inference exceeded "
                    f"{self._inference_timeout_seconds:g}s "
                    f"({elapsed:.1f}s)"
                )

            except LLMResponseValidationError as exc:
                last_error = exc

                logger.warning(
                    "Invalid OpenCode Go structured output for %s on attempt %d/%d: %s",
                    label,
                    attempt,
                    self._max_attempts,
                    exc,
                )

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                body = exc.response.text.strip()

                if len(body) > 4000:
                    body = body[:4000] + "..."

                message = f"OpenCode Go HTTP {status}" + (f": {body}" if body else "")

                if status != 429 and status < 500:
                    raise RuntimeError(message) from exc

                last_error = RuntimeError(message)

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
                    "OpenCode Go inference for %s "
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
                    "OpenCode Go attempt %d/%d failed for %s: %s; retrying",
                    attempt,
                    self._max_attempts,
                    label,
                    last_error,
                )

                await asyncio.sleep(0.5 * attempt)

        raise RuntimeError(
            "OpenCode Go inference failed after "
            f"{self._max_attempts} attempt(s): "
            f"{last_error}"
        ) from last_error
