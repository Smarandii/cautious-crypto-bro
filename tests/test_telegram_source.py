import asyncio
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from types import SimpleNamespace

import cautious_crypto_bro.telegram_source as telegram_source


class FakeMessage:
    def __init__(
        self,
        *,
        message_id: int,
        published_at: datetime,
        text: str,
    ) -> None:
        self.id = message_id
        self.date = published_at
        self.message = text
        self.chat_id = -1001234567890
        self.chat = None
        self.photo = None
        self.file = None


class FakeTelegramClient:
    def __init__(
        self,
        messages: list[FakeMessage],
    ) -> None:
        self.messages = messages
        self.entity = SimpleNamespace(
            title="Test trader",
            username=None,
        )
        self.handlers = []

    async def start(self) -> None:
        pass

    async def get_entity(
        self,
        channel,
    ):
        return self.entity

    def on(self, event):
        def register(handler):
            self.handlers.append(
                handler
            )
            return handler

        return register

    def iter_messages(
        self,
        entity,
    ):
        async def iterator():
            for message in self.messages:
                yield message

        return iterator()

    async def disconnect(self) -> None:
        pass


def test_startup_lookback_processes_recent_messages_oldest_first(
    monkeypatch,
) -> None:
    async def run() -> None:
        now = datetime.now(
            timezone.utc
        )

        fake_client = FakeTelegramClient(
            [
                FakeMessage(
                    message_id=3,
                    published_at=(
                        now
                        - timedelta(
                            minutes=30
                        )
                    ),
                    text="newest",
                ),
                FakeMessage(
                    message_id=2,
                    published_at=(
                        now
                        - timedelta(
                            hours=2
                        )
                    ),
                    text="older",
                ),
                FakeMessage(
                    message_id=1,
                    published_at=(
                        now
                        - timedelta(
                            hours=6
                        )
                    ),
                    text="outside lookback",
                ),
            ]
        )

        monkeypatch.setattr(
            telegram_source,
            "TelegramClient",
            lambda *args, **kwargs: (
                fake_client
            ),
        )

        processed: list[int] = []

        async def on_message(
            post,
        ) -> None:
            processed.append(
                post.source.message_id
            )

        source = (
            telegram_source.TelegramSource(
                api_id=1,
                api_hash="hash",
                session_name="session",
                channels=[
                    -1001234567890
                ],
                on_message=on_message,
                startup_lookback_hours=5,
            )
        )

        await source.start()

        assert processed == [
            2,
            3,
        ]

    asyncio.run(run())


def test_startup_lookback_can_be_disabled(
    monkeypatch,
) -> None:
    async def run() -> None:
        now = datetime.now(
            timezone.utc
        )

        fake_client = FakeTelegramClient(
            [
                FakeMessage(
                    message_id=1,
                    published_at=now,
                    text="recent",
                ),
            ]
        )

        monkeypatch.setattr(
            telegram_source,
            "TelegramClient",
            lambda *args, **kwargs: (
                fake_client
            ),
        )

        processed: list[int] = []

        async def on_message(
            post,
        ) -> None:
            processed.append(
                post.source.message_id
            )

        source = (
            telegram_source.TelegramSource(
                api_id=1,
                api_hash="hash",
                session_name="session",
                channels=[
                    -1001234567890
                ],
                on_message=on_message,
                startup_lookback_hours=0,
            )
        )

        await source.start()

        assert processed == []

    asyncio.run(run())
