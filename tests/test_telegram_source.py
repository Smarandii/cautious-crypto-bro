import asyncio
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from types import SimpleNamespace

import pytest

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
        ids,
    ):
        return [message for message in self.messages if message.id in ids]

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

            if not reverse:
                selected = list(reversed(selected))

            if limit is not None:
                selected = selected[:limit]

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

        # With lookback disabled, pre-startup messages are outside the window.
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


def _message(message_id, **kwargs):
    kwargs.setdefault("published_at", datetime.now(UTC) - timedelta(minutes=1))
    return FakeMessage(message_id=message_id, **kwargs)


def _source(monkeypatch, client, processed, *, lookback=0):
    monkeypatch.setattr(telegram_source, "TelegramClient", lambda *a, **k: client)

    async def receive(post):
        processed.append((post.source.message_id, len(post.images)))

    return telegram_source.TelegramSource(
        api_id=1,
        api_hash="test",
        session_name="test",
        channels=[-1001234567890],
        on_message=receive,
        startup_lookback_hours=lookback,
    )


def test_live_success_does_not_skip_earlier_missing_message(monkeypatch):
    async def run():
        client = FakeTelegramClient([_message(1, text="baseline")])
        seen = []
        source = _source(monkeypatch, client, seen)
        await source.start()
        client.messages += [
            _message(2, text="missed signal"),
            _message(3, text="live commentary"),
        ]
        client.messages[-1].chat = client.entity
        # Exercise the actual registered live callback, not just cursor helpers.
        await client.handlers[1](SimpleNamespace(message=client.messages[-1]))
        await source.poll_once()
        assert seen == [(3, 0), (2, 0), (3, 0)]
        await source.poll_once()
        assert len(seen) == 3
        # The existing service claim rejects the second delivery of source 3.

    asyncio.run(run())


def test_post_published_during_startup_processing_is_recovered(monkeypatch):
    async def run():
        client = FakeTelegramClient([_message(1, text="lookback")])
        seen = []
        source = _source(monkeypatch, client, seen, lookback=5)
        receive = source._on_message

        async def publish_during_processing(post):
            await receive(post)
            if post.source.message_id == 1:
                client.messages.append(_message(2, text="new signal during lookback"))

        source._on_message = publish_during_processing
        await source.start()
        await source.poll_once()
        assert seen == [(1, 0), (2, 0)]

    asyncio.run(run())


def test_album_crossing_poll_limit_remains_one_complete_post(monkeypatch):
    async def run():
        client = FakeTelegramClient([_message(1)])
        seen = []
        source = _source(monkeypatch, client, seen)
        await source.start()
        client.messages += [_message(n, text="comment") for n in range(2, 201)]
        client.messages += [
            _message(201, text="signal", grouped_id=99, image=b"chart"),
            _message(202, grouped_id=99, image=b"position"),
            _message(203, text="next post"),
        ]
        await source.poll_once()
        assert seen[-1] == (201, 2)
        await source.poll_once()
        assert [post for post in seen if post[0] >= 201] == [(201, 2), (203, 0)]

    asyncio.run(run())


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("empty_download", [False, True])
def test_failed_image_retries_before_advancing_past_it(
    monkeypatch, startup, empty_download
):
    class FlakyImage(FakeMessage):
        attempts = 0

        async def download_media(self, *, file):
            self.attempts += 1
            if self.attempts == 1:
                if empty_download:
                    return b""
                raise OSError("temporary download failure")
            return b"chart"

    async def run():
        client = FakeTelegramClient(
            [_message(1, published_at=datetime.now(UTC) - timedelta(hours=6))]
        )
        seen = []
        source = _source(monkeypatch, client, seen, lookback=5 if startup else 0)
        if not startup:
            await source.start()
        client.messages += [
            FlakyImage(
                message_id=2,
                published_at=datetime.now(UTC),
                text="signal",
                image=b"chart",
            ),
            _message(3, text="later post"),
        ]
        if startup:
            await source.start()
        else:
            await source.poll_once()
        assert seen == []
        await source.poll_once()
        assert seen == [(2, 1), (3, 0)]

    asyncio.run(run())


def test_failed_seed_retries_from_original_startup_boundary(monkeypatch):
    class FlakyHistory(FakeTelegramClient):
        failed = False

        def iter_messages(self, entity, **kwargs):
            if not self.failed:
                self.failed = True

                async def fail():
                    raise OSError("history unavailable")
                    yield  # Make this an async iterator.

                return fail()
            return super().iter_messages(entity, **kwargs)

    async def run():
        client = FlakyHistory(
            [
                _message(
                    1, text="old", published_at=datetime.now(UTC) - timedelta(hours=6)
                ),
                _message(2, text="within lookback"),
            ]
        )
        seen = []
        source = _source(monkeypatch, client, seen, lookback=5)
        await source.start()
        assert seen == []
        client.messages.append(_message(3, text="during seed outage"))
        await source.poll_once()
        assert seen == [(2, 0), (3, 0)]

    asyncio.run(run())


def test_empty_channel_and_unsupported_post_do_not_disable_polling(monkeypatch):
    async def run():
        client = FakeTelegramClient([])
        seen = []
        source = _source(monkeypatch, client, seen)
        await source.start()
        client.messages += [_message(1), _message(2, text="first signal")]
        await source.poll_once()
        await source.poll_once()
        assert seen == [(2, 0)]

    asyncio.run(run())


def test_startup_time_boundary_includes_whole_album(monkeypatch):
    async def run():
        now = datetime.now(UTC)
        client = FakeTelegramClient(
            [
                _message(1, published_at=now - timedelta(hours=6)),
                _message(
                    2,
                    published_at=now - timedelta(hours=5, minutes=1),
                    text="signal",
                    grouped_id=99,
                    image=b"chart",
                ),
                _message(
                    3,
                    published_at=now - timedelta(hours=4, minutes=59),
                    grouped_id=99,
                    image=b"position",
                ),
            ]
        )
        seen = []
        source = _source(monkeypatch, client, seen, lookback=5)
        await source.start()
        await source.poll_once()
        assert seen == [(2, 2)]

    asyncio.run(run())


def test_partial_live_album_resolves_to_complete_canonical_post(monkeypatch):
    async def run():
        client = FakeTelegramClient([_message(1)])
        seen = []
        source = _source(monkeypatch, client, seen)
        await source.start()
        client.messages += [
            _message(2, text="signal", grouped_id=99, image=b"chart"),
            _message(3, grouped_id=99, image=b"position"),
        ]
        # The live album event contains only the tail; history has both images.
        await client.handlers[0](
            SimpleNamespace(messages=[client.messages[-1]], chat=client.entity)
        )
        await source.poll_once()
        assert seen == [(2, 2), (2, 2)]
        # Both paths deliver the same identity for the service's durable claim.

    asyncio.run(run())


def test_fresh_album_waits_for_tail_before_delivery(monkeypatch):
    async def run():
        client = FakeTelegramClient([_message(1)])
        seen = []
        source = _source(monkeypatch, client, seen)
        await source.start()
        client.messages.append(
            _message(
                2,
                published_at=datetime.now(UTC),
                text="signal",
                grouped_id=99,
                image=b"chart",
            )
        )

        async def finish_album(delay):
            assert 0 < delay <= 2
            client.messages.append(_message(3, grouped_id=99, image=b"position"))

        monkeypatch.setattr(telegram_source.asyncio, "sleep", finish_album)
        await source.poll_once()
        assert seen == [(2, 2)]

    asyncio.run(run())
