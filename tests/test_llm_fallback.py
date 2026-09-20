import asyncio

import pytest

from cautious_crypto_bro.llm_provider import (
    FallbackLLMProvider,
    LLMProviderFailure,
    LLMRequest,
    LLMResponse,
    NamedLLMProvider,
)


def _request() -> LLMRequest:
    return LLMRequest(
        system_prompt="system",
        user_text="user",
        response_schema_name="result",
        response_schema={
            "type": "object",
        },
        request_label="test/1",
    )


class FakeProvider:
    def __init__(
        self,
        *,
        response: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls = 0
        self.closed = False

    async def complete(
        self,
        request: LLMRequest,
    ) -> LLMResponse:
        self.calls += 1

        if self.error is not None:
            raise self.error

        assert self.response is not None

        return LLMResponse(content=self.response)

    async def close(self) -> None:
        self.closed = True


def test_valid_primary_response_never_calls_fallback() -> None:
    async def run() -> None:
        primary = FakeProvider(response=('{"actionable":false}'))
        secondary = FakeProvider(response=('{"actionable":true}'))

        chain = FallbackLLMProvider(
            (
                NamedLLMProvider(
                    "primary",
                    primary,
                ),
                NamedLLMProvider(
                    "secondary",
                    secondary,
                ),
            )
        )

        try:
            result = await chain.complete(_request())
        finally:
            await chain.close()

        assert result.content == ('{"actionable":false}')
        assert primary.calls == 1
        assert secondary.calls == 0
        assert primary.closed
        assert secondary.closed

    asyncio.run(run())


def test_provider_failure_uses_next_provider() -> None:
    async def run() -> None:
        primary = FakeProvider(error=LLMProviderFailure("primary exhausted"))
        secondary = FakeProvider(response='{"ok":true}')

        chain = FallbackLLMProvider(
            (
                NamedLLMProvider(
                    "primary",
                    primary,
                ),
                NamedLLMProvider(
                    "secondary",
                    secondary,
                ),
            )
        )

        try:
            result = await chain.complete(_request())
        finally:
            await chain.close()

        assert result.content == ('{"ok":true}')
        assert primary.calls == 1
        assert secondary.calls == 1

    asyncio.run(run())


def test_non_fallback_error_stops_chain() -> None:
    async def run() -> None:
        primary = FakeProvider(error=RuntimeError("invalid credentials"))
        secondary = FakeProvider(response='{"ok":true}')

        chain = FallbackLLMProvider(
            (
                NamedLLMProvider(
                    "primary",
                    primary,
                ),
                NamedLLMProvider(
                    "secondary",
                    secondary,
                ),
            )
        )

        try:
            with pytest.raises(
                RuntimeError,
                match="invalid credentials",
            ):
                await chain.complete(_request())
        finally:
            await chain.close()

        assert primary.calls == 1
        assert secondary.calls == 0

    asyncio.run(run())


def test_all_provider_failures_surface_last_failure() -> None:
    async def run() -> None:
        primary = FakeProvider(error=LLMProviderFailure("primary failed"))
        secondary = FakeProvider(error=LLMProviderFailure("secondary failed"))

        chain = FallbackLLMProvider(
            (
                NamedLLMProvider(
                    "primary",
                    primary,
                ),
                NamedLLMProvider(
                    "secondary",
                    secondary,
                ),
            )
        )

        try:
            with pytest.raises(
                LLMProviderFailure,
                match="secondary failed",
            ):
                await chain.complete(_request())
        finally:
            await chain.close()

        assert primary.calls == 1
        assert secondary.calls == 1

    asyncio.run(run())
