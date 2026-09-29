from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import aiosqlite

from ..domain import ExecutionPlan, PositionStrategy, StrategyStatus
from ._connection import _Store
from ._plan_payload import load_strategy_plan


class PositionStrategyRepositoryImpl(_Store):
    async def ensure_position_strategy(
        self,
        plan: ExecutionPlan,
    ) -> None:
        if plan.strategy_version < 2:
            return

        state = PositionStrategy(
            strategy_id=plan.intent_id,
            symbol=plan.symbol,
            side=plan.side,
        )

        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO position_strategies(
                    strategy_id,
                    symbol,
                    side,
                    status,
                    entry_frozen,
                    base_position_qty,
                    last_position_qty,
                    last_avg_price,
                    tp1_done,
                    tp2_done,
                    tp3_done,
                    trailing_active,
                    exit_revision,
                    rebalance_needed,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?, ?, ?, ?, 0,
                    NULL, NULL, NULL,
                    0, 0, 0, 0, 0, 0,
                    ?, ?
                )
                """,
                (
                    str(state.strategy_id),
                    state.symbol,
                    state.side.value,
                    state.status.value,
                    state.created_at.isoformat(),
                    state.updated_at.isoformat(),
                ),
            )

            await db.commit()

    async def get_active_position_strategies(
        self,
    ) -> tuple[
        tuple[
            PositionStrategy,
            ExecutionPlan,
        ],
        ...,
    ]:
        rows = await self._fetch(
            """
            SELECT
                s.*,
                p.payload_json AS plan_json,
                i.payload_json AS intent_json
            FROM position_strategies AS s
            JOIN execution_plans AS p
              ON p.intent_id = s.strategy_id
            LEFT JOIN intents AS i ON i.intent_id = s.strategy_id
            WHERE s.status IN (?, ?, ?, ?, ?)
            ORDER BY s.created_at ASC
            """,
            (
                StrategyStatus.ENTERING.value,
                StrategyStatus.OPEN_RISK.value,
                StrategyStatus.PROFIT_PROTECTED.value,
                StrategyStatus.CLOSING.value,
                StrategyStatus.UNCERTAIN.value,
            ),
            many=True,
            row_factory=True,
        )

        return tuple(
            (
                self._position_strategy_from_row(row),
                load_strategy_plan(row["plan_json"], row["intent_json"]),
            )
            for row in rows
        )

    async def save_position_strategy(
        self,
        state: PositionStrategy,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE position_strategies
                SET
                    status = ?,
                    entry_frozen = ?,
                    base_position_qty = ?,
                    last_position_qty = ?,
                    last_avg_price = ?,
                    tp1_done = ?,
                    tp2_done = ?,
                    tp3_done = ?,
                    trailing_active = ?,
                    protected_stop_loss = ?,
                    trailing_distance = ?,
                    exit_revision = ?,
                    rebalance_needed = ?,
                    installing_exits = ?,
                    updated_at = ?
                WHERE strategy_id = ?
                """,
                (
                    state.status.value,
                    int(state.entry_frozen),
                    (
                        str(state.base_position_qty)
                        if (state.base_position_qty is not None)
                        else None
                    ),
                    (
                        str(state.last_position_qty)
                        if (state.last_position_qty is not None)
                        else None
                    ),
                    (
                        str(state.last_avg_price)
                        if (state.last_avg_price is not None)
                        else None
                    ),
                    int(state.tp1_done),
                    int(state.tp2_done),
                    int(state.tp3_done),
                    int(state.trailing_active),
                    (
                        str(state.protected_stop_loss)
                        if (state.protected_stop_loss is not None)
                        else None
                    ),
                    (
                        str(state.trailing_distance)
                        if (state.trailing_distance is not None)
                        else None
                    ),
                    state.exit_revision,
                    int(state.rebalance_needed),
                    int(state.installing_exits),
                    state.updated_at.isoformat(),
                    str(state.strategy_id),
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError("Position strategy no longer exists")

            await db.commit()

    async def set_position_strategy_status(
        self,
        strategy_id: UUID,
        status: StrategyStatus,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE position_strategies
                SET
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE strategy_id = ?
                """,
                (
                    status.value,
                    str(strategy_id),
                ),
            )

            await db.commit()

    async def request_strategy_rebalance(
        self,
        symbol: str,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE position_strategies
                SET
                    entry_frozen = 1,
                    rebalance_needed = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    symbol = ?
                    AND status NOT IN (?, ?)
                """,
                (
                    symbol.upper(),
                    StrategyStatus.CLOSED.value,
                    StrategyStatus.MANUAL_OVERRIDE.value,
                ),
            )

            await db.commit()

    async def request_strategy_close(
        self,
        symbol: str,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE position_strategies
                SET
                    status = ?,
                    entry_frozen = 1,
                    rebalance_needed = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    symbol = ?
                    AND status NOT IN (?, ?)
                """,
                (
                    StrategyStatus.CLOSING.value,
                    symbol.upper(),
                    StrategyStatus.CLOSED.value,
                    StrategyStatus.MANUAL_OVERRIDE.value,
                ),
            )

            await db.commit()

    @staticmethod
    def _position_strategy_from_row(
        row: aiosqlite.Row,
    ) -> PositionStrategy:
        def optional_decimal(
            name: str,
        ) -> Decimal | None:
            value = row[name]

            return Decimal(value) if value is not None else None

        return PositionStrategy(
            strategy_id=UUID(row["strategy_id"]),
            symbol=row["symbol"],
            side=row["side"],
            status=row["status"],
            entry_frozen=bool(row["entry_frozen"]),
            base_position_qty=(optional_decimal("base_position_qty")),
            last_position_qty=(optional_decimal("last_position_qty")),
            last_avg_price=(optional_decimal("last_avg_price")),
            tp1_done=bool(row["tp1_done"]),
            tp2_done=bool(row["tp2_done"]),
            tp3_done=bool(row["tp3_done"]),
            trailing_active=bool(row["trailing_active"]),
            protected_stop_loss=(optional_decimal("protected_stop_loss")),
            trailing_distance=(optional_decimal("trailing_distance")),
            exit_revision=int(row["exit_revision"]),
            rebalance_needed=bool(row["rebalance_needed"]),
            installing_exits=bool(row["installing_exits"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
