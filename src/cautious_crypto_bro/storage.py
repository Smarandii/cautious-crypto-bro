from __future__ import annotations

from pathlib import Path
from uuid import UUID

import aiosqlite

from .domain import IntentStatus, SourceMessage, TradingIntent


class IntentStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._database_path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS source_messages (
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (channel_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision_user_id INTEGER,
                    bybit_order_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signal_guidance (
                    scope TEXT NOT NULL
                        CHECK(scope IN ('global', 'channel')),
                    channel_id INTEGER,
                    content TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(
                        (scope = 'global' AND channel_id IS NULL)
                        OR
                        (scope = 'channel' AND channel_id IS NOT NULL)
                    )
                );

                CREATE UNIQUE INDEX IF NOT EXISTS ux_signal_guidance_global
                ON signal_guidance(scope)
                WHERE scope = 'global';

                CREATE UNIQUE INDEX IF NOT EXISTS ux_signal_guidance_channel
                ON signal_guidance(channel_id)
                WHERE scope = 'channel';
                """
            )
            await db.commit()

    async def save_source(self, source: SourceMessage) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO source_messages(channel_id, message_id, payload_json)
                   VALUES (?, ?, ?)""",
                (source.channel_id, source.message_id, source.model_dump_json()),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def create_intent(self, intent: TradingIntent) -> None:
        now = intent.created_at.isoformat()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """INSERT INTO intents(
                       intent_id, channel_id, message_id, payload_json,
                       status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(intent.intent_id),
                    intent.source.channel_id,
                    intent.source.message_id,
                    intent.model_dump_json(),
                    intent.status.value,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_intent(self, intent_id: UUID) -> TradingIntent | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT payload_json, status FROM intents WHERE intent_id = ?",
                (str(intent_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        intent = TradingIntent.model_validate_json(row["payload_json"])
        return intent.model_copy(update={"status": IntentStatus(row["status"])})

    async def claim_for_execution(self, intent_id: UUID, user_id: int) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """UPDATE intents
                   SET status = ?, decision_user_id = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE intent_id = ? AND status = ?""",
                (IntentStatus.EXECUTING.value, user_id, str(intent_id), IntentStatus.PENDING.value),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def mark_skipped(self, intent_id: UUID, user_id: int) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """UPDATE intents
                   SET status = ?, decision_user_id = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE intent_id = ? AND status = ?""",
                (IntentStatus.SKIPPED.value, user_id, str(intent_id), IntentStatus.PENDING.value),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def get_guidance(
        self,
        channel_id: int,
    ) -> tuple[str | None, str | None]:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                SELECT scope, content
                FROM signal_guidance
                WHERE scope = 'global'
                   OR (scope = 'channel' AND channel_id = ?)
                """,
                (channel_id,),
            )
            rows = await cursor.fetchall()

        global_guidance = None
        channel_guidance = None

        for scope, content in rows:
            if scope == "global":
                global_guidance = content
            else:
                channel_guidance = content

        return global_guidance, channel_guidance

    async def set_guidance(
        self,
        content: str,
        *,
        channel_id: int | None = None,
    ) -> None:
        content = content.strip()

        if not content:
            raise ValueError("Guidance must not be empty")

        scope = "global" if channel_id is None else "channel"

        async with aiosqlite.connect(self._database_path) as db:
            if scope == "global":
                await db.execute(
                    "DELETE FROM signal_guidance "
                    "WHERE scope = 'global'"
                )
            else:
                await db.execute(
                    """
                    DELETE FROM signal_guidance
                    WHERE scope = 'channel'
                      AND channel_id = ?
                    """,
                    (channel_id,),
                )

            await db.execute(
                """
                INSERT INTO signal_guidance(
                    scope,
                    channel_id,
                    content,
                    updated_at
                )
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (scope, channel_id, content),
            )

            await db.commit()

    async def mark_executed(self, intent_id: UUID, order_id: str) -> None:
        await self._set_terminal(intent_id, IntentStatus.EXECUTED, bybit_order_id=order_id)

    async def mark_failed(self, intent_id: UUID, error: str) -> None:
        await self._set_terminal(intent_id, IntentStatus.FAILED, error=error[:2000])

    async def _set_terminal(
        self,
        intent_id: UUID,
        status: IntentStatus,
        *,
        bybit_order_id: str | None = None,
        error: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """UPDATE intents
                   SET status = ?, bybit_order_id = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE intent_id = ?""",
                (status.value, bybit_order_id, error, str(intent_id)),
            )
            await db.commit()
