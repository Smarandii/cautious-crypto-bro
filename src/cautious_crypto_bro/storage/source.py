from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import aiosqlite

from ..domain import (
    ApprovalMode,
    ExecutionPlan,
    PositionActionIntent,
    SourceMessage,
    TradingIntent,
)
from ._connection import _Store


class SourceRepositoryImpl(_Store):
    async def claim_source(
        self,
        source: SourceMessage,
        *,
        lease_seconds: int,
    ) -> str | None:
        if lease_seconds <= 0:
            raise ValueError("Source processing lease must be positive")

        claim_token = uuid4().hex

        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO source_messages(
                    channel_id,
                    message_id,
                    payload_json,
                    status,
                    attempt_count,
                    last_error,
                    claim_token,
                    updated_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    'PROCESSING',
                    1,
                    NULL,
                    ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(
                    channel_id,
                    message_id
                )
                DO UPDATE SET
                    payload_json =
                        excluded.payload_json,
                    status = 'PROCESSING',
                    attempt_count =
                        source_messages.attempt_count
                        + 1,
                    last_error = NULL,
                    claim_token =
                        excluded.claim_token,
                    updated_at =
                        CURRENT_TIMESTAMP
                WHERE
                    source_messages.status =
                        'FAILED'
                    OR (
                        source_messages.status =
                            'PROCESSING'
                        AND
                        source_messages.updated_at
                            <= datetime(
                                'now',
                                ?
                            )
                    )
                """,
                (
                    source.channel_id,
                    source.message_id,
                    source.model_dump_json(),
                    claim_token,
                    (f"-{lease_seconds} seconds"),
                ),
            )

            await db.commit()

            if cursor.rowcount != 1:
                return None

            return claim_token

    async def mark_source_completed(
        self,
        source: SourceMessage,
        claim_token: str,
    ) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            finished = await self._finish_source(
                db,
                source,
                claim_token,
                status="COMPLETED",
                error=None,
            )
            await db.commit()
            return finished

    async def mark_source_failed(
        self,
        source: SourceMessage,
        claim_token: str,
        error: str,
    ) -> bool:
        error = (error.strip() or "unknown processing failure")[:2000]

        async with aiosqlite.connect(self._database_path) as db:
            finished = await self._finish_source(
                db,
                source,
                claim_token,
                status="FAILED",
                error=error,
            )
            await db.commit()
            return finished

    async def create_signal_batch_and_complete_source(
        self,
        items: Sequence[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ],
        position_actions: Sequence[PositionActionIntent],
        claim_token: str,
    ) -> bool:
        source = self._validate_batch_inputs(items, position_actions)

        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")

            try:
                cursor = await db.execute(
                    """
                    SELECT 1
                    FROM source_messages
                    WHERE
                        channel_id = ?
                        AND message_id = ?
                        AND status = 'PROCESSING'
                        AND claim_token = ?
                    """,
                    (
                        source.channel_id,
                        source.message_id,
                        claim_token,
                    ),
                )

                if await cursor.fetchone() is None:
                    await db.rollback()
                    return False

                for intent, plan in items:
                    await self._insert_intent_with_plan(
                        db,
                        intent,
                        plan,
                    )

                for action in position_actions:
                    await self._insert_position_action(
                        db,
                        action,
                    )

                for record_id, kind in self._pending_manual_rows(
                    items,
                    position_actions,
                ):
                    await db.execute(
                        "INSERT INTO manual_deliveries(record_id, kind) VALUES (?, ?)",
                        (record_id, kind),
                    )

                if not await self._finish_source(
                    db,
                    source,
                    claim_token,
                    status="COMPLETED",
                    error=None,
                ):
                    await db.rollback()
                    return False

                await db.commit()
                return True

            except Exception:
                await db.rollback()
                raise

    @staticmethod
    def _validate_batch_inputs(
        items: Sequence[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ],
        position_actions: Sequence[PositionActionIntent],
    ) -> SourceMessage:
        sources = [intent.source for intent, _ in items]
        sources.extend(action.source for action in position_actions)

        if not sources:
            raise ValueError("At least one signal output is required")

        source = sources[0]
        source_key = (
            source.channel_id,
            source.message_id,
        )

        for candidate_source in sources:
            if (
                candidate_source.channel_id,
                candidate_source.message_id,
            ) != source_key:
                raise ValueError(
                    "All signal outputs in a batch "
                    "must belong to the same Telegram post"
                )

        for intent, plan in items:
            if plan.intent_id != intent.intent_id:
                raise ValueError("ExecutionPlan intent_id does not match TradingIntent")

        return source

    @staticmethod
    def _pending_manual_rows(
        items: Sequence[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ],
        position_actions: Sequence[PositionActionIntent],
    ) -> list[tuple[str, str]]:
        return [
            *(
                (str(intent.intent_id), "OPEN")
                for intent, _ in items
                if intent.approval_mode is ApprovalMode.MANUAL
            ),
            *(
                (str(action.action_id), "ACTION")
                for action in position_actions
                if action.approval_mode is ApprovalMode.MANUAL
            ),
        ]

    async def reset_stale_processing_sources(
        self,
        lease_seconds: int,
    ) -> int:
        """Fail source messages stuck in PROCESSING beyond their lease."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE source_messages
                SET
                    status = ?,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    status = 'PROCESSING'
                    AND updated_at <= datetime('now', ?)
                """,
                (
                    "FAILED",
                    "Source processing lease expired during previous run",
                    f"-{lease_seconds} seconds",
                ),
            )
            await db.commit()
            return cursor.rowcount

    @staticmethod
    async def _finish_source(
        db: aiosqlite.Connection,
        source: SourceMessage,
        claim_token: str,
        *,
        status: str,
        error: str | None,
    ) -> bool:
        cursor = await db.execute(
            """
            UPDATE source_messages
            SET
                status = ?,
                last_error = ?,
                claim_token = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE
                channel_id = ?
                AND message_id = ?
                AND status = 'PROCESSING'
                AND claim_token = ?
            """,
            (
                status,
                error,
                source.channel_id,
                source.message_id,
                claim_token,
            ),
        )

        return cursor.rowcount == 1

    async def _insert_intent_with_plan(
        self,
        db: aiosqlite.Connection,
        intent: TradingIntent,
        plan: ExecutionPlan,
    ) -> None:
        if plan.intent_id != intent.intent_id:
            raise ValueError("ExecutionPlan intent_id does not match TradingIntent")

        now = intent.created_at.isoformat()

        await db.execute(
            """
            INSERT INTO intents(
                intent_id,
                channel_id,
                message_id,
                payload_json,
                status,
                approval_mode,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(intent.intent_id),
                intent.source.channel_id,
                intent.source.message_id,
                intent.model_dump_json(),
                intent.status.value,
                intent.approval_mode.value,
                now,
                now,
            ),
        )

        await db.execute(
            """
            INSERT INTO execution_plans(
                intent_id,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                str(intent.intent_id),
                plan.model_dump_json(),
                plan.created_at.isoformat(),
            ),
        )

    async def _insert_position_action(
        self,
        db: aiosqlite.Connection,
        action: PositionActionIntent,
    ) -> None:
        now = action.created_at.isoformat()

        await db.execute(
            """
            INSERT INTO position_actions(
                action_id,
                channel_id,
                message_id,
                payload_json,
                status,
                approval_mode,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(action.action_id),
                action.source.channel_id,
                action.source.message_id,
                action.model_dump_json(),
                action.status.value,
                action.approval_mode.value,
                now,
                now,
            ),
        )
