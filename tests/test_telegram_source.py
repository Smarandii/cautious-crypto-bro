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
            id=1234567890,
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

    async def get_messages(
        self,
        entity,
        *,
        limit=None,
    ):
        return list(
            sorted(
                self.messages,
                key=lambda message: message.id,
            )
        )[-limit:]

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
        *,
        min_id=0,
        reverse=False,
        limit=None,
    ):
        async def iterator():
            selected = sorted(
                (message for message in self.messages if message.id > min_id),
                key=lambda message: message.id,
            )

            if limit is not None:
                selected = selected[:limit]

            if not reverse:
                selected = list(reversed(selected))

            for message in selected:
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


def test_catchup_poll_replays_messages_the_live_push_dropped(
    monkeypatch,
) -> None:
    async def run() -> None:
        now = datetime.now(UTC)

        fake_client = FakeTelegramClient(
            [
                FakeMessage(
                    message_id=1,
                    published_at=(now - timedelta(hours=6)),
                    text="live",
                ),
                # Never reached the dispatcher, exactly like the
                # 2026-10-03 posts the audit found missing.
                FakeMessage(
                    message_id=2,
                    published_at=(now - timedelta(minutes=20)),
                    text="dropped",
                ),
                FakeMessage(
                    message_id=3,
                    published_at=(now - timedelta(minutes=10)),
                    text="album head",
                    grouped_id=9,
                    image=b"first",
                ),
                FakeMessage(
                    message_id=4,
                    published_at=(now - timedelta(minutes=9)),
                    grouped_id=9,
                    image=b"second",
                ),
            ]
        )

        monkeypatch.setattr(
            telegram_source,
            "TelegramClient",
            lambda *args, **kwargs: fake_client,
        )

        processed: list[tuple[int, int]] = []

        async def on_message(
            post,
        ) -> None:
            processed.append(
                (
                    post.source.message_id,
                    len(post.images),
                )
            )

        source = telegram_source.TelegramSource(
            api_id=1,
            api_hash="hash",
            session_name="session",
            channels=[-1001234567890],
            on_message=on_message,
            startup_lookback_hours=0,
        )

        # Startup seeds the watermark at the newest message, so the poll
        # starts from "now" and would see nothing new.
        await source.start()

        assert processed == []

        fake_client.messages.append(
            FakeMessage(
                message_id=5,
                published_at=(now - timedelta(minutes=1)),
                text="missed while running",
            )
        )

        await source.poll_once()

        assert processed == [
            (5, 0),
        ]

        # Watermark advanced, so a second poll adds nothing new.
        await source.poll_once()

        assert processed == [
            (5, 0),
        ]

        # Messages the live push dropped are still in history, so a
        # watermark that never advanced replays them oldest first.
        source._watermarks[-1001234567890] = 1

        await source.poll_once()

        assert processed == [
            (5, 0),
            (2, 0),
            (3, 2),
            (5, 0),
        ]

    asyncio.run(run())
