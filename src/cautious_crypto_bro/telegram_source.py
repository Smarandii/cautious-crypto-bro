from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from .domain import SourceMessage

logger = logging.getLogger(__name__)
MessageHandler = Callable[[SourceMessage], Awaitable[None]]


class TelegramSource:
    def __init__(self, *, api_id: int, api_hash: str, session_name: str,
                 channels: list[str | int], on_message: MessageHandler) -> None:
        self._client = TelegramClient(session_name, api_id, api_hash)
        self._channels = channels
        self._on_message = on_message

    async def start(self) -> None:
        await self._client.start()
        entities = []
        for channel in self._channels:
            entity = await self._client.get_entity(channel)
            entities.append(entity)
            logger.info("Watching Telegram source: %s", channel)

        @self._client.on(events.NewMessage(chats=entities))
        async def handle(event: events.NewMessage.Event) -> None:
            source = self._convert(event.message)
            if source is not None:
                await self._on_message(source)

    async def run_until_disconnected(self) -> None:
        await self._client.run_until_disconnected()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    @staticmethod
    def _convert(message: Message) -> SourceMessage | None:
        # First vertical slice supports text and media captions only.
        text = (message.message or "").strip()
        if not text:
            logger.info("Ignoring message %s: no text/caption in MVP", message.id)
            return None
        chat = message.chat
        if chat is None:
            return None

        published_at = message.date
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        return SourceMessage(
            channel_id=message.chat_id,
            channel_title=getattr(chat, "title", None) or str(message.chat_id),
            channel_username=getattr(chat, "username", None),
            message_id=message.id,
            published_at=published_at,
            received_at=datetime.now(timezone.utc),
            text=text,
        )
