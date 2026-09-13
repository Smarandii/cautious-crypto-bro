from __future__ import annotations

import sqlite3
import tempfile
from contextlib import (
    contextmanager,
)
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.tl.custom.message import (
    Message,
)

from .domain import IncomingPost
from .telegram_source import (
    telegram_messages_to_post,
)


class TelegramReplayError(
    RuntimeError
):
    pass


@dataclass(
    frozen=True,
    slots=True,
)
class TelegramPostReference:
    entity: str | int
    message_id: int


def parse_telegram_post_url(
    url: str,
) -> TelegramPostReference:
    parsed = urlparse(
        url.strip()
    )

    host = (
        parsed.netloc
        .lower()
        .removeprefix("www.")
    )

    if host not in {
        "t.me",
        "telegram.me",
    }:
        raise TelegramReplayError(
            "Expected a t.me Telegram "
            "post URL"
        )

    parts = [
        part
        for part
        in parsed.path.split("/")
        if part
    ]

    if not parts:
        raise TelegramReplayError(
            "Telegram URL does not "
            "contain a post"
        )

    if parts[0] == "c":
        if len(parts) < 3:
            raise TelegramReplayError(
                "Private-channel link must "
                "look like "
                "https://t.me/c/<channel>/<message>"
            )

        channel_part = (
            parts[1]
        )

        if not channel_part.isdigit():
            raise TelegramReplayError(
                "Invalid private Telegram "
                "channel id"
            )

        entity: str | int = int(
            f"-100{channel_part}"
        )

        message_part = (
            parts[2]
        )

    elif parts[0] == "s":
        if len(parts) < 3:
            raise TelegramReplayError(
                "Telegram preview link must "
                "contain channel and message id"
            )

        entity = parts[1]
        message_part = (
            parts[2]
        )

    else:
        if len(parts) < 2:
            raise TelegramReplayError(
                "Public-channel link must "
                "look like "
                "https://t.me/<channel>/<message>"
            )

        entity = parts[0]
        message_part = (
            parts[1]
        )

    try:
        message_id = int(
            message_part
        )
    except ValueError as exc:
        raise TelegramReplayError(
            "Telegram message id "
            "must be numeric"
        ) from exc

    if message_id <= 0:
        raise TelegramReplayError(
            "Telegram message id "
            "must be positive"
        )

    return TelegramPostReference(
        entity=entity,
        message_id=message_id,
    )


async def _replay_message_group(
    client: TelegramClient,
    entity: object,
    message: Message,
) -> tuple[
    Message,
    ...,
]:
    grouped_id = getattr(
        message,
        "grouped_id",
        None,
    )

    if grouped_id is None:
        return (
            message,
        )

    # Telegram media groups contain at most a small
    # number of adjacent channel messages. Fetch a
    # bounded neighborhood around whichever album
    # item the replay URL references.
    radius = 12

    first_id = max(
        1,
        message.id - radius,
    )

    ids = list(
        range(
            first_id,
            message.id + radius + 1,
        )
    )

    candidates = (
        await client.get_messages(
            entity,
            ids=ids,
        )
    )

    grouped: dict[
        int,
        Message,
    ] = {
        candidate.id: candidate
        for candidate in candidates
        if (
            candidate is not None
            and getattr(
                candidate,
                "grouped_id",
                None,
            )
            == grouped_id
        )
    }

    # Keep the requested message even if Telegram's
    # neighborhood result is unexpectedly incomplete.
    grouped[
        message.id
    ] = message

    return tuple(
        sorted(
            grouped.values(),
            key=lambda item: item.id,
        )
    )


async def fetch_telegram_post(
    *,
    url: str,
    api_id: int,
    api_hash: str,
    session_name: str,
) -> IncomingPost:
    reference = (
        parse_telegram_post_url(
            url
        )
    )

    with cloned_telegram_session(
        session_name
    ) as replay_session:
        client = TelegramClient(
            replay_session,
            api_id,
            api_hash,
            receive_updates=False,
        )

        await client.connect()

        try:
            if not (
                await client
                .is_user_authorized()
            ):
                raise TelegramReplayError(
                    "Cloned Telegram session "
                    "is not authorized"
                )

            try:
                entity = (
                    await client
                    .get_entity(
                        reference.entity
                    )
                )
            except ValueError:
                await client.get_dialogs()

                entity = (
                    await client
                    .get_entity(
                        reference.entity
                    )
                )

            message = (
                await client
                .get_messages(
                    entity,
                    ids=(
                        reference.message_id
                    ),
                )
            )

            if message is None:
                raise TelegramReplayError(
                    "Telegram post was not found "
                    "or is not accessible to "
                    "the configured account"
                )

            messages = (
                await _replay_message_group(
                    client,
                    entity,
                    message,
                )
            )

            post = (
                await telegram_messages_to_post(
                    messages,
                    chat=entity,
                )
            )

            if post is None:
                raise TelegramReplayError(
                    "Telegram post contains no "
                    "supported text or image"
                )

            return post

        finally:
            await client.disconnect()


@contextmanager
def cloned_telegram_session(
    session_name: str,
):
    source_path = (
        _session_database_path(
            session_name
        )
    )

    if not source_path.is_file():
        raise TelegramReplayError(
            "Telegram session database "
            f"not found: {source_path}"
        )

    with tempfile.TemporaryDirectory(
        prefix="ccb-telegram-replay-"
    ) as directory:
        target_base = (
            Path(directory)
            / "telegram_replay"
        )

        target_database = Path(
            f"{target_base}.session"
        )

        source = sqlite3.connect(
            source_path
        )

        target = sqlite3.connect(
            target_database
        )

        try:
            source.backup(
                target
            )
        finally:
            target.close()
            source.close()

        yield str(
            target_base
        )


def _session_database_path(
    session_name: str,
) -> Path:
    path = Path(
        session_name
    )

    if path.name.endswith(
        ".session"
    ):
        return path

    return Path(
        f"{session_name}.session"
    )
