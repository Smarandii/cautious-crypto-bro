from datetime import datetime, timezone

from cautious_crypto_bro.domain import (
    ImageAttachment,
    IncomingPost,
    SourceMessage,
)
from cautious_crypto_bro.openrouter import (
    _build_user_content,
)


def source() -> SourceMessage:
    now = datetime.now(timezone.utc)

    return SourceMessage(
        channel_id=-1001234567890,
        channel_title="Test channel",
        channel_username=None,
        message_id=123,
        published_at=now,
        received_at=now,
        text="LONG BTCUSDT",
    )


def test_text_post_keeps_text_content() -> None:
    content = _build_user_content(
        IncomingPost(source=source())
    )

    assert isinstance(content, str)
    assert "LONG BTCUSDT" in content


def test_image_post_builds_base64_content() -> None:
    content = _build_user_content(
        IncomingPost(
            source=source(),
            images=(
                ImageAttachment(
                    media_type="image/png",
                    data=b"\x01\x02",
                ),
            ),
        )
    )

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": (
                "data:image/png;base64,AQI="
            ),
        },
    }


def test_guidance_is_added_to_prompt() -> None:
    content = _build_user_content(
        IncomingPost(source=source()),
        global_guidance="Global rule",
        channel_guidance="Trader rule",
    )

    assert isinstance(content, str)

    assert (
        "Global guidance:\nGlobal rule"
        in content
    )

    assert (
        "Channel-specific guidance:\n"
        "Trader rule"
        in content
    )

    assert content.index(
        "Global guidance:"
    ) < content.index(
        "Channel-specific guidance:"
    )

    assert content.index(
        "Channel-specific guidance:"
    ) < content.index(
        "Telegram post text/caption:"
    )


def test_provider_error_inside_http_success_is_rejected() -> None:
    from cautious_crypto_bro.openrouter import (
        _completion_content,
    )

    response = {
        "provider": "NextBit",
        "choices": [
            {
                "finish_reason": "error",
                "error": {
                    "code": 502,
                    "message": (
                        "Network connection lost."
                    ),
                    "metadata": {
                        "error_type": (
                            "provider_unavailable"
                        ),
                    },
                },
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"actionable": true'
                    ),
                },
            }
        ],
    }

    import pytest

    with pytest.raises(
        ValueError,
        match=(
            "provider=NextBit.*"
            "code=502.*"
            "Network connection lost"
        ),
    ):
        _completion_content(
            response
        )


def test_truncated_completion_is_rejected() -> None:
    from cautious_crypto_bro.openrouter import (
        _completion_content,
    )

    response = {
        "provider": "Parasail",
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"actionable": true'
                        + " " * 1000
                    ),
                },
            }
        ],
    }

    import pytest

    with pytest.raises(
        ValueError,
        match=(
            "provider=Parasail.*"
            "finish_reason=length"
        ),
    ):
        _completion_content(
            response
        )


class FakeProviderCooldownStore:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.recorded: list[
            tuple[str, str, int]
        ] = []

    async def get_openrouter_provider_cooldowns(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                self.active
            )
        )

    async def cooldown_openrouter_provider(
        self,
        provider: str,
        reason: str,
        duration_seconds: int,
    ) -> None:
        self.active.add(
            provider
        )

        self.recorded.append(
            (
                provider,
                reason,
                duration_seconds,
            )
        )


def test_failed_provider_is_excluded_on_retry() -> None:
    import asyncio
    import json

    import httpx

    from cautious_crypto_bro.openrouter import (
        OpenRouterIntentExtractor,
    )

    async def run() -> None:
        store = (
            FakeProviderCooldownStore()
        )

        payloads = []

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            payload = json.loads(
                request.content
            )

            payloads.append(
                payload
            )

            if len(payloads) == 1:
                return httpx.Response(
                    200,
                    json={
                        "provider": "Venice",
                        "choices": [
                            {
                                "finish_reason": (
                                    "length"
                                ),
                                "message": {
                                    "content": "{}",
                                },
                            }
                        ],
                    },
                )

            return httpx.Response(
                200,
                json={
                    "provider": "Healthy",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": (
                                    json.dumps(
                                        {
                                            "actionable": False,
                                            "reason": (
                                                "commentary"
                                            ),
                                            "intent": None,
                                        }
                                    )
                                ),
                            },
                        }
                    ],
                },
            )

        extractor = (
            OpenRouterIntentExtractor(
                api_key="test",
                model="test/model",
                base_url=(
                    "https://openrouter.test"
                ),
                inference_timeout_seconds=5,
                max_attempts=2,
                provider_cooldown_store=store,
                provider_cooldown_seconds=(
                    12 * 60 * 60
                ),
            )
        )

        await extractor._client.aclose()

        extractor._client = (
            httpx.AsyncClient(
                base_url=(
                    "https://openrouter.test"
                ),
                transport=(
                    httpx.MockTransport(
                        handler
                    )
                ),
            )
        )

        try:
            result = (
                await extractor.extract(
                    IncomingPost(
                        source=source()
                    )
                )
            )
        finally:
            await extractor.close()

        assert result is None
        assert len(payloads) == 2

        first_ignore = set(
            payloads[0][
                "provider"
            ]["ignore"]
        )

        second_ignore = set(
            payloads[1][
                "provider"
            ]["ignore"]
        )

        assert "venice" not in (
            first_ignore
        )

        assert {
            "nextbit",
            "parasail",
            "venice",
        } <= second_ignore

        assert (
            store.recorded[0][0]
            == "venice"
        )

        assert (
            store.recorded[0][2]
            == 12 * 60 * 60
        )

    asyncio.run(
        run()
    )


def test_invalid_output_cools_down_named_provider() -> None:
    import asyncio
    import json

    import httpx

    from cautious_crypto_bro.openrouter import (
        OpenRouterIntentExtractor,
    )

    async def run() -> None:
        store = (
            FakeProviderCooldownStore()
        )

        calls = 0

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal calls
            calls += 1

            if calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "provider": "Venice",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": (
                                        json.dumps(
                                            {
                                                "actionable": True,
                                                "reason": "invalid",
                                                "intent": None,
                                            }
                                        )
                                    ),
                                },
                            }
                        ],
                    },
                )

            return httpx.Response(
                200,
                json={
                    "provider": "Healthy",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": (
                                    json.dumps(
                                        {
                                            "actionable": False,
                                            "reason": (
                                                "commentary"
                                            ),
                                            "intent": None,
                                        }
                                    )
                                ),
                            },
                        }
                    ],
                },
            )

        extractor = (
            OpenRouterIntentExtractor(
                api_key="test",
                model="test/model",
                base_url=(
                    "https://openrouter.test"
                ),
                inference_timeout_seconds=5,
                max_attempts=2,
                provider_cooldown_store=store,
            )
        )

        await extractor._client.aclose()

        extractor._client = (
            httpx.AsyncClient(
                base_url=(
                    "https://openrouter.test"
                ),
                transport=(
                    httpx.MockTransport(
                        handler
                    )
                ),
            )
        )

        try:
            result = (
                await extractor.extract(
                    IncomingPost(
                        source=source()
                    )
                )
            )
        finally:
            await extractor.close()

        assert result is None
        assert calls == 2

        assert (
            store.recorded[0][0]
            == "venice"
        )

        assert (
            "invalid structured output"
            in store.recorded[0][1]
        )

    asyncio.run(
        run()
    )
