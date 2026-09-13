import asyncio
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from types import SimpleNamespace

import cautious_crypto_bro.telegram_source as telegram_source


class FakeMessage:
    def __init__(
        self,
        *,
        message_id: int,
        published_at: datetime,
        text: str = "",
        grouped_id: int | None = None,
        image: bytes | None = None,
    ) -> None:
        self.id = message_id
        self.date = published_at
        self.message = text
        self.chat_id = -1001234567890
        self.chat = None
        self.grouped_id = grouped_id

        self.photo = object() if image is not None else None

        self.file = None
        self._image = image

    async def download_media(
        self,
        *,
        file,
    ):
        assert file is bytes
        return self._image


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

    def on(
        self,
        event,
    ):
        def register(handler):
            self.handlers.append(handler)
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

    async def disconnect(
        self,
    ) -> None:
        pass


def test_album_becomes_one_post_with_all_images() -> None:
    async def run() -> None:
        now = datetime.now(UTC)

        chat = SimpleNamespace(
            title="Trader",
            username=None,
        )

        post = await telegram_source.telegram_messages_to_post(
            (
                FakeMessage(
                    message_id=101,
                    published_at=now,
                    text=("Некоторые ждут биткоин по 30к.😫"),
                    grouped_id=77,
                    image=b"first",
                ),
                FakeMessage(
                    message_id=102,
                    published_at=(now + timedelta(seconds=1)),
                    grouped_id=77,
                    image=b"second",
                ),
            ),
            chat=chat,
        )

        assert post is not None

        assert post.source.message_id == 101

        assert post.source.text == ("Некоторые ждут биткоин по 30к.😫")

        assert len(post.images) == 2

        assert [image.data for image in post.images] == [
            b"first",
            b"second",
        ]

    asyncio.run(run())


def test_startup_lookback_processes_recent_messages_oldest_first(
    monkeypatch,
) -> None:
    async def run() -> None:
        now = datetime.now(UTC)

        fake_client = FakeTelegramClient(
            [
                FakeMessage(
                    message_id=3,
                    published_at=(now - timedelta(minutes=30)),
                    text="newest",
                ),
                FakeMessage(
                    message_id=2,
                    published_at=(now - timedelta(hours=2)),
                    text="older",
                ),
                FakeMessage(
                    message_id=1,
                    published_at=(now - timedelta(hours=6)),
                    text="outside lookback",
                ),
            ]
        )

        monkeypatch.setattr(
            telegram_source,
            "TelegramClient",
            lambda *args, **kwargs: fake_client,
        )

        processed: list[int] = []

        async def on_message(
            post,
        ) -> None:
            processed.append(post.source.message_id)

        source = telegram_source.TelegramSource(
            api_id=1,
            api_hash="hash",
            session_name="session",
            channels=[-1001234567890],
            on_message=on_message,
            startup_lookback_hours=5,
        )

        await source.start()

        assert processed == [
            2,
            3,
        ]

    asyncio.run(run())


def test_startup_lookback_groups_album_once(
    monkeypatch,
) -> None:
    async def run() -> None:
        now = datetime.now(UTC)

        # iter_messages is newest-first.
        fake_client = FakeTelegramClient(
            [
                FakeMessage(
                    message_id=12,
                    published_at=(now - timedelta(minutes=10)),
                    grouped_id=500,
                    image=b"second",
                ),
                FakeMessage(
                    message_id=11,
                    published_at=(
                        now
                        - timedelta(
                            minutes=10,
                            seconds=1,
                        )
                    ),
                    text="album caption",
                    grouped_id=500,
                    image=b"first",
                ),
            ]
        )

        monkeypatch.setattr(
            telegram_source,
            "TelegramClient",
            lambda *args, **kwargs: fake_client,
        )

        processed = []

        async def on_message(
            post,
        ) -> None:
            processed.append(post)

        source = telegram_source.TelegramSource(
            api_id=1,
            api_hash="hash",
            session_name="session",
            channels=[-1001234567890],
            on_message=on_message,
            startup_lookback_hours=5,
        )

        await source.start()

        assert len(processed) == 1

        post = processed[0]

        assert post.source.message_id == 11

        assert post.source.text == "album caption"

        assert len(post.images) == 2

    asyncio.run(run())


def test_startup_lookback_can_be_disabled(
    monkeypatch,
) -> None:
    async def run() -> None:
        now = datetime.now(UTC)

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
            lambda *args, **kwargs: fake_client,
        )

        processed: list[int] = []

        async def on_message(
            post,
        ) -> None:
            processed.append(post.source.message_id)

        source = telegram_source.TelegramSource(
            api_id=1,
            api_hash="hash",
            session_name="session",
            channels=[-1001234567890],
            on_message=on_message,
            startup_lookback_hours=0,
        )

        await source.start()

        assert processed == []

    asyncio.run(run())
