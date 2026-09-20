from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class LLMResponseValidationError(ValueError):
    pass


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
