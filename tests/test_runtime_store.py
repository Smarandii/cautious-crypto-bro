import asyncio
from unittest.mock import AsyncMock

import pytest
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


def test_client_close_failure_still_closes_owned_pool(monkeypatch):
    async def run():
        store = RedisRuntimeStore("redis://unused")
        failure = RuntimeError("client close failed")
        monkeypatch.setattr(store._redis, "aclose", AsyncMock(side_effect=failure))
        close_pool = AsyncMock(wraps=store._pool.aclose)
        monkeypatch.setattr(store._pool, "aclose", close_pool)
        with pytest.raises(RuntimeError) as caught:
            await store.close()
        assert caught.value is failure
        close_pool.assert_awaited_once()

    asyncio.run(run())
