import asyncio

from cautious_crypto_bro.storage import (
    IntentStore,
)


def test_global_and_channel_guidance(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "test.sqlite3")

        await store.initialize()

        await store.set_guidance("global rule")

        await store.set_guidance(
            "channel rule",
            channel_id=-1001234567890,
        )

        global_rule, channel_rule = await store.get_guidance(-1001234567890)

        assert global_rule == "global rule"
        assert channel_rule == "channel rule"

        other_global, other_channel = await store.get_guidance(-1009999999999)

        assert other_global == "global rule"
        assert other_channel is None

        await store.set_guidance(
            "updated channel rule",
            channel_id=-1001234567890,
        )

        _, updated_channel = await store.get_guidance(-1001234567890)

        assert updated_channel == "updated channel rule"

    asyncio.run(run())
