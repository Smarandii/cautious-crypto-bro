from datetime import UTC, datetime

from cautious_crypto_bro.domain import (
    IncomingPost,
    IntentExtraction,
    SourceMessage,
)
from cautious_crypto_bro.openrouter import (
    IntentExtractor,
    OpenRouterProvider,
    _signals_from_extraction,
)


def _openrouter_extractor(
    *,
    api_key: str,
    model: str,
    base_url: str,
    inference_timeout_seconds: float = 45,
    max_attempts: int = 2,
    provider_cooldown_store=None,
    persist_provider_cooldowns: bool = True,
    provider_cooldown_seconds: int = (12 * 60 * 60),
    evaluation_cache=None,
    evaluation_cache_seconds: int = (6 * 60 * 60),
) -> IntentExtractor:
    provider = OpenRouterProvider(
        api_key=api_key,
        model=model,
        base_url=base_url,
        inference_timeout_seconds=(inference_timeout_seconds),
        max_attempts=max_attempts,
        provider_cooldown_store=(provider_cooldown_store),
        persist_provider_cooldowns=(persist_provider_cooldowns),
        provider_cooldown_seconds=(provider_cooldown_seconds),
    )

    return IntentExtractor(
        provider=provider,
        cache_identity=model,
        evaluation_cache=evaluation_cache,
        evaluation_cache_seconds=(evaluation_cache_seconds),
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


def test_intent_extractor_accepts_generic_provider() -> None:
    import asyncio
    import json

    from cautious_crypto_bro.llm_provider import (
        LLMResponse,
    )

    class FakeProvider:
        def __init__(self) -> None:
            self.request = None
            self.closed = False

        async def complete(self, request):
            self.request = request

            content = json.dumps(
                {
                    "actionable": False,
                    "reason": "commentary",
                    "intents": [],
                }
            )

            if request.response_validator is not None:
                request.response_validator(content)

            return LLMResponse(content=content)

        async def close(self) -> None:
            self.closed = True

    async def run() -> None:
        provider = FakeProvider()

        extractor = IntentExtractor(
            provider=provider,
            cache_identity="generic/test-model",
        )

        try:
            result = await extractor.extract(IncomingPost(source=source()))
        finally:
            await extractor.close()

        assert not result.actionable

        request = provider.request
        assert request is not None
        assert request.response_schema_name == ("trading_intent_extraction")
        assert "LONG BTCUSDT" in request.user_text
        assert request.images == ()
        assert provider.closed

    asyncio.run(run())


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

    async def run(
        persist_provider_cooldowns: bool,
    ) -> None:
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

        extractor = _openrouter_extractor(
            api_key="test",
            model="test/model",
            base_url=("https://openrouter.test"),
            inference_timeout_seconds=5,
            max_attempts=2,
            provider_cooldown_store=store,
            persist_provider_cooldowns=persist_provider_cooldowns,
            provider_cooldown_seconds=(12 * 60 * 60),
        )

        await extractor._provider._client.aclose()

        extractor._provider._client = httpx.AsyncClient(
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

        if persist_provider_cooldowns:
            assert store.recorded == [
                (
                    "venice",
                    store.recorded[0][1],
                    12 * 60 * 60,
                )
            ]
        else:
            assert store.recorded == []

    asyncio.run(run(True))
    asyncio.run(run(False))


def test_invalid_structured_output_is_retried() -> None:
    import asyncio
    import json

    import httpx

    async def run() -> None:
        store = FakeProviderCooldownStore()
        payloads = []

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            payloads.append(json.loads(request.content))

            if len(payloads) == 1:
                return httpx.Response(
                    200,
                    json={
                        "provider": "Venice",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "actionable": False,
                                        }
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
                                "content": json.dumps(
                                    {
                                        "actionable": False,
                                        "reason": "commentary",
                                        "intents": [],
                                    }
                                ),
                            },
                        }
                    ],
                },
            )

        extractor = _openrouter_extractor(
            api_key="test",
            model="test/model",
            base_url="https://openrouter.test",
            inference_timeout_seconds=5,
            max_attempts=2,
            provider_cooldown_store=store,
        )

        await extractor._provider._client.aclose()

        extractor._provider._client = httpx.AsyncClient(
            base_url="https://openrouter.test",
            transport=httpx.MockTransport(handler),
        )

        try:
            result = await extractor.extract(IncomingPost(source=source()))
        finally:
            await extractor.close()

        assert not result.actionable
        assert len(payloads) == 2

        first_ignore = set(payloads[0]["provider"]["ignore"])
        second_ignore = set(payloads[1]["provider"]["ignore"])

        assert "venice" not in first_ignore
        assert "venice" in second_ignore

        # Invalid structured output excludes this backend
        # for the current retry only.
        assert store.recorded == []

    asyncio.run(run())


class FakeEvaluationCache:
    def __init__(self) -> None:
        self.values = {}
        self.writes = []

    async def get_evaluation(
        self,
        fingerprint,
    ):
        return self.values.get(fingerprint)

    async def cache_evaluation(
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

        extractor = _openrouter_extractor(
            api_key="test",
            model="test/model",
            base_url=("https://openrouter.test"),
            inference_timeout_seconds=5,
            max_attempts=1,
            evaluation_cache=cache,
            evaluation_cache_seconds=21600,
        )

        await extractor._provider._client.aclose()

        extractor._provider._client = httpx.AsyncClient(
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

        extractor = _openrouter_extractor(
            api_key="test",
            model="test/model",
            base_url=("https://openrouter.test"),
            inference_timeout_seconds=5,
            max_attempts=1,
            evaluation_cache=cache,
        )

        await extractor._provider._client.aclose()

        extractor._provider._client = httpx.AsyncClient(
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

        extractor = _openrouter_extractor(
            api_key="test",
            model="test/model",
            base_url="https://openrouter.test",
            inference_timeout_seconds=5,
            max_attempts=1,
        )

        await extractor._provider._client.aclose()

        extractor._provider._client = httpx.AsyncClient(
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

        extractor = _openrouter_extractor(
            api_key="test",
            model="test/model",
            base_url="https://openrouter.test",
            inference_timeout_seconds=5,
            max_attempts=1,
        )

        await extractor._provider._client.aclose()

        extractor._provider._client = httpx.AsyncClient(
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


def _audit_position_action_extraction(
    *,
    symbol: str,
    action: str,
    evidence_text: str | None,
    expected_side: str = "LONG",
):
    from cautious_crypto_bro.domain import (
        IntentExtraction,
    )

    return IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Audit regression case",
            "intents": [],
            "position_actions": [
                {
                    "symbol": symbol,
                    "action": action,
                    "close_pct": None,
                    "expected_side": expected_side,
                    "evidence_text": evidence_text,
                    "summary": "Audit action",
                    "confidence": 1,
                }
            ],
        }
    )


def test_current_caption_authorizes_close_when_model_omits_evidence() -> None:
    from cautious_crypto_bro.domain import (
        PositionActionType,
    )
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = _audit_position_action_extraction(
        symbol="LSKUSDT",
        action="CLOSE",
        evidence_text=None,
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": ("Моментально, закрываем 🙂‍↕️")}),
        extraction,
    )

    assert len(signals.position_actions) == 1

    assert signals.position_actions[0].action is PositionActionType.CLOSE


def test_negated_close_is_not_authorized() -> None:
    from cautious_crypto_bro.openrouter import (
        _signals_from_extraction,
    )

    extraction = _audit_position_action_extraction(
        symbol="ARKUSDT",
        action="CLOSE",
        evidence_text=None,
    )

    signals = _signals_from_extraction(
        source().model_copy(update={"text": ("Пока не закрываем позицию")}),
        extraction,
    )

    assert not signals.actionable
    assert signals.position_actions == ()


def test_take_profit_ordinal_ignores_movement_percentage() -> None:
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
            "reason": "Third take profit",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "AKEUSDT",
                    "action": "REDUCE",
                    "close_pct": 33.33,
                    "expected_side": "LONG",
                    "evidence_text": "Фиксируем 3 тейк",
                    "summary": "Third take profit",
                    "confidence": 0.8,
                }
            ],
        }
    )

    signals = _signals_from_extraction(
        source().model_copy(
            update={
                "text": ("$AKE\n\nФиксируем 3 тейк\n\n4.5% чистого движения 🔥🔥🔥")
            }
        ),
        extraction,
    )

    assert len(signals.position_actions) == 1

    action = signals.position_actions[0]

    assert action.action is PositionActionType.REDUCE
    assert action.close_pct == 50


def test_russian_explicit_reduce_percentage_is_preserved() -> None:
    item = source().model_copy(
        update={"text": "Закрываем часть BTC, 25% позиции."}
    )
    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "reduce",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "BTCUSDT",
                    "action": "REDUCE",
                    "close_pct": 25,
                    "evidence_text": "Закрываем часть BTC, 25% позиции.",
                    "summary": "Reduce BTC",
                    "confidence": 1,
                }
            ],
        }
    )

    result = _signals_from_extraction(item, extraction)

    assert len(result.position_actions) == 1
    assert result.position_actions[0].close_pct == 25


def test_model_excerpt_cannot_bypass_negated_source_clause() -> None:
    item = source().model_copy(update={"text": "Do not close BTC."})
    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "close",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "BTCUSDT",
                    "action": "CLOSE",
                    "evidence_text": "close BTC",
                    "summary": "Close BTC",
                    "confidence": 1,
                }
            ],
        }
    )

    result = _signals_from_extraction(item, extraction)

    assert result.position_actions == ()


def test_unrelated_negation_does_not_block_other_symbol_close() -> None:
    item = source().model_copy(
        update={"text": "Do not close ETH. Close BTC now."}
    )
    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "close",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "BTCUSDT",
                    "action": "CLOSE",
                    "evidence_text": "Close BTC now",
                    "summary": "Close BTC",
                    "confidence": 1,
                }
            ],
        }
    )

    result = _signals_from_extraction(item, extraction)

    assert len(result.position_actions) == 1
    assert result.position_actions[0].symbol == "BTCUSDT"


def test_reduce_percentage_is_read_from_action_clause_not_other_sentence() -> None:
    item = source().model_copy(
        update={
            "text": (
                "ETH is up 25% today. "
                "Закрываем часть BTC, 30% позиции."
            )
        }
    )
    extraction = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "reduce",
            "intents": [],
            "position_actions": [
                {
                    "symbol": "BTCUSDT",
                    "action": "REDUCE",
                    "close_pct": 30,
                    "evidence_text": "Закрываем часть BTC",
                    "summary": "Reduce BTC",
                    "confidence": 1,
                }
            ],
        }
    )

    result = _signals_from_extraction(item, extraction)

    assert len(result.position_actions) == 1
    assert result.position_actions[0].close_pct == 30
