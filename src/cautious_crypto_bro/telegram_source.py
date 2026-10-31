from __future__ import annotations

import asyncio
import logging
from collections.abc import (
    Awaitable,
    Buffer,
    Callable,
    Sequence,
)
from dataclasses import replace
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from io import BytesIO
from typing import Any, cast

from telethon import (
    TelegramClient,
    events,
)
from telethon.errors import UnauthorizedError
from telethon.tl.custom.message import (
    Message,
)
from telethon.tl.custom.qrlogin import QRLogin
from telethon.tl.types import User

from .domain import (
    ImageAttachment,
    IncomingPost,
    SourceMessage,
)

logger = logging.getLogger(__name__)
MAX_CONCURRENT_POSTS = 2


class SourceAuthorizationRequired(RuntimeError):
    pass


def _require_authorization() -> str:
    raise SourceAuthorizationRequired("Telegram authorization required")


MAX_POST_IMAGE_BYTES = 20 * 1024 * 1024


class _ImageBudgetExceeded(ValueError):
    pass


class _BoundedImageBuffer(BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, data: Buffer, /) -> int:
        if self.tell() + memoryview(data).nbytes > self._limit:
            raise _ImageBudgetExceeded
        return super().write(data)


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

    texts = _album_captions(ordered)
    rejection_reason = None
    try:
        images = await _collect_album_images(ordered)
    except _ImageBudgetExceeded:
        images = []
        rejection_reason = (
            f"Telegram post image budget exceeded ({MAX_POST_IMAGE_BYTES} bytes)"
        )

    if not texts and not images and rejection_reason is None:
        logger.info(
            "Ignoring Telegram message group "
            "starting at %s: no text or "
            "supported images",
            ordered[0].id,
        )
        return None

    canonical = ordered[0]

    source = _album_source(canonical, ordered, resolved_chat, texts)

    if source is None:
        return None

    return IncomingPost(
        source=source,
        images=tuple(images),
        rejection_reason=rejection_reason,
    )


def _album_captions(ordered: tuple[Message, ...]) -> list[str]:
    texts: list[str] = []
    for message in ordered:
        text = (message.message or "").strip()
        if text and text not in texts:
            texts.append(text)
    return texts


async def _collect_album_images(ordered: tuple[Message, ...]) -> list[ImageAttachment]:
    images: list[ImageAttachment] = []
    remaining = MAX_POST_IMAGE_BYTES
    for message in ordered:
        media_type = telegram_image_media_type(message)

        if media_type is None:
            continue

        try:
            with _BoundedImageBuffer(remaining) as output:
                await message.download_media(file=output)
                data = output.getvalue()
        except _ImageBudgetExceeded:
            raise
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
        remaining -= len(data)

    return images


def _album_source(
    canonical: Message,
    ordered: tuple[Message, ...],
    resolved_chat: object,
    texts: list[str],
) -> SourceMessage | None:
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

    return SourceMessage(
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
        retry_message_ids: Callable[[int], Awaitable[tuple[int, ...]]] | None = None,
        defer_unavailable_messages: Callable[[int, tuple[int, ...]], Awaitable[None]]
        | None = None,
        load_history_cursor: Callable[[int], Awaitable[int | None]] | None = None,
        save_history_cursor: Callable[[int, int], Awaitable[None]] | None = None,
        source_completed: Callable[[int, int], Awaitable[bool]] | None = None,
    ) -> None:
        if startup_lookback_hours < 0:
            raise ValueError("startup_lookback_hours must not be negative")

        if catchup_interval_seconds <= 0:
            raise ValueError("catchup_interval_seconds must be positive")
        callbacks = (load_history_cursor, save_history_cursor, source_completed)
        if any(callback is not None for callback in callbacks) and any(
            callback is None for callback in callbacks
        ):
            raise ValueError("Durable source recovery requires all cursor callbacks")

        self._client = TelegramClient(
            session_name,
            api_id,
            api_hash,
        )

        self._channels = channels
        self._on_message = on_message
        self._post_slots = asyncio.Semaphore(MAX_CONCURRENT_POSTS)

        self._startup_lookback_hours = startup_lookback_hours
        self._catchup_interval_seconds = catchup_interval_seconds
        self._retry_message_ids = retry_message_ids
        self._defer_unavailable_messages = defer_unavailable_messages
        self._load_history_cursor = load_history_cursor
        self._save_history_cursor = save_history_cursor
        self._source_completed = source_completed
        self._entities: list[Any] = []
        # Only chronological history reconciliation may advance this cursor.
        # A newer live delivery says nothing about earlier missed messages.
        self._watermarks: dict[int, int] = {}
        self._history_cutoff: datetime | None = None
        self._bootstrap_cutoff: datetime | None = None
        self._handlers_registered = False
        self._read_errors: dict[int, str] = {}

    def read_warnings(self) -> tuple[str, ...]:
        return tuple(
            f"{channel}: {reason}"
            for channel, reason in sorted(self._read_errors.items())
        )

    async def session_user_id(self) -> int | None:
        await self._client.connect()
        user = await self._client.get_me()
        return user.id if isinstance(user, User) else None

    async def login_qr(self) -> QRLogin:
        return await self._client.qr_login()

    async def start(self) -> None:
        # Keep this boundary fixed through startup and failed seeding retries.
        # Telegram dates have second precision, so include the starting second.
        if self._history_cutoff is None:
            self._history_cutoff = datetime.now(UTC).replace(microsecond=0) - timedelta(
                hours=self._startup_lookback_hours
            )
            self._bootstrap_cutoff = self._history_cutoff
            if self._load_history_cursor is not None:
                # First enrollment has no proven earlier cursor. Inspect a bounded
                # day, requiring human approval outside the automatic window.
                self._bootstrap_cutoff = min(
                    self._history_cutoff, datetime.now(UTC) - timedelta(hours=24)
                )
        await self._client.start(phone=_require_authorization)  # pyright: ignore[reportGeneralTypeIssues]

        entities: list[Any] = []

        for channel in self._channels:
            entity = await self._client.get_entity(channel)

            entities.append(entity)

            logger.info(
                "Watching Telegram source: %s",
                channel,
            )

        self._entities = entities

        if not self._handlers_registered:
            self._register_handlers(entities)
            self._handlers_registered = True

        await self.poll_once()

        logger.info("Telegram startup complete; listening for live updates")

    def _register_handlers(self, entities: list[Any]) -> None:
        # Register once: reconnects must not duplicate post processing.
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

    async def _seed_watermarks(
        self,
        entities: list[Any],
    ) -> None:
        assert self._bootstrap_cutoff is not None
        for entity in entities:
            chat_id = _telegram_chat_id(entity)
            if chat_id in self._watermarks:
                continue

            try:
                if self._load_history_cursor is not None:
                    saved = await self._load_history_cursor(chat_id)
                    if saved is not None:
                        self._watermarks[chat_id] = saved
                        continue
                watermark = 0
                last_group = None
                # Find the last message outside the fixed startup window.
                # Include an entire album if it straddles the time boundary.
                async for message in self._client.iter_messages(entity):
                    grouped_id = getattr(message, "grouped_id", None)
                    if _as_utc(message.date) < self._bootstrap_cutoff and (
                        grouped_id is None or grouped_id != last_group
                    ):
                        watermark = message.id
                        break
                    last_group = grouped_id
                if self._save_history_cursor is not None:
                    await self._save_history_cursor(chat_id, watermark)
                self._watermarks[chat_id] = watermark
            except UnauthorizedError:
                raise
            except Exception as exc:
                self._read_errors[chat_id] = type(exc).__name__
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
            except UnauthorizedError:
                raise
            except Exception:
                logger.exception("Telegram catch-up poll failed")

    async def poll_once(
        self,
    ) -> None:
        await self._seed_watermarks(self._entities)
        for entity in self._entities:
            chat_id = _telegram_chat_id(entity)
            retry_results = await self._retry_failed_messages(entity, chat_id)
            retries_processed = retry_results is not None and all(
                retry_results.values()
            )
            watermark = self._watermarks.get(chat_id)

            if watermark is None:
                # Retry seeding next time; zero is valid for an empty channel.
                continue

            try:
                messages = await self._catchup_messages(entity, watermark)
            except UnauthorizedError:
                raise
            except Exception as exc:
                self._read_errors[chat_id] = type(exc).__name__
                logger.exception("Failed to fetch Telegram catch-up for %s", chat_id)
                continue

            if not messages:
                if retries_processed:
                    await self._refresh_read_warning(chat_id)
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

            replayed = await self._replay_catchup_messages(
                entity, messages, retry_results
            )
            if replayed and retries_processed:
                await self._refresh_read_warning(chat_id)

    async def _replay_catchup_messages(
        self,
        entity: Any,
        messages: Sequence[Message],
        retry_results: dict[int, bool] | None,
    ) -> bool:
        chat_id = _telegram_chat_id(entity)
        for group in _group_telegram_messages(messages):
            retried = retry_results.get(group[0].id) if retry_results else None
            if retried is False:
                # A thrown retry failure must still block chronological progress.
                return False
            try:
                if retried is None:
                    await self._handle_messages(group, chat=entity)
                watermark = max(message.id for message in group)
                if self._save_history_cursor is not None:
                    await self._save_history_cursor(chat_id, watermark)
                self._watermarks[chat_id] = watermark
            except Exception as exc:
                self._read_errors[chat_id] = type(exc).__name__
                logger.exception(
                    "Failed to process Telegram catch-up post starting at %s/%s",
                    group[0].chat_id,
                    group[0].id,
                )
                # Never advance over a failed post, including startup
                # downloads. Other channels can still make progress.
                return False
        return True

    async def _refresh_read_warning(
        self, chat_id: int, *, clear_recovered: bool = True
    ) -> None:
        try:
            pending = ()
            if self._retry_message_ids is not None:
                pending = await self._retry_message_ids(chat_id)
        except UnauthorizedError:
            raise
        except Exception as exc:
            self._read_errors[chat_id] = type(exc).__name__
            logger.exception(
                "Failed to check Telegram post retry status for %s", chat_id
            )
            return
        if pending:
            # The callback can record a failed post without raising, so a
            # successful history read alone does not prove processing recovered.
            self._read_errors[chat_id] = "Post processing retries pending"
        elif clear_recovered:
            self._read_errors.pop(chat_id, None)

    async def _catchup_messages(self, entity: Any, watermark: int) -> list[Message]:
        messages: list[Message] = []
        async for message in self._client.iter_messages(
            entity, min_id=watermark, reverse=True
        ):
            # Bound work per channel, but finish the boundary album.
            if len(messages) >= 200:
                last_group = getattr(messages[-1], "grouped_id", None)
                if last_group is None or last_group != getattr(
                    message, "grouped_id", None
                ):
                    break
            messages.append(message)
        return messages

    async def _retry_failed_messages(
        self, entity: Any, chat_id: int
    ) -> dict[int, bool] | None:
        """Return callback delivery outcomes; None means retry lookup/fetch failed."""
        if self._retry_message_ids is None:
            return {}
        try:
            message_ids = await self._retry_message_ids(chat_id)
            if not message_ids:
                return {}
            messages = cast(
                Sequence[Message | None],
                await self._client.get_messages(entity, ids=list(message_ids)),
            )
        except UnauthorizedError:
            raise
        except Exception as exc:
            self._read_errors[chat_id] = type(exc).__name__
            logger.exception("Failed to fetch retryable Telegram posts for %s", chat_id)
            return None

        available = [
            message
            for message in messages
            if message is not None and getattr(message, "date", None) is not None
        ]
        missing = set(message_ids) - {message.id for message in available}
        if missing:
            if self._defer_unavailable_messages is not None:
                await self._defer_unavailable_messages(chat_id, tuple(sorted(missing)))
            logger.warning(
                "Retryable Telegram posts unavailable in %s: %s", chat_id, missing
            )
        results: dict[int, bool] = {}
        for group in _group_telegram_messages(available):
            processed = True
            try:
                await self._handle_messages(group, chat=entity)
            except Exception as exc:
                processed = False
                self._read_errors[chat_id] = type(exc).__name__
                logger.exception(
                    "Failed to retry Telegram post %s/%s", chat_id, group[0].id
                )
            results.update((message.id, processed) for message in group)
        return results

    async def _handle_messages(
        self,
        messages: Sequence[Message],
        *,
        chat: object | None = None,
    ) -> None:
        # Bound buffered media as well as model calls, across live and replay paths.
        async with self._post_slots:
            messages = await telegram_message_group(
                self._client,
                chat if chat is not None else messages[0].chat_id,
                messages,
            )
            channel_id = messages[0].chat_id
            if (
                self._source_completed is not None
                and channel_id is not None
                and await self._source_completed(
                    channel_id, min(message.id for message in messages)
                )
            ):
                return
            post = await telegram_messages_to_post(
                messages,
                chat=chat,
            )

            if post is not None:
                if (
                    self._load_history_cursor is not None
                    and self._history_cutoff is not None
                    and _as_utc(post.source.published_at) < self._history_cutoff
                ):
                    post = replace(
                        post,
                        source=post.source.model_copy(
                            update={
                                "recovery_review_reason": "Recovered outside the automatic startup window; "
                                "verify this historical instruction before approving."
                            }
                        ),
                    )
                logger.info(
                    "Delivering Telegram post %s/%s (%d image(s))",
                    post.source.channel_id,
                    post.source.message_id,
                    len(post.images),
                )
                await self._on_message(post)
                await self._refresh_read_warning(
                    post.source.channel_id, clear_recovered=False
                )
                if (
                    self._source_completed is not None
                    and not await self._source_completed(
                        post.source.channel_id, post.source.message_id
                    )
                ):
                    raise RuntimeError("Source post processing remains incomplete")

    async def run_until_disconnected(
        self,
    ) -> None:
        await self._client.run_until_disconnected()  # pyright: ignore[reportGeneralTypeIssues]
        # A normally returning TaskGroup child does not stop its siblings.
        raise RuntimeError("Telegram source disconnected; ingestion stopped")

    async def disconnect(
        self,
    ) -> None:
        await self._client.disconnect()  # pyright: ignore[reportGeneralTypeIssues]
