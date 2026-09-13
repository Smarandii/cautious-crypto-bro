from __future__ import annotations

import logging
from collections.abc import (
    Awaitable,
    Callable,
    Sequence,
)
from datetime import (
    UTC,
    datetime,
    timedelta,
)

from telethon import (
    TelegramClient,
    events,
)
from telethon.tl.custom.message import (
    Message,
)

from .domain import (
    ImageAttachment,
    IncomingPost,
    SourceMessage,
)

logger = logging.getLogger(__name__)

MessageHandler = Callable[
    [IncomingPost],
    Awaitable[None],
]


def _as_utc(
    value: datetime,
) -> datetime:
    if value.tzinfo is None:
        return value.replace(
            tzinfo=UTC,
        )

    return value.astimezone(UTC)


async def telegram_messages_to_post(
    messages: Sequence[Message],
    *,
    chat: object | None = None,
) -> IncomingPost | None:
    if not messages:
        return None

    ordered = tuple(
        sorted(
            messages,
            key=lambda message: message.id,
        )
    )

    resolved_chat = chat

    if resolved_chat is None:
        resolved_chat = next(
            (message.chat for message in ordered if message.chat is not None),
            None,
        )

    if resolved_chat is None:
        return None

    texts: list[str] = []
    images: list[ImageAttachment] = []

    for message in ordered:
        text = (message.message or "").strip()

        # Telegram albums normally have one caption, but
        # preserving multiple distinct captions is safer.
        if text and text not in texts:
            texts.append(text)

        media_type = telegram_image_media_type(message)

        if media_type is None:
            continue

        try:
            data = await message.download_media(file=bytes)
        except Exception:
            logger.exception(
                "Failed to download image from Telegram message %s",
                message.id,
            )
            return None

        if not data:
            logger.warning(
                "Telegram returned empty image for message %s",
                message.id,
            )
            return None

        images.append(
            ImageAttachment(
                media_type=media_type,
                data=bytes(data),
            )
        )

    if not texts and not images:
        logger.info(
            "Ignoring Telegram message group "
            "starting at %s: no text or "
            "supported images",
            ordered[0].id,
        )
        return None

    canonical = ordered[0]

    published_at = min(_as_utc(message.date) for message in ordered)

    source = SourceMessage(
        channel_id=canonical.chat_id,
        channel_title=(
            getattr(
                resolved_chat,
                "title",
                None,
            )
            or str(canonical.chat_id)
        ),
        channel_username=getattr(
            resolved_chat,
            "username",
            None,
        ),
        # The lowest Telegram message id is the
        # deterministic identity of the album.
        message_id=canonical.id,
        published_at=published_at,
        received_at=datetime.now(UTC),
        text="\n".join(texts),
    )

    return IncomingPost(
        source=source,
        images=tuple(images),
    )


async def telegram_message_to_post(
    message: Message,
    *,
    chat: object | None = None,
) -> IncomingPost | None:
    return await telegram_messages_to_post(
        (message,),
        chat=chat,
    )


def telegram_image_media_type(
    message: Message,
) -> str | None:
    if message.photo is not None:
        return "image/jpeg"

    file = message.file

    media_type = (
        getattr(
            file,
            "mime_type",
            None,
        )
        if file is not None
        else None
    )

    if isinstance(
        media_type,
        str,
    ) and media_type.startswith("image/"):
        return media_type

    return None


def _group_telegram_messages(
    messages: Sequence[Message],
) -> tuple[
    tuple[Message, ...],
    ...,
]:
    ordered = sorted(
        messages,
        key=lambda message: message.id,
    )

    groups: list[list[Message]] = []

    album_indexes: dict[
        int,
        int,
    ] = {}

    for message in ordered:
        grouped_id = getattr(
            message,
            "grouped_id",
            None,
        )

        if grouped_id is None:
            groups.append([message])
            continue

        group_index = album_indexes.get(grouped_id)

        if group_index is None:
            album_indexes[grouped_id] = len(groups)

            groups.append([message])
            continue

        groups[group_index].append(message)

    return tuple(tuple(group) for group in groups)


class TelegramSource:
    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_name: str,
        channels: list[str | int],
        on_message: MessageHandler,
        startup_lookback_hours: int = 0,
    ) -> None:
        if startup_lookback_hours < 0:
            raise ValueError("startup_lookback_hours must not be negative")

        self._client = TelegramClient(
            session_name,
            api_id,
            api_hash,
        )

        self._channels = channels
        self._on_message = on_message

        self._startup_lookback_hours = startup_lookback_hours

    async def start(self) -> None:
        await self._client.start()

        entities = []

        for channel in self._channels:
            entity = await self._client.get_entity(channel)

            entities.append(entity)

            logger.info(
                "Watching Telegram source: %s",
                channel,
            )

        # Albums are handled as one logical Telegram post.
        @self._client.on(events.Album(chats=entities))
        async def handle_album(
            event: events.Album.Event,
        ) -> None:
            await self._handle_messages(
                tuple(event.messages),
                chat=event.chat,
            )

        # Telethon also emits NewMessage for each album
        # member. Skip grouped messages here so the Album
        # event is the only live processing path for them.
        @self._client.on(events.NewMessage(chats=entities))
        async def handle_message(
            event: (events.NewMessage.Event),
        ) -> None:
            if (
                getattr(
                    event.message,
                    "grouped_id",
                    None,
                )
                is not None
            ):
                return

            await self._handle_messages((event.message,))

        if self._startup_lookback_hours:
            await self._run_startup_lookback(entities)

        logger.info("Telegram startup complete; listening for live updates")

    async def _run_startup_lookback(
        self,
        entities: list[object],
    ) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=(self._startup_lookback_hours))

        logger.info(
            "Scanning Telegram history for the previous %d hour(s)",
            self._startup_lookback_hours,
        )

        for entity in entities:
            messages: list[Message] = []

            last_included_group: int | None = None

            try:
                # Telethon history is newest-first.
                async for message in self._client.iter_messages(entity):
                    grouped_id = getattr(
                        message,
                        "grouped_id",
                        None,
                    )

                    if _as_utc(message.date) < cutoff:
                        # If the lookback boundary falls in
                        # the middle of an album, finish
                        # collecting that album.
                        if (
                            last_included_group is not None
                            and grouped_id == last_included_group
                        ):
                            messages.append(message)
                            continue

                        break

                    messages.append(message)

                    last_included_group = grouped_id

            except Exception:
                logger.exception(
                    "Failed to fetch Telegram startup lookback for %s",
                    getattr(
                        entity,
                        "title",
                        entity,
                    ),
                )
                continue

            posts = _group_telegram_messages(messages)

            logger.info(
                "Startup lookback found %d message(s) / %d post(s) in %s",
                len(messages),
                len(posts),
                getattr(
                    entity,
                    "title",
                    entity,
                ),
            )

            for group in posts:
                try:
                    await self._handle_messages(
                        group,
                        chat=entity,
                    )
                except Exception:
                    logger.exception(
                        "Failed to process Telegram "
                        "startup lookback post "
                        "starting at %s/%s",
                        group[0].chat_id,
                        group[0].id,
                    )

    async def _handle_messages(
        self,
        messages: Sequence[Message],
        *,
        chat: object | None = None,
    ) -> None:
        post = await telegram_messages_to_post(
            messages,
            chat=chat,
        )

        if post is not None:
            await self._on_message(post)

    async def run_until_disconnected(
        self,
    ) -> None:
        await self._client.run_until_disconnected()

    async def disconnect(
        self,
    ) -> None:
        await self._client.disconnect()
