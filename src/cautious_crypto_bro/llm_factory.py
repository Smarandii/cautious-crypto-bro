from __future__ import annotations

from .config import Settings
from .llm_provider import (
    FallbackLLMProvider,
    LLMProvider,
    NamedLLMProvider,
)
from .opencode_go import OpenCodeGoProvider
from .openrouter import (
    IntentExtractor,
    OpenRouterProvider,
)
from .runtime_store import (
    EvaluationCache,
    ProviderCooldownStore,
)


def _required_api_key(
    value: str | None,
    environment_name: str,
) -> str:
    key = (value or "").strip()

    if not key:
        raise ValueError(
            f"{environment_name} is required for the selected LLM provider"
        )

    return key


def _build_provider(
    name: str,
    settings: Settings,
    *,
    provider_cooldown_store: (ProviderCooldownStore | None),
    persist_provider_cooldowns: bool,
) -> tuple[LLMProvider, str]:
    if name == "openrouter":
        provider = OpenRouterProvider(
            api_key=_required_api_key(
                settings.openrouter_api_key,
                "OPENROUTER_API_KEY",
            ),
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            inference_timeout_seconds=(settings.openrouter_inference_timeout_seconds),
            max_attempts=(settings.openrouter_inference_max_attempts),
            provider_cooldown_store=(provider_cooldown_store),
            persist_provider_cooldowns=(persist_provider_cooldowns),
            provider_cooldown_seconds=(
                settings.openrouter_provider_cooldown_hours * 60 * 60
            ),
        )

        # Preserve historical OpenRouter-only cache
        # identity when it is the sole provider.
        return provider, settings.openrouter_model

    if name == "opencode_go":
        provider = OpenCodeGoProvider(
            api_key=_required_api_key(
                settings.opencode_go_api_key,
                "OPENCODE_GO_API_KEY",
            ),
            model=settings.opencode_go_model,
            base_url=settings.opencode_go_base_url,
            inference_timeout_seconds=(settings.opencode_go_inference_timeout_seconds),
            max_attempts=(settings.opencode_go_inference_max_attempts),
        )

        return (
            provider,
            f"opencode_go:{settings.opencode_go_model}",
        )

    raise ValueError(f"Unsupported LLM provider: {name}")


def build_llm_provider(
    settings: Settings,
    *,
    provider_cooldown_store: (ProviderCooldownStore | None) = None,
    persist_provider_cooldowns: bool = True,
) -> tuple[LLMProvider, str]:
    names = tuple(settings.llm_providers)

    if len(set(names)) != len(names):
        raise ValueError("LLM_PROVIDERS must not contain duplicates")

    built = [
        (
            name,
            *_build_provider(
                name,
                settings,
                provider_cooldown_store=(provider_cooldown_store),
                persist_provider_cooldowns=(persist_provider_cooldowns),
            ),
        )
        for name in names
    ]

    if len(built) == 1:
        _, provider, identity = built[0]
        return provider, identity

    provider = FallbackLLMProvider(
        tuple(
            NamedLLMProvider(
                name=name,
                provider=item,
            )
            for name, item, _ in built
        )
    )

    cache_identity = "chain:" + "|".join(identity for _, _, identity in built)

    return provider, cache_identity


def build_intent_extractor(
    settings: Settings,
    *,
    evaluation_cache: EvaluationCache | None = None,
    provider_cooldown_store: (ProviderCooldownStore | None) = None,
    persist_provider_cooldowns: bool = True,
) -> IntentExtractor:
    provider, cache_identity = build_llm_provider(
        settings,
        provider_cooldown_store=(provider_cooldown_store),
        persist_provider_cooldowns=(persist_provider_cooldowns),
    )

    return IntentExtractor(
        provider=provider,
        cache_identity=cache_identity,
        evaluation_cache=evaluation_cache,
        evaluation_cache_seconds=(settings.llm_evaluation_cache_hours * 60 * 60),
    )
