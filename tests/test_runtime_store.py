import asyncio

from fakeredis.aioredis import (
    FakeRedis,
)

from cautious_crypto_bro.runtime_store import (
    RedisRuntimeStore,
)


def test_provider_cooldown_uses_redis_ttl() -> None:
    async def run() -> None:
        redis = FakeRedis(
            decode_responses=True
        )

        store = RedisRuntimeStore(
            "redis://unused",
            key_prefix="test",
            client=redis,
        )

        await store.initialize()

        await (
            store
            .cooldown_openrouter_provider(
                "Venice",
                "truncated response",
                3600,
            )
        )

        assert (
            await store
            .get_openrouter_provider_cooldowns()
        ) == (
            "venice",
        )

        key = (
            "test:openrouter:"
            "provider-cooldown:venice"
        )

        ttl = await redis.ttl(
            key
        )

        assert (
            0 < ttl <= 3600
        )

        await redis.delete(
            key
        )

        assert (
            await store
            .get_openrouter_provider_cooldowns()
        ) == ()

        await store.close()

    asyncio.run(
        run()
    )
