from __future__ import annotations

from uuid import UUID

import aiosqlite

from ..domain import ApprovalMode, IntentStatus
from ._connection import _Store


class SharedRepositoryMixin(_Store):
    async def _transition_pending(
        self,
        *,
        table: str,
        id_column: str,
        record_id: UUID,
        status: IntentStatus,
        user_id: int | None,
        expected_approval_mode: ApprovalMode | None = None,
    ) -> bool:
        approval_filter = ""
        parameters: list[object] = [
            status.value,
            user_id,
            str(record_id),
            IntentStatus.PENDING.value,
        ]

        if expected_approval_mode is not None:
            approval_filter = " AND approval_mode = ?"
            parameters.append(expected_approval_mode.value)

        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                f"""
                UPDATE {table}
                SET
                    status = ?,
                    decision_user_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    {id_column} = ?
                    AND status = ?
                    {approval_filter}
                """,
                parameters,
            )
            await db.commit()
            return cursor.rowcount == 1

    async def _mark_failed(
        self,
        *,
        table: str,
        id_column: str,
        record_id: UUID,
        error: str,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                f"""
                UPDATE {table}
                SET
                    status = ?,
                    error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE {id_column} = ?
                """,
                (
                    IntentStatus.FAILED.value,
                    error[:2000],
                    str(record_id),
                ),
            )
            await db.commit()

    async def _get_pending_auto_ids(
        self,
        *,
        table: str,
        id_column: str,
        limit: int,
    ) -> tuple[UUID, ...]:
        rows = await self._fetch(
            f"""
            SELECT {id_column}
            FROM {table}
            WHERE approval_mode = ? AND status = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (
                ApprovalMode.AUTO.value,
                IntentStatus.PENDING.value,
                limit,
            ),
            many=True,
        )

        return tuple(UUID(row[0]) for row in rows)

    @staticmethod
    async def _quarantine_executing_records(
        db: aiosqlite.Connection,
        *,
        table: str,
        id_column: str,
        error: str,
    ) -> tuple[UUID, ...]:
        cursor = await db.execute(
            f"SELECT {id_column} FROM {table} WHERE status = ?",
            (IntentStatus.EXECUTING.value,),
        )
        record_ids = tuple(UUID(row[0]) for row in await cursor.fetchall())
        await db.execute(
            f"""
            UPDATE {table}
            SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE status = ?
            """,
            (
                IntentStatus.UNCERTAIN.value,
                error,
                IntentStatus.EXECUTING.value,
            ),
        )
        return record_ids
