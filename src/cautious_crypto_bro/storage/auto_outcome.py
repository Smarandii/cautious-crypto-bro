from __future__ import annotations

import json
from uuid import UUID

from ..domain import (
    ApprovalMode,
    ExecutionPlan,
    IntentExecutionOutcome,
    IntentStatus,
    PositionActionExecutionOutcome,
    PositionActionIntent,
    TradingIntent,
)
from ._connection import _Store, sqlite_connection


class AutoOutcomeRepositoryImpl(_Store):
    """The existing delivery table also holds AUTO terminal-notice reservations."""

    async def ensure_auto_outcome_delivery(self, record_id: UUID, kind: str) -> None:
        table, column = self._outcome_table(kind)
        async with sqlite_connection(self._database_path) as db:
            await db.execute(
                f"""INSERT OR IGNORE INTO manual_deliveries(record_id, kind)
                    SELECT {column}, ? FROM {table}
                    WHERE {column} = ? AND approval_mode = 'AUTO'
                      AND status IN ('PENDING', 'EXECUTING')""",
                (kind, str(record_id)),
            )
            await db.commit()

    async def pending_auto_outcome_deliveries(self) -> tuple[tuple[UUID, str], ...]:
        rows = await self._fetch(
            """SELECT d.record_id, d.kind FROM manual_deliveries d
               LEFT JOIN intents i ON d.kind = 'OPEN' AND i.intent_id = d.record_id
               LEFT JOIN position_actions a ON d.kind = 'ACTION' AND a.action_id = d.record_id
               WHERE d.delivered_at IS NULL
                 AND (d.retry_after IS NULL OR d.retry_after <= CURRENT_TIMESTAMP)
                 AND COALESCE(i.approval_mode, a.approval_mode) = 'AUTO'
                 AND COALESCE(i.status, a.status) IN ('EXECUTED', 'FAILED', 'UNCERTAIN', 'SKIPPED')
                 AND datetime(COALESCE(i.updated_at, a.updated_at)) <= datetime('now', '-30 seconds')
               ORDER BY COALESCE(d.retry_after, ''), COALESCE(i.created_at, a.created_at), d.record_id
               LIMIT 100""",
            many=True,
        )
        return tuple((UUID(row[0]), row[1]) for row in rows)

    async def get_recorded_auto_outcome(
        self, record_id: UUID, kind: str
    ) -> IntentExecutionOutcome | PositionActionExecutionOutcome | None:
        table, column = self._outcome_table(kind)
        row = await self._fetch(
            f"""SELECT * FROM {table} WHERE {column} = ?
                AND approval_mode = 'AUTO'
                AND status IN ('EXECUTED', 'FAILED', 'UNCERTAIN', 'SKIPPED')""",
            (str(record_id),),
            row_factory=True,
        )
        if row is None:
            return None
        status = IntentStatus(row["status"])
        message = (
            f"Recovered durable execution record {record_id}; not a live confirmation."
        )
        if kind == "OPEN":
            plan_row = await self._fetch(
                "SELECT payload_json, bybit_order_ids_json FROM execution_plans WHERE intent_id = ?",
                (str(record_id),),
            )
            return IntentExecutionOutcome(
                status=status,
                recovered=True,
                message=f"{row['error']}\n{message}" if row["error"] else message,
                intent=TradingIntent.model_validate_json(
                    row["payload_json"]
                ).model_copy(
                    update={"status": status, "approval_mode": ApprovalMode.AUTO}
                ),
                plan=ExecutionPlan.model_validate_json(plan_row[0])
                if plan_row
                else None,
                order_ids=tuple(json.loads(plan_row[1] or "[]")) if plan_row else (),
            )
        message += (
            f" Recorded order reference: {row['bybit_order_id'] or 'unavailable'}. "
            "Exact fill details are unavailable; check Bybit."
        )
        return PositionActionExecutionOutcome(
            status=status,
            recovered=True,
            message=f"{row['error']}\n{message}" if row["error"] else message,
            action=PositionActionIntent.model_validate_json(
                row["payload_json"]
            ).model_copy(update={"status": status, "approval_mode": ApprovalMode.AUTO}),
        )

    @staticmethod
    def _outcome_table(kind: str) -> tuple[str, str]:
        if kind == "OPEN":
            return "intents", "intent_id"
        if kind == "ACTION":
            return "position_actions", "action_id"
        raise ValueError("Unknown delivery kind")
