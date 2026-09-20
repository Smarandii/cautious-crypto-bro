import asyncio
from types import SimpleNamespace

import pytest

from cautious_crypto_bro.llm_factory import (
    build_llm_provider,
)
from cautious_crypto_bro.opencode_go import (
    OpenCodeGoProvider,
)
from cautious_crypto_bro.openrouter import (
    OpenRouterProvider,
)


def _settings(provider: str):
    return SimpleNamespace(
        llm_provider=provider,
        llm_evaluation_cache_hours=6,
        openrouter_api_key="or-key",
        openrouter_model="test/openrouter",
        openrouter_base_url=("https://openrouter.test"),
        openrouter_inference_timeout_seconds=5,
        openrouter_inference_max_attempts=1,
        openrouter_provider_cooldown_hours=12,
        opencode_go_api_key="go-key",
        opencode_go_model="gpt-5.6-luna",
        opencode_go_base_url=("https://opencode.test"),
        opencode_go_inference_timeout_seconds=5,
        opencode_go_inference_max_attempts=1,
    )


def test_factory_builds_openrouter_by_default_shape() -> None:
    async def run() -> None:
        provider, identity = build_llm_provider(_settings("openrouter"))

        try:
            assert isinstance(
                provider,
                OpenRouterProvider,
            )
            assert identity == "test/openrouter"
        finally:
            await provider.close()

    asyncio.run(run())


def test_factory_builds_opencode_go() -> None:
    async def run() -> None:
        provider, identity = build_llm_provider(_settings("opencode_go"))

        try:
            assert isinstance(
                provider,
                OpenCodeGoProvider,
            )
            assert identity == ("opencode_go:gpt-5.6-luna")
        finally:
            await provider.close()

    asyncio.run(run())


def test_factory_requires_selected_provider_key() -> None:
    settings = _settings("opencode_go")
    settings.opencode_go_api_key = None

    with pytest.raises(
        ValueError,
        match="OPENCODE_GO_API_KEY",
    ):
        build_llm_provider(settings)
