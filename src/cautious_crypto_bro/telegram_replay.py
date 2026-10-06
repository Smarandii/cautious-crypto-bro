from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.tl.custom.message import (
    Message,
)

from .domain import IncomingPost
from .telegram_source import telegram_message_group, telegram_messages_to_post


class TelegramReplayError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramPostReference:
    entity: str | int
    message_id: int


def parse_telegram_post_url(
    url: str,
) -> TelegramPostReference:
    parsed = urlparse(url.strip())

    _require_telegram_host(parsed.netloc)

    parts = [part for part in parsed.path.split("/") if part]

    if not parts:
        raise TelegramReplayError("Telegram URL does not contain a post")

    if parts[0] == "c":
        entity, message_part = _private_channel_reference(parts)
    elif parts[0] == "s":
        entity, message_part = _preview_reference(parts)
    else:
        entity, message_part = _public_channel_reference(parts)

    try:
        message_id = int(message_part)
    except ValueError as exc:
        raise TelegramReplayError("Telegram message id must be numeric") from exc

    if message_id <= 0:
        raise TelegramReplayError("Telegram message id must be positive")

    return TelegramPostReference(
        entity=entity,
        message_id=message_id,
    )


def _require_telegram_host(netloc: str) -> None:
    host = netloc.lower().removeprefix("www.")

    if host not in {
        "t.me",
        "telegram.me",
    }:
        raise TelegramReplayError("Expected a t.me Telegram post URL")


def _private_channel_reference(
    parts: list[str],
) -> tuple[str | int, str]:
    if len(parts) < 3:
        raise TelegramReplayError(
            "Private-channel link must look like https://t.me/c/<channel>/<message>"
        )

    channel_part = parts[1]

    if not channel_part.isdigit():
        raise TelegramReplayError("Invalid private Telegram channel id")

    return int(f"-100{channel_part}"), parts[2]


def _preview_reference(
    parts: list[str],
) -> tuple[str | int, str]:
    if len(parts) < 3:
        raise TelegramReplayError(
            "Telegram preview link must contain channel and message id"
        )

    return parts[1], parts[2]


def _public_channel_reference(
    parts: list[str],
) -> tuple[str | int, str]:
    if len(parts) < 2:
        raise TelegramReplayError(
            "Public-channel link must look like https://t.me/<channel>/<message>"
        )

    return parts[0], parts[1]


async def fetch_telegram_post(
    *,
    url: str,
    api_id: int,
    api_hash: str,
    session_name: str,
) -> IncomingPost:
    reference = parse_telegram_post_url(url)

    with cloned_telegram_session(session_name) as replay_session:
        client = TelegramClient(
            replay_session,
            api_id,
            api_hash,
            receive_updates=False,
        )

        await client.connect()

        try:
            if not (await client.is_user_authorized()):
                raise TelegramReplayError("Cloned Telegram session is not authorized")

            try:
                entity = await client.get_entity(reference.entity)
            except ValueError:
                await client.get_dialogs()

                entity = await client.get_entity(reference.entity)

            message = cast(
                Message | None,
                await client.get_messages(
                    entity,
                    ids=(reference.message_id),
                ),
            )

            if message is None:
                raise TelegramReplayError(
                    "Telegram post was not found "
                    "or is not accessible to "
                    "the configured account"
                )

            messages = await telegram_message_group(
                client,
                entity,
                (message,),
            )

            post = await telegram_messages_to_post(
                messages,
                chat=entity,
            )

            if post is None:
                raise TelegramReplayError(
                    "Telegram post contains no supported text or image"
                )

            return post

        finally:
            await client.disconnect()  # pyright: ignore[reportGeneralTypeIssues]


@contextmanager
def cloned_telegram_session(
    session_name: str,
):
    source_path = _session_database_path(session_name)

    if not source_path.is_file():
        raise TelegramReplayError(f"Telegram session database not found: {source_path}")

    with tempfile.TemporaryDirectory(prefix="ccb-telegram-replay-") as directory:
        target_base = Path(directory) / "telegram_replay"
        target_database = Path(f"{target_base}.session")

        with (
            closing(sqlite3.connect(source_path)) as source,
            closing(sqlite3.connect(target_database)) as target,
        ):
            source.backup(target)

        yield str(target_base)


def _session_database_path(
    session_name: str,
) -> Path:
    path = Path(session_name)

    return path if path.name.endswith(".session") else Path(f"{session_name}.session")
