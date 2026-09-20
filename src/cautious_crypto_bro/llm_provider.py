from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class LLMResponseValidationError(ValueError):
    pass


class LLMProviderFailure(RuntimeError):
    """Provider exhausted a failure eligible for fallback."""


LLMResponseValidator = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class LLMImage:
    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system_prompt: str
    user_text: str
    response_schema_name: str
    response_schema: dict[str, object]
    images: tuple[LLMImage, ...] = ()
    response_validator: LLMResponseValidator | None = None
    temperature: int = 0
    max_tokens: int = 1536
    request_label: str | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str


class LLMProvider(Protocol):
    async def complete(
        self,
        request: LLMRequest,
    ) -> LLMResponse: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class NamedLLMProvider:
    name: str
    provider: LLMProvider


class FallbackLLMProvider:
    def __init__(
        self,
        providers: tuple[NamedLLMProvider, ...],
    ) -> None:
        if not providers:
            raise ValueError("At least one LLM provider is required")

        names = tuple(item.name.strip() for item in providers)

        if any(not name for name in names):
            raise ValueError("LLM provider names must not be empty")

        if len(set(names)) != len(names):
            raise ValueError("LLM provider names must be unique")

        self._providers = providers

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self._providers)

    async def complete(
        self,
        request: LLMRequest,
    ) -> LLMResponse:
        last_failure: LLMProviderFailure | None = None

        for index, item in enumerate(self._providers):
            try:
                return await item.provider.complete(request)
            except LLMProviderFailure as exc:
                last_failure = exc

                if index + 1 >= len(self._providers):
                    raise

                next_provider = self._providers[index + 1]

                logger.warning(
                    "LLM provider %s failed for %s: %s; falling back to %s",
                    item.name,
                    (request.request_label or "request"),
                    exc,
                    next_provider.name,
                )

        if last_failure is not None:
            raise last_failure

        raise RuntimeError("LLM provider chain produced no response")

    async def close(self) -> None:
        first_error: Exception | None = None

        for item in self._providers:
            try:
                await item.provider.close()
            except Exception as exc:
                logger.exception(
                    "Failed to close LLM provider %s",
                    item.name,
                )

                if first_error is None:
                    first_error = exc

        if first_error is not None:
            raise first_error
