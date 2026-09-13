from __future__ import annotations

import logging
from collections.abc import (
    Awaitable,
    Callable,
)
from datetime import (
    datetime,
    timezone,
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


async def telegram_message_to_post(
    message: Message,
    *,
    chat: object | None = None,
) -> IncomingPost | None:
    resolved_chat = (
        chat
        if chat is not None
        else message.chat
    )

    if resolved_chat is None:
        return None

    text = (
        message.message
        or ""
    ).strip()

    media_type = (
        telegram_image_media_type(
            message
        )
    )

    if (
        not text
        and media_type is None
    ):
        logger.info(
            "Ignoring message %s: "
            "no text or supported image",
            message.id,
        )
        return None

    images: tuple[
        ImageAttachment,
        ...,
    ] = ()

    if media_type is not None:
        try:
            data = (
                await message.download_media(
                    file=bytes
                )
            )
        except Exception:
            logger.exception(
                "Failed to download image "
                "from message %s",
                message.id,
            )
            return None

        if not data:
            logger.warning(
                "Telegram returned empty image "
                "for message %s",
                message.id,
            )
            return None

        images = (
            ImageAttachment(
                media_type=media_type,
                data=bytes(data),
            ),
        )

    published_at = (
        message.date
    )

    if published_at.tzinfo is None:
        published_at = (
            published_at.replace(
                tzinfo=timezone.utc,
            )
        )

    source = SourceMessage(
        channel_id=message.chat_id,
        channel_title=(
            getattr(
                resolved_chat,
                "title",
                None,
            )
            or str(
                message.chat_id
            )
        ),
        channel_username=getattr(
            resolved_chat,
            "username",
            None,
        ),
        message_id=message.id,
        published_at=published_at,
        received_at=datetime.now(
            timezone.utc
        ),
        text=text,
    )

    return IncomingPost(
        source=source,
        images=images,
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

    if (
        isinstance(
            media_type,
            str,
        )
        and media_type.startswith(
            "image/"
        )
    ):
        return media_type

    return None


class TelegramSource:
    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_name: str,
        channels: list[
            str | int
        ],
        on_message: MessageHandler,
    ) -> None:
        self._client = TelegramClient(
            session_name,
            api_id,
            api_hash,
        )

        self._channels = (
            channels
        )

        self._on_message = (
            on_message
        )

    async def start(self) -> None:
        await self._client.start()

        entities = []

        for channel in self._channels:
            entity = (
                await self._client
                .get_entity(
                    channel
                )
            )

            entities.append(
                entity
            )

            logger.info(
                "Watching Telegram source: %s",
                channel,
            )

        @self._client.on(
            events.NewMessage(
                chats=entities
            )
        )
        async def handle(
            event: (
                events.NewMessage.Event
            ),
        ) -> None:
            post = (
                await telegram_message_to_post(
                    event.message
                )
            )

            if post is not None:
                await self._on_message(
                    post
                )

    async def run_until_disconnected(
        self,
    ) -> None:
        await (
            self._client
            .run_until_disconnected()
        )

    async def disconnect(
        self,
    ) -> None:
        await self._client.disconnect()
