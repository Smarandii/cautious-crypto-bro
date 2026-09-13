import asyncio
from datetime import (
    datetime,
    timezone,
)

from cautious_crypto_bro.domain import (
    IncomingPost,
    SourceMessage,
)
from cautious_crypto_bro.service import (
    SignalService,
)


class DuplicateStore:
    async def save_source(
        self,
        source,
    ) -> bool:
        return False


class MustNotBeCalled:
    def __getattr__(
        self,
        name,
    ):
        raise AssertionError(
            f"{name} must not be called "
            "for a duplicate source message"
        )


def test_duplicate_source_stops_before_processing() -> None:
    async def run() -> None:
        now = datetime.now(
            timezone.utc
        )

        post = IncomingPost(
            source=SourceMessage(
                channel_id=-1001234567890,
                channel_title="Trader",
                channel_username=None,
                message_id=123,
                published_at=now,
                received_at=now,
                text="signal",
            )
        )

        never = MustNotBeCalled()

        service = SignalService(
            store=DuplicateStore(),
            extractor=never,
            planner=never,
            executor=never,
            approval_bot=never,
        )

        await service.on_message(
            post
        )

    asyncio.run(run())
