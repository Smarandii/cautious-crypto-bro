from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from ..domain import ApprovalMode, IntentStatus
from ._connection import _Store, sqlite_connection
from .position_action import CONTRACT_REVIEW_PREFIX, EXPIRED_ACTION_ERROR


class ManualDeliveryRepositoryImpl(_Store):
    """Manual approvals, expired notices and shared delivery acknowledgements."""

    async def pending_manual_deliveries(self) -> tuple[tuple[UUID, str], ...]:
        rows = await self._fetch(
            """SELECT d.record_id, d.kind FROM manual_deliveries d
               LEFT JOIN intents i ON d.kind = 'OPEN' AND i.intent_id = d.record_id
               LEFT JOIN position_actions a ON d.kind = 'ACTION' AND a.action_id = d.record_id
               WHERE d.delivered_at IS NULL
                 AND (d.retry_after IS NULL OR d.retry_after <= CURRENT_TIMESTAMP)
                 AND (COALESCE(i.status, a.status) = 'PENDING'
                      OR (d.kind='ACTION' AND a.status='SKIPPED'
                          AND (a.error=? OR a.error LIKE ?)))
                 AND COALESCE(i.approval_mode, a.approval_mode) = 'MANUAL'
               ORDER BY COALESCE(i.created_at, a.created_at) LIMIT 100""",
            (EXPIRED_ACTION_ERROR, CONTRACT_REVIEW_PREFIX + "%"),
            many=True,
        )
        return tuple((UUID(row[0]), row[1]) for row in rows)

    async def claim_manual_delivery(self, record_id: UUID) -> str | None:
        token = str(uuid4())
        async with sqlite_connection(self._database_path) as db:
            cursor = await db.execute(
                """UPDATE manual_deliveries SET claim_token = ?,
                   retry_after = datetime('now', '+5 minutes')
                   WHERE record_id = ? AND delivered_at IS NULL
                     AND (retry_after IS NULL OR retry_after <= CURRENT_TIMESTAMP)""",
                (token, str(record_id)),
            )
            await db.commit()
            return token if cursor.rowcount == 1 else None

    async def finish_manual_delivery(
        self, record_id: UUID, token: str, *, delivered: bool
    ) -> None:
        async with sqlite_connection(self._database_path) as db:
            await db.execute(
                """UPDATE manual_deliveries SET claim_token = NULL,
                   delivered_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                   retry_after = datetime('now', '+30 seconds')
                   WHERE record_id = ? AND claim_token = ?""",
                (delivered, str(record_id), token),
            )
            await db.commit()

    async def reconcile_manual_deliveries(
        self,
        *,
        stale_after_seconds: float = 86400,
    ) -> tuple[int, int]:
        """Re-queue recent missing manual deliveries; fail stale orphans."""
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")

        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)

        async with sqlite_connection(self._database_path) as db:
            cursor = await db.execute(
                """
                SELECT i.intent_id, 'OPEN', i.created_at
                FROM intents i
                LEFT JOIN manual_deliveries d ON d.record_id = i.intent_id
                WHERE i.status = ?
                  AND i.approval_mode = ?
                  AND d.record_id IS NULL
                UNION ALL
                SELECT a.action_id, 'ACTION', a.created_at
                FROM position_actions a
                LEFT JOIN manual_deliveries d ON d.record_id = a.action_id
                WHERE a.status = ?
                  AND a.approval_mode = ?
                  AND d.record_id IS NULL
                """,
                (
                    IntentStatus.PENDING.value,
                    ApprovalMode.MANUAL.value,
                    IntentStatus.PENDING.value,
                    ApprovalMode.MANUAL.value,
                ),
            )
            orphaned = await cursor.fetchall()

            requeued = 0
            failed = 0
            for record_id, kind, created_at in orphaned:
                table = "intents" if kind == "OPEN" else "position_actions"
                id_column = "intent_id" if kind == "OPEN" else "action_id"
                # A callback or another recovery can change the row after selection.
                still_orphaned = f"""
                    {id_column} = ? AND status = ? AND approval_mode = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM manual_deliveries WHERE record_id = {table}.{id_column}
                    )
                """
                orphan_params = (
                    record_id,
                    IntentStatus.PENDING.value,
                    ApprovalMode.MANUAL.value,
                )
                if datetime.fromisoformat(created_at) < cutoff:
                    cursor = await db.execute(
                        f"""
                        UPDATE {table}
                        SET
                            status = ?,
                            error = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE {still_orphaned}
                        """,
                        (
                            IntentStatus.FAILED.value,
                            "Stale manual approval request; delivery queue was lost",
                            *orphan_params,
                        ),
                    )
                    failed += cursor.rowcount
                else:
                    cursor = await db.execute(
                        f"""
                        INSERT INTO manual_deliveries(record_id, kind)
                        SELECT {id_column}, ? FROM {table} WHERE {still_orphaned}
                        """,
                        (kind, *orphan_params),
                    )
                    requeued += cursor.rowcount

            await db.commit()
            return requeued, failed
