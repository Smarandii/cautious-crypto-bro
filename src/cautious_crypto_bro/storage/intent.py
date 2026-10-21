from __future__ import annotations

import json
from uuid import UUID

from ..domain import (
    ApprovalMode,
    ExecutionPlan,
    IntentStatus,
    StrategyStatus,
    TradingIntent,
)
from ._connection import _Store, sqlite_connection
from ._shared import SharedRepositoryMixin


class IntentRepositoryImpl(SharedRepositoryMixin, _Store):
    async def get_intent(
        self,
        intent_id: UUID,
    ) -> TradingIntent | None:
        row = await self._fetch(
            """
            SELECT payload_json, status, approval_mode
            FROM intents
            WHERE intent_id = ?
            """,
            (str(intent_id),),
            row_factory=True,
        )

        if row is None:
            return None

        intent = TradingIntent.model_validate_json(row["payload_json"])

        return intent.model_copy(
            update={
                "status": IntentStatus(row["status"]),
                "approval_mode": ApprovalMode(row["approval_mode"]),
            }
        )

    async def get_execution_plan(
        self,
        intent_id: UUID,
    ) -> ExecutionPlan | None:
        row = await self._fetch(
            """
            SELECT payload_json
            FROM execution_plans
            WHERE intent_id = ?
            """,
            (str(intent_id),),
        )

        if row is None:
            return None

        return ExecutionPlan.model_validate_json(row[0])

    async def update_execution_plan(
        self,
        plan: ExecutionPlan,
    ) -> None:
        async with sqlite_connection(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE execution_plans
                SET payload_json = ?
                WHERE intent_id = ?
                """,
                (
                    plan.model_dump_json(),
                    str(plan.intent_id),
                ),
            )

            if cursor.rowcount != 1:
                await db.rollback()

                raise RuntimeError("Execution plan no longer exists")

            await db.commit()

    async def claim_for_execution(
        self,
        intent_id: UUID,
        user_id: int | None,
        *,
        expected_approval_mode: ApprovalMode | None = None,
    ) -> bool:
        return await self._transition_pending(
            table="intents",
            id_column="intent_id",
            record_id=intent_id,
            status=IntentStatus.EXECUTING,
            user_id=user_id,
            expected_approval_mode=expected_approval_mode,
        )

    async def mark_skipped(
        self,
        intent_id: UUID,
        user_id: int,
    ) -> bool:
        return await self._transition_pending(
            table="intents",
            id_column="intent_id",
            record_id=intent_id,
            status=IntentStatus.SKIPPED,
            user_id=user_id,
        )

    async def mark_executed(
        self,
        intent_id: UUID,
        order_ids: tuple[str, ...],
    ) -> None:
        async with sqlite_connection(self._database_path) as db:
            await db.execute(
                """
                UPDATE intents
                SET
                    status = ?,
                    error = NULL,
                    updated_at =
                        CURRENT_TIMESTAMP
                WHERE intent_id = ?
                """,
                (
                    IntentStatus.EXECUTED.value,
                    str(intent_id),
                ),
            )

            await db.execute(
                """
                UPDATE execution_plans
                SET bybit_order_ids_json = ?
                WHERE intent_id = ?
                """,
                (
                    json.dumps(list(order_ids)),
                    str(intent_id),
                ),
            )

            await db.commit()

    async def mark_failed(
        self,
        intent_id: UUID,
        error: str,
    ) -> None:
        await self._mark_failed(
            table="intents",
            id_column="intent_id",
            record_id=intent_id,
            error=error,
        )

    async def record_entry_failure(
        self,
        intent_id: UUID,
        error: str,
        *,
        strategy_status: StrategyStatus | None,
    ) -> None:
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE intents SET status = ?, error = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE intent_id = ? AND status = ?",
                    (
                        IntentStatus.FAILED.value,
                        error[:2000],
                        str(intent_id),
                        IntentStatus.EXECUTING.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Entry execution claim no longer exists")
                if strategy_status is not None:
                    cursor = await db.execute(
                        "UPDATE position_strategies SET status = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE strategy_id = ?",
                        (strategy_status.value, str(intent_id)),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("Entry strategy no longer exists")
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def get_recent_source_intents(
        self,
        channel_id: int,
        *,
        limit: int = 50,
    ) -> tuple[TradingIntent, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")

        rows = await self._fetch(
            """
            SELECT payload_json, status
            FROM intents
            WHERE
                channel_id = ?
                AND status IN (?, ?, ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (
                channel_id,
                IntentStatus.PENDING.value,
                IntentStatus.EXECUTING.value,
                IntentStatus.EXECUTED.value,
                limit,
            ),
            many=True,
            row_factory=True,
        )

        return tuple(
            TradingIntent.model_validate_json(row["payload_json"]).model_copy(
                update={"status": IntentStatus(row["status"])}
            )
            for row in rows
        )
