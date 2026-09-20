import asyncio
import json

import httpx

from cautious_crypto_bro.llm_provider import (
    LLMImage,
    LLMRequest,
    LLMResponseValidationError,
)
from cautious_crypto_bro.opencode_go import (
    OpenCodeGoProvider,
)


def _request(
    *,
    validator=None,
    images=(),
) -> LLMRequest:
    return LLMRequest(
        system_prompt="system instructions",
        user_text="LONG BTCUSDT",
        response_schema_name="trade_schema",
        response_schema={
            "type": "object",
            "properties": {
                "actionable": {
                    "type": "boolean",
                }
            },
            "required": [
                "actionable",
            ],
            "additionalProperties": False,
        },
        images=images,
        response_validator=validator,
        request_label="-100123/42",
    )


def test_responses_payload_supports_images_and_schema() -> None:
    async def run() -> None:
        seen = []

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            seen.append(
                {
                    "headers": request.headers,
                    "payload": json.loads(request.content),
                }
            )

            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": ("output_text"),
                                    "text": ('{"actionable":false}'),
                                }
                            ],
                        }
                    ],
                },
            )

        provider = OpenCodeGoProvider(
            api_key="test-key",
            model="gpt-5.6-luna",
            base_url="https://opencode.test",
            inference_timeout_seconds=5,
            max_attempts=1,
        )

        await provider._client.aclose()

        provider._client = httpx.AsyncClient(
            base_url="https://opencode.test",
            headers={
                "Authorization": ("Bearer test-key"),
                "Content-Type": ("application/json"),
                "User-Agent": ("cautious-crypto-bro/0.3.1"),
            },
            transport=httpx.MockTransport(handler),
        )

        try:
            response = await provider.complete(
                _request(
                    images=(
                        LLMImage(
                            media_type="image/png",
                            data=b"image-data",
                        ),
                    )
                )
            )
        finally:
            await provider.close()

        assert response.content == ('{"actionable":false}')
        assert len(seen) == 1

        payload = seen[0]["payload"]
        headers = seen[0]["headers"]

        assert payload["model"] == ("gpt-5.6-luna")

        assert payload["instructions"] == ("system instructions")

        content = payload["input"][0]["content"]

        assert content[0] == {
            "type": "input_text",
            "text": "LONG BTCUSDT",
        }

        assert content[1]["type"] == ("input_image")

        assert content[1]["image_url"].startswith("data:image/png;base64,")

        assert payload["text"]["format"] == {
            "type": "json_schema",
            "name": "trade_schema",
            "schema": (_request().response_schema),
            "strict": True,
        }

        assert headers["authorization"] == ("Bearer test-key")

        assert headers["user-agent"] == ("cautious-crypto-bro/0.3.1")

        assert headers["x-opencode-session"]

    asyncio.run(run())


def test_invalid_structured_output_is_retried() -> None:
    async def run() -> None:
        calls = []
        validation_calls = 0

        def validator(
            content: str,
        ) -> None:
            nonlocal validation_calls
            validation_calls += 1

            data = json.loads(content)

            if "actionable" not in data:
                raise LLMResponseValidationError("missing actionable")

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            calls.append(request.headers["x-opencode-session"])

            content = '{"wrong":true}' if len(calls) == 1 else '{"actionable":false}'

            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output_text": content,
                },
            )

        provider = OpenCodeGoProvider(
            api_key="test-key",
            model="gpt-5.6-luna",
            base_url="https://opencode.test",
            inference_timeout_seconds=5,
            max_attempts=2,
        )

        await provider._client.aclose()

        provider._client = httpx.AsyncClient(
            base_url="https://opencode.test",
            transport=httpx.MockTransport(handler),
        )

        try:
            response = await provider.complete(
                _request(
                    validator=validator,
                )
            )
        finally:
            await provider.close()

        assert response.content == ('{"actionable":false}')

        assert validation_calls == 2
        assert len(calls) == 2
        assert calls[0] == calls[1]

    asyncio.run(run())


def test_unsupported_model_is_rejected() -> None:
    import pytest

    with pytest.raises(
        ValueError,
        match="currently supports only",
    ):
        OpenCodeGoProvider(
            api_key="test",
            model="kimi-k3",
        )
