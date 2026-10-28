"""Compare visible supported history with intake evidence, without replaying it."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

import aiosqlite
from telethon import TelegramClient

from .telegram_source import (
    _as_utc,
    _group_telegram_messages,
    telegram_image_media_type,
)


async def _history_groups(client, entity, since, max_messages):
    messages = []
    complete = True
    omitted_album = False
    async for message in client.iter_messages(entity, limit=max_messages + 1):
        group = getattr(message, "grouped_id", None)
        previous_group = getattr(messages[-1], "grouped_id", None) if messages else None
        if _as_utc(message.date) < since and (group is None or group != previous_group):
            break
        if len(messages) >= max_messages:
            complete = False
            if group is not None and group == previous_group:
                # The cap cut an album before its canonical head. Do not
                # misidentify its visible tail as an independently missing post.
                messages = [m for m in messages if m.grouped_id != group]
                omitted_album = True
            break
        messages.append(message)
    return _group_telegram_messages(messages), complete, omitted_album


async def _channel_intake(db, groups, until, has_decisions):
    statuses: Counter[str] = Counter()
    missing = []
    unknown = []
    supported = unsupported = decided = rejected = 0
    for group in groups:
        if max(_as_utc(message.date) for message in group) > until:
            continue
        if not any(
            (m.message or "").strip() or telegram_image_media_type(m) for m in group
        ):
            unsupported += 1
            continue
        supported += 1
        channel_id = group[0].chat_id
        message_id = min(message.id for message in group)
        cursor = await db.execute(
            "SELECT status,last_error,attempt_count FROM source_messages "
            "WHERE channel_id=? AND message_id=?",
            (channel_id, message_id),
        )
        row = await cursor.fetchone()
        if row is None:
            missing.append(message_id)
            continue
        status, reason, attempt = row
        statuses[status] += 1
        if status != "COMPLETED":
            continue
        decision = None
        if has_decisions:
            cursor = await db.execute(
                "SELECT 1 FROM source_decisions WHERE channel_id=? AND message_id=? "
                "AND attempt_count=? LIMIT 1",
                (channel_id, message_id, attempt),
            )
            decision = await cursor.fetchone()
        if decision:
            decided += 1
        elif reason:
            rejected += 1
        else:
            unknown.append(message_id)
    return {
        "supported_posts": supported,
        "unsupported_groups": unsupported,
        "source_statuses": dict(sorted(statuses.items())),
        "completed_with_decision": decided,
        "recorded_rejections": rejected,
        "missing_source_ids": sorted(missing),
        "completed_without_decision_ids": sorted(unknown),
    }


async def telegram_history_coverage(
    database_path: Path,
    client: TelegramClient,
    channels: list[str | int],
    *,
    since: datetime,
    until: datetime,
    max_messages: int = 2000,
) -> dict:
    """Audit publication-window metadata; callers own an isolated client session."""
    since, until = _as_utc(since), _as_utc(until)
    if since >= until or max_messages <= 0 or not channels:
        raise ValueError("History audit requires a positive window, cap and channels")
    histories = []
    for index, channel in enumerate(channels, 1):
        try:
            entity = await client.get_entity(channel)
            groups, complete, omitted = await _history_groups(
                client, entity, since, max_messages
            )
            histories.append((index, groups, complete, omitted, None))
        except Exception as exc:
            # Exception strings can disclose channel/session details. Other
            # channels still provide useful evidence, never overall success.
            histories.append((index, (), False, False, type(exc).__name__))
    results = []
    uri = database_path.resolve().as_uri() + "?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='source_decisions' AND type='table'"
        )
        has_decisions = await cursor.fetchone() is not None
        for index, groups, complete, omitted, error in histories:
            intake = await _channel_intake(db, groups, until, has_decisions)
            results.append(
                {
                    "channel_index": index,
                    "history_complete": complete,
                    "omitted_partial_album": omitted,
                    "error": error,
                    **intake,
                    "intake_coverage_complete": complete
                    and intake["completed_with_decision"]
                    + intake["recorded_rejections"]
                    == intake["supported_posts"],
                }
            )
    # Zen of Python: "Explicit is better than implicit." Visible-history scope
    # and truncated scans must not become a blanket all-posts success claim.
    return {
        "window_basis": "visible Telegram publication time (including boundary albums)",
        "since": since.isoformat(),
        "until": until.isoformat(),
        "settling_basis": "Every album member must be at or before until",
        "max_messages_per_channel": max_messages,
        "history_complete": all(row["history_complete"] for row in results),
        "intake_coverage_complete": all(
            row["intake_coverage_complete"] for row in results
        ),
        "scope": "Supported visible posts only; excludes edits, deleted/inaccessible history, model correctness and execution success",
        "channels": results,
    }
