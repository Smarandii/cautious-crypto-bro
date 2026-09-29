from __future__ import annotations

from uuid import UUID

import aiosqlite

from ..domain import (
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    StrategyStatus,
)
from ._connection import _Store
from ._shared import SharedRepositoryMixin


class RecoveryRepositoryImpl(SharedRepositoryMixin, _Store):
    async def quarantine_interrupted_executions(
        self,
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
        """Quarantine interrupted records; lifecycle actions also quarantine strategy."""
        error = (
            "Process restarted during execution; exchange outcome is unknown; "
            "not retried automatically"
        )
        managed_statuses = (
            StrategyStatus.ENTERING.value,
            StrategyStatus.OPEN_RISK.value,
            StrategyStatus.PROFIT_PROTECTED.value,
            StrategyStatus.CLOSING.value,
        )

        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                # Action symbols must be captured before status transitions,
                # in the same transaction as the strategy quarantine.
                cursor = await db.execute(
                    "SELECT payload_json FROM position_actions WHERE status = ?",
                    (IntentStatus.EXECUTING.value,),
                )
                action_symbols = {
                    action.symbol
                    for row in await cursor.fetchall()
                    if (
                        action := PositionActionIntent.model_validate_json(row[0])
                    ).action
                    is not PositionActionType.CANCEL_ENTRIES
                }
                intent_ids = await self._quarantine_executing_records(
                    db,
                    table="intents",
                    id_column="intent_id",
                    error=error,
                )
                action_ids = await self._quarantine_executing_records(
                    db,
                    table="position_actions",
                    id_column="action_id",
                    error=error,
                )

                if action_symbols:
                    await db.executemany(
                        """
                        UPDATE position_strategies
                        SET status = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE symbol = ? AND status IN (?, ?, ?, ?)
                        """,
                        [
                            (
                                StrategyStatus.UNCERTAIN.value,
                                symbol,
                                *managed_statuses,
                            )
                            for symbol in sorted(action_symbols)
                        ],
                    )

                await db.commit()
            except Exception:
                await db.rollback()
                raise

        return intent_ids, action_ids

    async def get_pending_auto_intent_ids(
        self,
        *,
        limit: int = 100,
    ) -> tuple[UUID, ...]:
        return await self._get_pending_auto_ids(
            table="intents",
            id_column="intent_id",
            limit=limit,
        )

    async def get_pending_auto_action_ids(
        self,
        *,
        limit: int = 100,
    ) -> tuple[UUID, ...]:
        return await self._get_pending_auto_ids(
            table="position_actions",
            id_column="action_id",
            limit=limit,
        )
