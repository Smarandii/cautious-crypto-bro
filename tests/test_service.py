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


def post() -> IncomingPost:
    now = datetime.now(
        timezone.utc
    )

    return IncomingPost(
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


class DuplicateStore:
    async def claim_source(
        self,
        source,
        *,
        lease_seconds,
    ):
        return None


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
        never = MustNotBeCalled()

        service = SignalService(
            store=DuplicateStore(),
            extractor=never,
            planner=never,
            executor=never,
            approval_bot=never,
        )

        await service.on_message(
            post()
        )

    asyncio.run(
        run()
    )


class RetryStore:
    def __init__(self) -> None:
        self.status = "READY"
        self.claim = None
        self.failed = 0
        self.completed = 0

    async def claim_source(
        self,
        source,
        *,
        lease_seconds,
    ):
        if self.status not in {
            "READY",
            "FAILED",
        }:
            return None

        self.status = "PROCESSING"
        self.claim = (
            f"claim-{self.failed}"
        )
        return self.claim

    async def get_guidance(
        self,
        channel_id,
    ):
        return None, None

    async def mark_source_failed(
        self,
        source,
        claim_token,
        error,
    ):
        assert claim_token == self.claim
        self.status = "FAILED"
        self.failed += 1
        return True

    async def mark_source_completed(
        self,
        source,
        claim_token,
    ):
        assert claim_token == self.claim
        self.status = "COMPLETED"
        self.completed += 1
        return True


class FailOnceExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(
        self,
        post,
        **kwargs,
    ):
        self.calls += 1

        if self.calls == 1:
            raise RuntimeError(
                "temporary failure"
            )

        return None


def test_extraction_failure_is_retryable() -> None:
    async def run() -> None:
        store = RetryStore()
        extractor = FailOnceExtractor()
        never = MustNotBeCalled()

        service = SignalService(
            store=store,
            extractor=extractor,
            planner=never,
            executor=never,
            approval_bot=never,
        )

        source_post = post()

        await service.on_message(
            source_post
        )

        assert store.status == "FAILED"
        assert store.failed == 1

        await service.on_message(
            source_post
        )

        assert extractor.calls == 2
        assert store.status == "COMPLETED"
        assert store.completed == 1

    asyncio.run(
        run()
    )
