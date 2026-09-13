from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

from redis.asyncio import Redis

from cautious_crypto_bro.config import (
    get_settings,
)
from cautious_crypto_bro.runtime_store import (
    RedisRuntimeStore,
)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stress-test Redis-backed "
            "OpenRouter provider cooldowns "
            "without making OpenRouter requests."
        )
    )

    parser.add_argument(
        "--providers",
        type=int,
        default=250,
    )

    parser.add_argument(
        "--concurrency",
        type=int,
        default=32,
    )

    args = parser.parse_args()

    if args.providers <= 0:
        parser.error(
            "--providers must be positive"
        )

    if args.concurrency <= 0:
        parser.error(
            "--concurrency must be positive"
        )

    settings = get_settings()

    run_id = uuid4().hex[:10]
    provider_prefix = (
        f"stress-{run_id}-"
    )

    store = RedisRuntimeStore(
        settings.redis_url,
        max_connections=(
            settings.redis_max_connections
        ),
        pool_timeout_seconds=(
            settings.redis_pool_timeout_seconds
        ),
    )

    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )

    await store.initialize()

    print(
        f"Creating {args.providers} "
        "provider cooldowns..."
    )

    providers = tuple(
        f"{provider_prefix}{index}"
        for index in range(
            args.providers
        )
    )

    try:
        semaphore = asyncio.Semaphore(
            args.concurrency
        )

        async def write_cooldown(
            provider: str,
        ) -> None:
            async with semaphore:
                await (
                    store
                    .cooldown_openrouter_provider(
                        provider,
                        "stress test",
                        60,
                    )
                )

        await asyncio.gather(
            *(
                write_cooldown(
                    provider
                )
                for provider in providers
            )
        )

        active = set(
            await store
            .get_openrouter_provider_cooldowns()
        )

        missing = (
            set(providers)
            - active
        )

        if missing:
            raise RuntimeError(
                f"{len(missing)} cooldown(s) "
                "missing after concurrent writes"
            )

        print(
            "Concurrent writes: PASS"
        )

        sample = providers[0]

        sample_key = (
            "cautious-crypto-bro:"
            "openrouter:"
            "provider-cooldown:"
            f"{sample}"
        )

        ttl = await redis.ttl(
            sample_key
        )

        if not (
            0 < ttl <= 60
        ):
            raise RuntimeError(
                f"Unexpected TTL: {ttl}"
            )

        print(
            f"TTL: PASS ({ttl}s)"
        )

        await store.close()

        # New connection simulates another
        # process/container using the same Redis.
        store = RedisRuntimeStore(
            settings.redis_url,
            max_connections=(
                settings.redis_max_connections
            ),
            pool_timeout_seconds=(
                settings.redis_pool_timeout_seconds
            ),
        )

        await store.initialize()

        after_reconnect = set(
            await store
            .get_openrouter_provider_cooldowns()
        )

        if not (
            set(providers)
            <= after_reconnect
        ):
            raise RuntimeError(
                "Cooldowns did not survive "
                "client reconnect"
            )

        print(
            "Cross-client persistence: PASS"
        )

        expiring_provider = (
            f"{provider_prefix}expiry"
        )

        await store.cooldown_openrouter_provider(
            expiring_provider,
            "expiry test",
            1,
        )

        await asyncio.sleep(
            1.2
        )

        after_expiry = set(
            await store
            .get_openrouter_provider_cooldowns()
        )

        if (
            expiring_provider
            in after_expiry
        ):
            raise RuntimeError(
                "Expired cooldown is still active"
            )

        print(
            "Automatic TTL expiry: PASS"
        )

        # Repeated writes to the same provider
        # should atomically refresh its TTL.
        refresh_provider = (
            f"{provider_prefix}refresh"
        )

        await store.cooldown_openrouter_provider(
            refresh_provider,
            "first",
            5,
        )

        await store.cooldown_openrouter_provider(
            refresh_provider,
            "second",
            90,
        )

        refresh_key = (
            "cautious-crypto-bro:"
            "openrouter:"
            "provider-cooldown:"
            f"{refresh_provider}"
        )

        refreshed_ttl = await redis.ttl(
            refresh_key
        )

        if not (
            80 <= refreshed_ttl <= 90
        ):
            raise RuntimeError(
                "Cooldown TTL was not refreshed: "
                f"{refreshed_ttl}"
            )

        print(
            "Concurrent-state refresh: PASS"
        )

        print()
        print(
            "ALL COOLDOWN STRESS CHECKS PASSED"
        )

        return 0

    finally:
        pattern = (
            "cautious-crypto-bro:"
            "openrouter:"
            "provider-cooldown:"
            f"{provider_prefix}*"
        )

        keys = [
            key
            async for key
            in redis.scan_iter(
                match=pattern,
                count=500,
            )
        ]

        if keys:
            await redis.delete(
                *keys
            )

        await store.close()
        await redis.aclose()


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            main()
        )
    )
