from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

import aiosqlite

from ..domain import (
    ApprovalMode,
    ExecutionPlan,
    PositionActionIntent,
    SourceMessage,
    TradingIntent,
)
from ._connection import _Store, sqlite_connection


class SourceRepositoryImpl(_Store):
    async def record_source_decision(
        self,
        source: SourceMessage,
        claim_token: str,
        payload_json: str,
    ) -> bool:
        """Retain the owning attempt's decision independently of the model cache."""
        async with sqlite_connection(self._database_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO source_decisions(
                    channel_id, message_id, claim_token, attempt_count, payload_json
                )
                SELECT channel_id, message_id, claim_token, attempt_count, ?
                FROM source_messages
                WHERE channel_id = ? AND message_id = ?
                  AND status = 'PROCESSING' AND claim_token = ?
                ON CONFLICT(channel_id, message_id, claim_token) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (payload_json, source.channel_id, source.message_id, claim_token),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def get_retryable_source_message_ids(
        self,
        channel_id: int,
        *,
        lease_seconds: int,
        limit: int = 100,
    ) -> tuple[int, ...]:
        if lease_seconds <= 0 or limit <= 0:
            raise ValueError("Retry lease and limit must be positive")

        rows = await self._fetch(
            """
            SELECT message_id FROM source_messages
            WHERE channel_id = ? AND (
                status = 'FAILED'
                OR (status = 'UNAVAILABLE'
                    AND updated_at <= datetime('now', '-1 day'))
                OR (status = 'PROCESSING'
                    AND updated_at <= datetime('now', ?))
            )
            ORDER BY updated_at ASC, message_id ASC
            LIMIT ?
            """,
            (channel_id, f"-{lease_seconds} seconds", limit),
            many=True,
        )
        return tuple(row[0] for row in rows)

    async def defer_unavailable_source_messages(
        self,
        channel_id: int,
        message_ids: tuple[int, ...],
        *,
        lease_seconds: int,
    ) -> None:
        """Retain missing posts for a daily retry without stealing active claims."""
        if lease_seconds <= 0:
            raise ValueError("Retry lease must be positive")
        async with sqlite_connection(self._database_path) as db:
            await db.executemany(
                """
                UPDATE source_messages
                SET status = 'UNAVAILABLE', claim_token = NULL,
                    last_error = 'Telegram post unavailable; automatic retry deferred for 24 hours',
                    updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND message_id = ? AND (
                    status = 'FAILED'
                    OR (status = 'UNAVAILABLE'
                        AND updated_at <= datetime('now', '-1 day'))
                    OR (status = 'PROCESSING'
                        AND updated_at <= datetime('now', ?))
                )
                """,
                [
                    (channel_id, message_id, f"-{lease_seconds} seconds")
                    for message_id in message_ids
                ],
            )
            await db.commit()

    async def claim_source(
        self,
        source: SourceMessage,
        *,
        lease_seconds: int,
    ) -> str | None:
        if lease_seconds <= 0:
            raise ValueError("Source processing lease must be positive")

        claim_token = uuid4().hex

        async with sqlite_connection(self._database_path) as db:
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
                    OR source_messages.status = 'UNAVAILABLE'
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
        *,
        reason: str | None = None,
    ) -> bool:
        async with sqlite_connection(self._database_path) as db:
            finished = await self._finish_source(
                db,
                source,
                claim_token,
                status="COMPLETED",
                error=reason[:2000] if reason else None,
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

        async with sqlite_connection(self._database_path) as db:
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
        *,
        planning_candidates: Sequence[tuple[TradingIntent, bool, datetime]] = (),
    ) -> bool:
        if planning_candidates and not (items or position_actions):
            source = planning_candidates[0][0].source
        else:
            source = self._validate_batch_inputs(items, position_actions)
        self._validate_planning_candidates(source, planning_candidates)

        async with sqlite_connection(self._database_path) as db:
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

                for record_id, kind in self._pending_delivery_rows(
                    items,
                    position_actions,
                ):
                    await db.execute(
                        "INSERT INTO manual_deliveries(record_id, kind) VALUES (?, ?)",
                        (record_id, kind),
                    )

                for intent, duplicate, deadline in planning_candidates:
                    await db.execute(
                        "INSERT INTO planning_candidates(intent_id,channel_id,message_id,"
                        "payload_json,duplicate_in_batch,deadline) VALUES (?,?,?,?,?,?)",
                        (
                            str(intent.intent_id),
                            source.channel_id,
                            source.message_id,
                            json.dumps(
                                intent.model_dump(mode="json")
                                | {"approval_mode": intent.approval_mode.value}
                            ),
                            int(duplicate),
                            deadline.isoformat(),
                        ),
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
    def _validate_planning_candidates(
        source: SourceMessage,
        candidates: Sequence[tuple[TradingIntent, bool, datetime]],
    ) -> None:
        for intent, _, deadline in candidates:
            if (intent.source.channel_id, intent.source.message_id) != (
                source.channel_id,
                source.message_id,
            ):
                raise ValueError("Planning candidate belongs to another source")
            if deadline.tzinfo is None:
                raise ValueError("Planning deadline must be timezone-aware")

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
    def _pending_delivery_rows(
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
                if intent.approval_mode in {ApprovalMode.MANUAL, ApprovalMode.AUTO}
            ),
            *(
                (str(action.action_id), "ACTION")
                for action in position_actions
                if action.approval_mode in {ApprovalMode.MANUAL, ApprovalMode.AUTO}
            ),
        ]

    async def reset_stale_processing_sources(
        self,
        lease_seconds: int,
    ) -> int:
        """Fail source messages stuck in PROCESSING beyond their lease."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        async with sqlite_connection(self._database_path) as db:
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
