from datetime import UTC, datetime

from cautious_crypto_bro.domain import (
    ImageAttachment,
    IncomingPost,
    SourceMessage,
)
from cautious_crypto_bro.openrouter import (
    _build_user_content,
)


def source() -> SourceMessage:
    now = datetime.now(UTC)

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
    content = _build_user_content(IncomingPost(source=source()))

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
            "url": ("data:image/png;base64,AQI="),
        },
    }


def test_guidance_is_added_to_prompt() -> None:
    content = _build_user_content(
        IncomingPost(source=source()),
        global_guidance="Global rule",
        channel_guidance="Trader rule",
    )

    assert isinstance(content, str)

    assert "Global guidance:\nGlobal rule" in content

    assert "Channel-specific guidance:\nTrader rule" in content

    assert content.index("Global guidance:") < content.index(
        "Channel-specific guidance:"
    )

    assert content.index("Channel-specific guidance:") < content.index(
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
                    "message": ("Network connection lost."),
                    "metadata": {
                        "error_type": ("provider_unavailable"),
                    },
                },
                "message": {
                    "role": "assistant",
                    "content": ('{"actionable": true'),
                },
            }
        ],
    }

    import pytest

    with pytest.raises(
        ValueError,
        match=("provider=NextBit.*code=502.*Network connection lost"),
    ):
        _completion_content(response)


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
                    "content": ('{"actionable": true' + " " * 1000),
                },
            }
        ],
    }

    import pytest

    with pytest.raises(
        ValueError,
        match=("provider=Parasail.*finish_reason=length"),
    ):
        _completion_content(response)


class FakeProviderCooldownStore:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.recorded: list[tuple[str, str, int]] = []

    async def get_openrouter_provider_cooldowns(
        self,
    ) -> tuple[str, ...]:
        return tuple(sorted(self.active))

    async def cooldown_openrouter_provider(
        self,
        provider: str,
        reason: str,
        duration_seconds: int,
    ) -> None:
        self.active.add(provider)

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
        store = FakeProviderCooldownStore()

        payloads = []

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            payload = json.loads(request.content)

            payloads.append(payload)

            if len(payloads) == 1:
                return httpx.Response(
                    200,
                    json={
                        "provider": "Venice",
                        "choices": [
                            {
                                "finish_reason": ("length"),
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
                                            "reason": ("commentary"),
                                            "intents": [],
                                        }
                                    )
                                ),
                            },
                        }
                    ],
                },
            )

        extractor = OpenRouterIntentExtractor(
            api_key="test",
            model="test/model",
            base_url=("https://openrouter.test"),
            inference_timeout_seconds=5,
            max_attempts=2,
            provider_cooldown_store=store,
            provider_cooldown_seconds=(12 * 60 * 60),
        )

        await extractor._client.aclose()

        extractor._client = httpx.AsyncClient(
            base_url=("https://openrouter.test"),
            transport=(httpx.MockTransport(handler)),
        )

        try:
            result = await extractor.extract(IncomingPost(source=source()))
        finally:
            await extractor.close()

        assert result == ()
        assert len(payloads) == 2

        first_ignore = set(payloads[0]["provider"]["ignore"])

        second_ignore = set(payloads[1]["provider"]["ignore"])

        assert "venice" not in (first_ignore)

        assert {
            "nextbit",
            "parasail",
            "venice",
        } <= second_ignore

        assert store.recorded[0][0] == "venice"

        assert store.recorded[0][2] == 12 * 60 * 60

    asyncio.run(run())


def test_invalid_output_is_excluded_only_for_current_extraction() -> None:
    import asyncio
    import json

    import httpx

    from cautious_crypto_bro.openrouter import (
        OpenRouterIntentExtractor,
    )

    async def run() -> None:
        store = FakeProviderCooldownStore()

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
                                                "intents": [],
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
                                            "reason": ("commentary"),
                                            "intents": [],
                                        }
                                    )
                                ),
                            },
                        }
                    ],
                },
            )

        extractor = OpenRouterIntentExtractor(
            api_key="test",
            model="test/model",
            base_url=("https://openrouter.test"),
            inference_timeout_seconds=5,
            max_attempts=2,
            provider_cooldown_store=store,
        )

        await extractor._client.aclose()

        extractor._client = httpx.AsyncClient(
            base_url=("https://openrouter.test"),
            transport=(httpx.MockTransport(handler)),
        )

        try:
            result = await extractor.extract(IncomingPost(source=source()))
        finally:
            await extractor.close()

        assert result == ()
        assert calls == 2

        assert store.recorded == []

        assert calls == 2

    asyncio.run(run())


class FakeEvaluationCache:
    def __init__(self) -> None:
        self.values = {}
        self.writes = []

    async def get_openrouter_evaluation(
        self,
        fingerprint,
    ):
        return self.values.get(fingerprint)

    async def cache_openrouter_evaluation(
        self,
        fingerprint,
        payload_json,
        duration_seconds,
    ):
        self.values[fingerprint] = payload_json

        self.writes.append(
            (
                fingerprint,
                payload_json,
                duration_seconds,
            )
        )


def test_valid_evaluation_is_reused_from_cache() -> None:
    import asyncio
    import json

    import httpx

    from cautious_crypto_bro.openrouter import (
        OpenRouterIntentExtractor,
    )

    async def run() -> None:
        calls = 0
        cache = FakeEvaluationCache()

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal calls
            calls += 1

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
                                            "reason": "commentary",
                                            "intents": [],
                                        }
                                    )
                                ),
                            },
                        }
                    ],
                },
            )

        extractor = OpenRouterIntentExtractor(
            api_key="test",
            model="test/model",
            base_url=("https://openrouter.test"),
            inference_timeout_seconds=5,
            max_attempts=1,
            evaluation_cache=cache,
            evaluation_cache_seconds=21600,
        )

        await extractor._client.aclose()

        extractor._client = httpx.AsyncClient(
            base_url=("https://openrouter.test"),
            transport=(httpx.MockTransport(handler)),
        )

        try:
            item = IncomingPost(source=source())

            first = await extractor.extract(item)

            second = await extractor.extract(item)
        finally:
            await extractor.close()

        assert first == ()
        assert second == ()
        assert calls == 1
        assert len(cache.writes) == 1
        assert cache.writes[0][2] == 21600

    asyncio.run(run())


def test_guidance_change_invalidates_evaluation_cache() -> None:
    import asyncio
    import json

    import httpx

    from cautious_crypto_bro.openrouter import (
        OpenRouterIntentExtractor,
    )

    async def run() -> None:
        calls = 0
        cache = FakeEvaluationCache()

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal calls
            calls += 1

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
                                            "reason": "commentary",
                                            "intents": [],
                                        }
                                    )
                                ),
                            },
                        }
                    ],
                },
            )

        extractor = OpenRouterIntentExtractor(
            api_key="test",
            model="test/model",
            base_url=("https://openrouter.test"),
            inference_timeout_seconds=5,
            max_attempts=1,
            evaluation_cache=cache,
        )

        await extractor._client.aclose()

        extractor._client = httpx.AsyncClient(
            base_url=("https://openrouter.test"),
            transport=(httpx.MockTransport(handler)),
        )

        try:
            item = IncomingPost(source=source())

            await extractor.extract(
                item,
                global_guidance="rule A",
            )

            await extractor.extract(
                item,
                global_guidance="rule B",
            )
        finally:
            await extractor.close()

        assert calls == 2
        assert len(cache.writes) == 2

    asyncio.run(run())


def test_multiple_actionable_intents_are_returned() -> None:
    import asyncio
    import json

    import httpx

    from cautious_crypto_bro.openrouter import (
        OpenRouterIntentExtractor,
    )

    async def run() -> None:
        response_payload = {
            "actionable": True,
            "reason": "Two complete independent setups",
            "intents": [
                {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": {
                        "type": "LIMIT",
                        "price": 100,
                        "range_low": None,
                        "range_high": None,
                    },
                    "stop_loss": 90,
                    "take_profit": 120,
                    "summary": "BTC long setup",
                    "confidence": 0.95,
                },
                {
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "entry": {
                        "type": "LIMIT",
                        "price": 200,
                        "range_low": None,
                        "range_high": None,
                    },
                    "stop_loss": 220,
                    "take_profit": 170,
                    "summary": "ETH short setup",
                    "confidence": 0.9,
                },
            ],
        }

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "provider": "Healthy",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(response_payload),
                            },
                        }
                    ],
                },
            )

        extractor = OpenRouterIntentExtractor(
            api_key="test",
            model="test/model",
            base_url="https://openrouter.test",
            inference_timeout_seconds=5,
            max_attempts=1,
        )

        await extractor._client.aclose()

        extractor._client = httpx.AsyncClient(
            base_url="https://openrouter.test",
            transport=httpx.MockTransport(handler),
        )

        try:
            intents = await extractor.extract(IncomingPost(source=source()))
        finally:
            await extractor.close()

        assert len(intents) == 2
        assert intents[0].symbol == "BTCUSDT"
        assert intents[1].symbol == "ETHUSDT"

    asyncio.run(run())
