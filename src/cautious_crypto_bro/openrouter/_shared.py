from __future__ import annotations

STATIC_IGNORED_PROVIDERS = (
    "nextbit",
    "parasail",
)


class OpenRouterProviderFailure(ValueError):
    def __init__(
        self,
        provider: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.provider = provider
