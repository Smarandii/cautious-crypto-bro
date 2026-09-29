from __future__ import annotations

from uuid import UUID

import aiosqlite

from ..domain import (
    ApprovalMode,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    StrategyStatus,
    TradingIntent,
)
from ._connection import _Store
from ._shared import SharedRepositoryMixin


class PositionActionRepositoryImpl(SharedRepositoryMixin, _Store):
    async def get_position_action(
        self,
        action_id: UUID,
    ) -> PositionActionIntent | None:
        row = await self._fetch(
            """
            SELECT payload_json, status, approval_mode
            FROM position_actions
            WHERE action_id = ?
            """,
            (str(action_id),),
            row_factory=True,
        )

        if row is None:
            return None

        action = PositionActionIntent.model_validate_json(row["payload_json"])

        return action.model_copy(
            update={
                "status": IntentStatus(row["status"]),
                "approval_mode": ApprovalMode(row["approval_mode"]),
            }
        )

    async def claim_position_action_for_execution(
        self,
        action_id: UUID,
        user_id: int | None,
        *,
        expected_approval_mode: ApprovalMode | None = None,
    ) -> bool:
        return await self._transition_pending(
            table="position_actions",
            id_column="action_id",
            record_id=action_id,
            status=IntentStatus.EXECUTING,
            user_id=user_id,
            expected_approval_mode=expected_approval_mode,
        )

    async def mark_position_action_skipped(
        self,
        action_id: UUID,
        user_id: int,
    ) -> bool:
        return await self._transition_pending(
            table="position_actions",
            id_column="action_id",
            record_id=action_id,
            status=IntentStatus.SKIPPED,
            user_id=user_id,
        )

    async def complete_position_action(
        self,
        action: PositionActionIntent,
        order_id: str,
    ) -> None:
        """Commit a confirmed action and its strategy transition atomically."""
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    UPDATE position_actions
                    SET status = ?, bybit_order_id = ?, error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE action_id = ?
                    """,
                    (
                        IntentStatus.EXECUTED.value,
                        order_id,
                        str(action.action_id),
                    ),
                )

                if action.action is PositionActionType.REDUCE:
                    await db.execute(
                        """
                        UPDATE position_strategies
                        SET entry_frozen = 1, rebalance_needed = 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE symbol = ? AND status NOT IN (?, ?)
                        """,
                        (
                            action.symbol,
                            StrategyStatus.CLOSED.value,
                            StrategyStatus.MANUAL_OVERRIDE.value,
                        ),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE position_strategies
                        SET status = ?, entry_frozen = 1, rebalance_needed = 0,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE symbol = ? AND status NOT IN (?, ?, ?)
                        """,
                        (
                            StrategyStatus.CLOSING.value,
                            action.symbol,
                            StrategyStatus.CLOSED.value,
                            StrategyStatus.MANUAL_OVERRIDE.value,
                            StrategyStatus.UNCERTAIN.value,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def mark_position_action_failed(
        self,
        action_id: UUID,
        error: str,
    ) -> None:
        await self._mark_failed(
            table="position_actions",
            id_column="action_id",
            record_id=action_id,
            error=error,
        )

    async def mark_position_action_uncertain(
        self,
        action_id: UUID,
        symbol: str,
        order_id: str | None,
        error: str,
    ) -> None:
        """Persist ambiguous execution and quarantine exposure in one commit."""
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    UPDATE position_actions
                    SET status = ?, bybit_order_id = ?, error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE action_id = ?
                    """,
                    (
                        IntentStatus.UNCERTAIN.value,
                        order_id,
                        error[:2000],
                        str(action_id),
                    ),
                )
                await db.execute(
                    """
                    UPDATE position_strategies
                    SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE symbol = ? AND status IN (?, ?, ?, ?)
                    """,
                    (
                        StrategyStatus.UNCERTAIN.value,
                        symbol.upper(),
                        StrategyStatus.ENTERING.value,
                        StrategyStatus.OPEN_RISK.value,
                        StrategyStatus.PROFIT_PROTECTED.value,
                        StrategyStatus.CLOSING.value,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def get_recent_executed_closes(
        self,
        *,
        limit: int = 100,
    ) -> tuple[PositionActionIntent, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")

        rows = await self._fetch(
            """
            SELECT payload_json, status
            FROM position_actions
            WHERE status = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (
                IntentStatus.EXECUTED.value,
                limit,
            ),
            many=True,
            row_factory=True,
        )

        actions = (
            PositionActionIntent.model_validate_json(row["payload_json"]).model_copy(
                update={"status": IntentStatus(row["status"])}
            )
            for row in rows
        )

        return tuple(
            action for action in actions if action.action is PositionActionType.CLOSE
        )

    async def entry_cancellation_targets(
        self, action: PositionActionIntent
    ) -> tuple[UUID, ...]:
        rows = await self._fetch(
            "SELECT payload_json FROM intents WHERE channel_id = ? AND status IN (?, ?, ?, ?, ?)",
            (
                action.source.channel_id,
                IntentStatus.EXECUTING.value,
                IntentStatus.FAILED.value,
                IntentStatus.PENDING.value,
                IntentStatus.EXECUTED.value,
                IntentStatus.UNCERTAIN.value,
            ),
            many=True,
        )
        intents = (TradingIntent.model_validate_json(row[0]) for row in rows)
        return tuple(
            i.intent_id
            for i in intents
            if i.symbol == action.symbol
            and i.source.published_at <= action.source.published_at
            and (action.expected_side is None or i.side is action.expected_side)
        )

    async def record_entry_cancellation(
        self,
        action: PositionActionIntent,
        targets: tuple[UUID, ...],
        *,
        complete: bool = False,
        flat: bool = False,
        error: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            for ident in targets:
                await db.execute(
                    "UPDATE intents SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE intent_id = ? AND status IN (?, ?)",
                    (
                        IntentStatus.SKIPPED.value,
                        "Entry withdrawn by source",
                        str(ident),
                        IntentStatus.PENDING.value,
                        IntentStatus.EXECUTING.value,
                    ),
                )
                await db.execute(
                    "UPDATE position_strategies SET entry_frozen = 1, rebalance_needed = 1, updated_at = CURRENT_TIMESTAMP WHERE strategy_id = ? AND status != ?",
                    (str(ident), StrategyStatus.CLOSED.value),
                )
                if complete and flat:
                    await db.execute(
                        "UPDATE position_strategies SET status = ?, rebalance_needed = 0, updated_at = CURRENT_TIMESTAMP WHERE strategy_id = ?",
                        (StrategyStatus.CLOSED.value, str(ident)),
                    )
            if complete or error is not None:
                await db.execute(
                    "UPDATE position_actions SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE action_id = ?",
                    (
                        IntentStatus.EXECUTED.value
                        if complete
                        else IntentStatus.UNCERTAIN.value,
                        error,
                        str(action.action_id),
                    ),
                )
            await db.commit()
