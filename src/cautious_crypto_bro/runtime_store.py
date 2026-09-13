from __future__ import annotations

import json
from typing import (
    Protocol,
)

from redis.asyncio import Redis


class ProviderCooldownStore(
    Protocol
):
    async def get_openrouter_provider_cooldowns(
        self,
    ) -> tuple[str, ...]:
        ...

    async def cooldown_openrouter_provider(
        self,
        provider: str,
        reason: str,
        duration_seconds: int,
    ) -> None:
        ...


class RedisRuntimeStore:
    def __init__(
        self,
        redis_url: str,
        *,
        key_prefix: str = (
            "cautious-crypto-bro"
        ),
        client: Redis | None = None,
    ) -> None:
        key_prefix = (
            key_prefix.strip()
        )

        if not key_prefix:
            raise ValueError(
                "Redis key prefix "
                "must not be empty"
            )

        self._key_prefix = (
            key_prefix.rstrip(":")
        )

        self._redis = (
            client
            if client is not None
            else Redis.from_url(
                redis_url,
                decode_responses=True,
            )
        )

    async def initialize(
        self,
    ) -> None:
        await self._redis.ping()

    async def close(
        self,
    ) -> None:
        await self._redis.aclose()

    async def get_openrouter_provider_cooldowns(
        self,
    ) -> tuple[str, ...]:
        prefix = (
            self._provider_key_prefix()
        )

        providers: set[str] = set()

        async for key in (
            self._redis.scan_iter(
                match=f"{prefix}*",
                count=100,
            )
        ):
            if isinstance(
                key,
                bytes,
            ):
                key = key.decode(
                    "utf-8"
                )

            if not isinstance(
                key,
                str,
            ):
                continue

            provider = key[
                len(prefix):
            ]

            if provider:
                providers.add(
                    provider
                )

        return tuple(
            sorted(
                providers
            )
        )

    async def cooldown_openrouter_provider(
        self,
        provider: str,
        reason: str,
        duration_seconds: int,
    ) -> None:
        provider = (
            self._normalize_provider(
                provider
            )
        )

        if duration_seconds <= 0:
            raise ValueError(
                "Provider cooldown "
                "must be positive"
            )

        payload = json.dumps(
            {
                "provider": provider,
                "reason": (
                    reason.strip()
                    or "provider failure"
                ),
            },
            separators=(
                ",",
                ":",
            ),
        )

        await self._redis.set(
            self._provider_key(
                provider
            ),
            payload,
            ex=duration_seconds,
        )

    def _provider_key_prefix(
        self,
    ) -> str:
        return (
            f"{self._key_prefix}:"
            "openrouter:"
            "provider-cooldown:"
        )

    def _provider_key(
        self,
        provider: str,
    ) -> str:
        return (
            self._provider_key_prefix()
            + self._normalize_provider(
                provider
            )
        )

    @staticmethod
    def _normalize_provider(
        provider: str,
    ) -> str:
        provider = (
            provider.strip()
            .casefold()
        )

        if not provider:
            raise ValueError(
                "Provider must not be empty"
            )

        return provider
