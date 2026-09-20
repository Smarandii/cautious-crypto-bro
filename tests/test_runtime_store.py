import asyncio

from fakeredis.aioredis import (
    FakeRedis,
)

from cautious_crypto_bro.runtime_store import (
    RedisRuntimeStore,
)


def test_evaluation_cache_preserves_openrouter_namespace() -> None:
    async def run() -> None:
        redis = FakeRedis(decode_responses=True)

        store = RedisRuntimeStore(
            "redis://unused",
            key_prefix="test",
            client=redis,
        )

        await store.initialize()

        await store.cache_evaluation(
            "abc123",
            '{"actionable":false}',
            3600,
        )

        assert (await store.get_evaluation("abc123")) == '{"actionable":false}'

        ttl = await redis.ttl("test:openrouter:evaluation:abc123")

        assert 0 < ttl <= 3600

        await store.close()

    asyncio.run(run())
