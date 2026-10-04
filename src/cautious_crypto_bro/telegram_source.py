from __future__ import annotations

import asyncio
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
from typing import Any, cast

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
            raise

        if not data:
            logger.warning(
                "Telegram returned empty image for message %s",
                message.id,
            )
            raise RuntimeError(
                f"Telegram returned empty image for message {message.id}"
            )

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

    dates = tuple(message.date for message in ordered if message.date is not None)

    if not dates:
        logger.warning(
            "Ignoring Telegram message group starting at %s: no publication date",
            ordered[0].id,
        )
        return None

    published_at = min(_as_utc(value) for value in dates)

    channel_id = canonical.chat_id

    if channel_id is None:
        logger.warning(
            "Ignoring Telegram message group starting at %s: no channel id",
            ordered[0].id,
        )
        return None

    source = SourceMessage(
        channel_id=channel_id,
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


def _telegram_chat_id(
    entity: Any,
) -> int:
    # Telethon exposes the raw channel id; SourceMessage uses the
    # -100<id> form that Message.chat_id already carries.
    return -int("100" + str(entity.id))


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


async def telegram_message_group(
    client: TelegramClient,
    entity: Any,
    messages: Sequence[Message],
) -> tuple[Message, ...]:
    """Resolve an album from any member, including a partial live event."""
    first = min(messages, key=lambda message: message.id)
    grouped_id = getattr(first, "grouped_id", None)
    if grouped_id is None:
        return tuple(messages)

    # Let a newly published album settle before fetching its adjacent members.
    # Bound the wait even when the source/server clock is ahead of ours.
    newest_date = max(
        (_as_utc(message.date) for message in messages if message.date is not None),
        default=None,
    )
    if newest_date is not None:
        delay = min(2.0, (newest_date - datetime.now(UTC)).total_seconds() + 2.0)
        if delay > 0:
            await asyncio.sleep(delay)

    # Reuse replay's bounded neighborhood: Telegram albums have at most ten
    # adjacent items. This also finds the canonical head from a trailing item.
    candidates = cast(
        Sequence[Message | None],
        await client.get_messages(
            entity, ids=list(range(max(1, first.id - 12), first.id + 13))
        ),
    )
    grouped = {message.id: message for message in messages}
    grouped.update(
        (message.id, message)
        for message in candidates
        if message is not None and getattr(message, "grouped_id", None) == grouped_id
    )
    return tuple(sorted(grouped.values(), key=lambda message: message.id))


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
        catchup_interval_seconds: int = 60,
    ) -> None:
        if startup_lookback_hours < 0:
            raise ValueError("startup_lookback_hours must not be negative")

        if catchup_interval_seconds <= 0:
            raise ValueError("catchup_interval_seconds must be positive")

        self._client = TelegramClient(
            session_name,
            api_id,
            api_hash,
        )

        self._channels = channels
        self._on_message = on_message

        self._startup_lookback_hours = startup_lookback_hours
        self._catchup_interval_seconds = catchup_interval_seconds
        self._entities: list[Any] = []
        # Only chronological history reconciliation may advance this cursor.
        # A newer live delivery says nothing about earlier missed messages.
        self._watermarks: dict[int, int] = {}
        self._history_cutoff: datetime | None = None

    async def start(self) -> None:
        # Keep this boundary fixed through startup and failed seeding retries.
        # Telegram dates have second precision, so include the starting second.
        self._history_cutoff = datetime.now(UTC).replace(microsecond=0) - timedelta(
            hours=self._startup_lookback_hours
        )
        await self._client.start()  # pyright: ignore[reportGeneralTypeIssues]

        entities: list[Any] = []

        for channel in self._channels:
            entity = await self._client.get_entity(channel)

            entities.append(entity)

            logger.info(
                "Watching Telegram source: %s",
                channel,
            )

        self._entities = entities

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

        await self.poll_once()

        logger.info("Telegram startup complete; listening for live updates")

    async def _seed_watermarks(
        self,
        entities: list[Any],
    ) -> None:
        assert self._history_cutoff is not None
        for entity in entities:
            chat_id = _telegram_chat_id(entity)
            if chat_id in self._watermarks:
                continue

            try:
                watermark = 0
                last_group = None
                # Find the last message outside the fixed startup window.
                # Include an entire album if it straddles the time boundary.
                async for message in self._client.iter_messages(entity):
                    grouped_id = getattr(message, "grouped_id", None)
                    if _as_utc(message.date) < self._history_cutoff and (
                        grouped_id is None or grouped_id != last_group
                    ):
                        watermark = message.id
                        break
                    last_group = grouped_id
                self._watermarks[chat_id] = watermark
            except Exception:
                logger.exception(
                    "Failed to seed Telegram catch-up watermark for %s",
                    getattr(
                        entity,
                        "title",
                        entity,
                    ),
                )

    async def run_catchup(
        self,
    ) -> None:
        while True:
            await asyncio.sleep(self._catchup_interval_seconds)

            try:
                await self.poll_once()
            except Exception:
                logger.exception("Telegram catch-up poll failed")

    async def poll_once(
        self,
    ) -> None:
        await self._seed_watermarks(self._entities)
        for entity in self._entities:
            chat_id = _telegram_chat_id(entity)
            watermark = self._watermarks.get(chat_id)

            if watermark is None:
                # Retry seeding next time; zero is valid for an empty channel.
                continue

            messages: list[Message] = []
            try:
                async for message in self._client.iter_messages(
                    entity,
                    min_id=watermark,
                    reverse=True,
                ):
                    # Bound work per channel, but finish the boundary album.
                    if len(messages) >= 200:
                        last_group = getattr(messages[-1], "grouped_id", None)
                        if last_group is None or last_group != getattr(
                            message, "grouped_id", None
                        ):
                            break
                    messages.append(message)
            except Exception:
                logger.exception("Failed to fetch Telegram catch-up for %s", chat_id)
                continue

            if not messages:
                continue

            logger.info(
                "Telegram catch-up replaying %d message(s) in %s",
                len(messages),
                getattr(
                    entity,
                    "title",
                    entity,
                ),
            )

            for group in _group_telegram_messages(messages):
                try:
                    await self._handle_messages(
                        group,
                        chat=entity,
                    )
                    self._watermarks[chat_id] = max(message.id for message in group)
                except Exception:
                    logger.exception(
                        "Failed to process Telegram catch-up post starting at %s/%s",
                        group[0].chat_id,
                        group[0].id,
                    )
                    # Never advance over a failed post, including startup
                    # downloads. Other channels can still make progress.
                    break

    async def _handle_messages(
        self,
        messages: Sequence[Message],
        *,
        chat: object | None = None,
    ) -> None:
        messages = await telegram_message_group(
            self._client, chat if chat is not None else messages[0].chat_id, messages
        )
        post = await telegram_messages_to_post(
            messages,
            chat=chat,
        )

        if post is not None:
            logger.info(
                "Delivering Telegram post %s/%s (%d image(s))",
                post.source.channel_id,
                post.source.message_id,
                len(post.images),
            )
            await self._on_message(post)

    async def run_until_disconnected(
        self,
    ) -> None:
        await self._client.run_until_disconnected()  # pyright: ignore[reportGeneralTypeIssues]

    async def disconnect(
        self,
    ) -> None:
        await self._client.disconnect()  # pyright: ignore[reportGeneralTypeIssues]
