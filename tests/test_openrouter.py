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

        assert not result.actionable
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
                                                "intents": "not-a-list",
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

        assert not result.actionable
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

        assert not first.actionable
        assert not second.actionable
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
            signals = await extractor.extract(IncomingPost(source=source()))
        finally:
            await extractor.close()

        assert len(signals.open_intents) == 2
        assert signals.open_intents[0].symbol == "BTCUSDT"
        assert signals.open_intents[1].symbol == "ETHUSDT"
        assert signals.position_actions == ()

    asyncio.run(run())


def test_position_actions_are_returned_separately() -> None:
    import asyncio
    import json

    import httpx

    from cautious_crypto_bro.domain import (
        PositionActionType,
        Side,
    )
    from cautious_crypto_bro.openrouter import (
        OpenRouterIntentExtractor,
    )

    async def run() -> None:
        payload = {
            "actionable": True,
            "reason": "Lifecycle instructions",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "NEARUSDT",
                    "action": "REDUCE",
                    "close_pct": 50,
                    "expected_side": "LONG",
                    "evidence_text": "Close half of NEAR",
                    "summary": "Close half of NEAR",
                    "confidence": 1,
                },
                {
                    "symbol": "TAOUSDT",
                    "action": "CLOSE",
                    "close_pct": None,
                    "expected_side": None,
                    "evidence_text": "Close TAO completely",
                    "summary": "Close TAO completely",
                    "confidence": 0.95,
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
                                "content": json.dumps(payload),
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
            lifecycle_source = source().model_copy(
                update={"text": ("Close half of NEAR. Close TAO completely.")}
            )

            signals = await extractor.extract(IncomingPost(source=lifecycle_source))
        finally:
            await extractor.close()

        assert signals.open_intents == ()
        assert len(signals.position_actions) == 2

        reduce = signals.position_actions[0]
        close = signals.position_actions[1]

        assert reduce.action is PositionActionType.REDUCE
        assert reduce.close_pct == 50
        assert reduce.expected_side is Side.LONG

        assert close.action is PositionActionType.CLOSE
        assert close.close_pct is None

    asyncio.run(run())


def test_market_string_with_price_is_normalized_safely() -> None:
    from cautious_crypto_bro.domain import (
        EntryType,
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Open position screenshot",
            "intents": [
                {
                    "symbol": "NEARUSDT",
                    "side": "LONG",
                    "entry": "MARKET",
                    "price": 2.3,
                    "stop_loss": 2.221,
                    "take_profit": None,
                    "summary": "NEAR long",
                    "confidence": 1,
                }
            ],
            "position_actions": [],
        }
    )

    signals = _signals_from_extraction(
        source(),
        extraction,
    )

    assert len(signals.open_intents) == 1

    intent = signals.open_intents[0]

    assert intent.entry.type is EntryType.MARKET
    assert intent.entry.price is None


def test_nested_market_with_price_is_normalized_safely() -> None:
    from cautious_crypto_bro.domain import (
        EntryType,
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Open position screenshot",
            "intents": [
                {
                    "symbol": "NEARUSDT",
                    "side": "LONG",
                    "entry": {
                        "type": "MARKET",
                        "price": 2.3,
                    },
                    "stop_loss": 2.221,
                    "take_profit": None,
                    "summary": "NEAR long",
                    "confidence": 1,
                }
            ],
            "position_actions": [],
        }
    )

    signals = _signals_from_extraction(
        source(),
        extraction,
    )

    assert len(signals.open_intents) == 1
    assert signals.open_intents[0].entry.type is EntryType.MARKET
    assert signals.open_intents[0].entry.price is None


def test_hold_with_open_fields_is_non_executable() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Existing positions shown",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "NEARUSDT",
                    "action": "HOLD",
                    "side": "LONG",
                    "entry": "MARKET",
                    "stop_loss": 2.221,
                    "take_profit": None,
                    "close_pct": None,
                    "summary": "Keep holding",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source(),
        extraction,
    )

    assert not signals.actionable
    assert signals.position_actions == ()


def test_position_action_side_alias_is_accepted() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
        PositionActionType,
        Side,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Close half",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "NEARUSDT",
                    "action": "REDUCE",
                    "close_pct": 50,
                    "side": "LONG",
                    "evidence_text": "Close half",
                    "summary": "Close half",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": "Close half"}),
        extraction,
    )

    assert len(signals.position_actions) == 1

    action = signals.position_actions[0]

    assert action.action is PositionActionType.REDUCE
    assert action.expected_side is Side.LONG
    assert action.close_pct == 50


def test_model_actionable_flag_does_not_override_domain() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Model claimed actionable",
            "intents": [],
            "position_actions": [],
        }
    )

    signals = _signals_from_extraction(
        source(),
        extraction,
    )

    assert not signals.actionable


def test_invalid_open_candidate_does_not_drop_valid_close() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
        PositionActionType,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Mixed output",
            "intents": [
                {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": "MARKET",
                    "stop_loss": 0,
                    "take_profit": None,
                    "summary": "Invalid BTC",
                    "confidence": 1,
                }
            ],
            "position_actions": [
                {
                    "symbol": "POLUSDT",
                    "action": "CLOSE",
                    "close_pct": None,
                    "expected_side": "LONG",
                    "evidence_text": "Close POL",
                    "summary": "Close POL",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": "Close POL"}),
        extraction,
    )

    assert signals.open_intents == ()
    assert len(signals.position_actions) == 1
    assert signals.position_actions[0].action is PositionActionType.CLOSE


def test_direction_alias_is_accepted_for_open_intent() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
        Side,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Provider used direction alias",
            "intents": [
                {
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "entry": "MARKET",
                    "stop_loss": 75000,
                    "take_profit": None,
                    "summary": "BTC long",
                    "confidence": 1,
                }
            ],
            "position_actions": [],
        }
    )

    signals = _signals_from_extraction(
        source(),
        extraction,
    )

    assert len(signals.open_intents) == 1
    assert signals.open_intents[0].side is Side.LONG


def test_image_only_close_cannot_authorize_lifecycle_action() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Exchange screenshot contains close UI",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "TRUMPUSDT",
                    "action": "CLOSE",
                    "close_pct": None,
                    "expected_side": "LONG",
                    "evidence_text": "Закрыть с помощью",
                    "summary": "Close TRUMP",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": ""}),
        extraction,
    )

    assert not signals.actionable
    assert signals.position_actions == ()


def test_embedded_old_chat_cannot_authorize_close() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Historical chat contains close command",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "FILUSDT",
                    "action": "CLOSE",
                    "close_pct": None,
                    "expected_side": "LONG",
                    "evidence_text": "Закрывай",
                    "summary": "Close FIL",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(
            update={
                "text": (
                    "С утра мы забрали +500% "
                    "профита по FIL. "
                    "Наберу людей на личную торговлю."
                )
            }
        ),
        extraction,
    )

    assert not signals.actionable
    assert signals.position_actions == ()


def test_exact_current_caption_authorizes_close() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
        PositionActionType,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Explicit current close",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "NEARUSDT",
                    "action": "CLOSE",
                    "close_pct": None,
                    "expected_side": "LONG",
                    "evidence_text": "Закрываем",
                    "summary": "Close NEAR",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": "Закрываем 🙂‍↕️"}),
        extraction,
    )

    assert len(signals.position_actions) == 1
    assert signals.position_actions[0].action is PositionActionType.CLOSE


def test_reduce_half_is_derived_as_50_percent() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Close half",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "NEARUSDT",
                    "action": "REDUCE",
                    # Observed Gemma output.
                    "close_pct": 0.5,
                    "expected_side": "LONG",
                    "evidence_text": ("Закрываем половину"),
                    "summary": "Close half",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": ("Закрываем половину 🕺")}),
        extraction,
    )

    assert len(signals.position_actions) == 1

    assert signals.position_actions[0].close_pct == 50


def test_explicit_sub_one_percent_is_preserved() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Explicit percentage",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "NEARUSDT",
                    "action": "REDUCE",
                    "close_pct": 50,
                    "expected_side": "LONG",
                    "evidence_text": ("Close 0.5%"),
                    "summary": "Reduce 0.5%",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": "Close 0.5%"}),
        extraction,
    )

    assert signals.position_actions[0].close_pct == 0.5


def test_vague_reduce_cannot_use_model_percentage() -> None:
    from cautious_crypto_bro.domain import (
        IntentExtraction,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Vague reduction",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "NEARUSDT",
                    "action": "REDUCE",
                    "close_pct": 50,
                    "expected_side": "LONG",
                    "evidence_text": ("Фиксируем часть"),
                    "summary": "Reduce position",
                    "confidence": 1,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": "Фиксируем часть"}),
        extraction,
    )

    assert not signals.actionable
    assert signals.position_actions == ()
