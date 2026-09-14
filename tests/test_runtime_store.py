import asyncio

from fakeredis.aioredis import (
    FakeRedis,
)

from cautious_crypto_bro.runtime_store import (
    RedisRuntimeStore,
)


def test_provider_cooldown_uses_redis_ttl() -> None:
    async def run() -> None:
        redis = FakeRedis(decode_responses=True)

        store = RedisRuntimeStore(
            "redis://unused",
            key_prefix="test",
            client=redis,
        )

        await store.initialize()

        await store.cooldown_openrouter_provider(
            "Venice",
            "truncated response",
            3600,
        )

        assert (await store.get_openrouter_provider_cooldowns()) == ("venice",)

        key = "test:openrouter:provider-cooldown:venice"

        ttl = await redis.ttl(key)

        assert 0 < ttl <= 3600

        await redis.delete(key)

        assert (await store.get_openrouter_provider_cooldowns()) == ()

        await store.close()

    asyncio.run(run())


def test_openrouter_evaluation_cache_uses_ttl() -> None:
    async def run() -> None:
        redis = FakeRedis(decode_responses=True)

        store = RedisRuntimeStore(
            "redis://unused",
            key_prefix="test",
            client=redis,
        )

        await store.initialize()

        await store.cache_openrouter_evaluation(
            "abc123",
            '{"actionable":false}',
            3600,
        )

        assert (
            await store.get_openrouter_evaluation("abc123")
        ) == '{"actionable":false}'

        ttl = await redis.ttl("test:openrouter:evaluation:abc123")

        assert 0 < ttl <= 3600

        await store.close()

    asyncio.run(run())


def test_read_only_provider_cooldown_store_does_not_write() -> None:
    import asyncio

    from cautious_crypto_bro.runtime_store import (
        ReadOnlyProviderCooldownStore,
    )

    class Store:
        def __init__(self) -> None:
            self.writes = []

        async def get_openrouter_provider_cooldowns(
            self,
        ):
            return (
                "venice",
                "darkbloom",
            )

        async def cooldown_openrouter_provider(
            self,
            provider,
            reason,
            duration_seconds,
        ):
            self.writes.append(
                (
                    provider,
                    reason,
                    duration_seconds,
                )
            )

    async def run() -> None:
        store = Store()

        wrapper = ReadOnlyProviderCooldownStore(store)

        assert (await wrapper.get_openrouter_provider_cooldowns()) == (
            "venice",
            "darkbloom",
        )

        await wrapper.cooldown_openrouter_provider(
            "siliconflow",
            "failure",
            3600,
        )

        assert store.writes == []

    asyncio.run(run())
