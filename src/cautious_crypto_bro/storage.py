from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import aiosqlite

from .domain import (
    ExecutionPlan,
    ExecutionPolicy,
    ExitPolicy,
    IntentStatus,
    SourceMessage,
    TradingIntent,
)


class IntentStore:
    def __init__(
        self,
        database_path: Path,
    ) -> None:
        self._database_path = (
            database_path
        )

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        async with aiosqlite.connect(
            self._database_path
        ) as db:
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
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    CHECK(
                        (
                            scope = 'global'
                            AND channel_id IS NULL
                        )
                        OR
                        (
                            scope = 'channel'
                            AND channel_id IS NOT NULL
                        )
                    )
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    ux_signal_guidance_global
                ON signal_guidance(scope)
                WHERE scope = 'global';

                CREATE UNIQUE INDEX IF NOT EXISTS
                    ux_signal_guidance_channel
                ON signal_guidance(channel_id)
                WHERE scope = 'channel';

                CREATE TABLE IF NOT EXISTS execution_policy (
                    id INTEGER PRIMARY KEY
                        CHECK(id = 1),
                    trading_capital_usdt TEXT NOT NULL,
                    risk_per_trade_pct TEXT NOT NULL,
                    range_order_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS execution_exit_policy (
                    id INTEGER PRIMARY KEY
                        CHECK(id = 1),
                    minimum_reward_bps TEXT NOT NULL,
                    basic_r_multiple TEXT NOT NULL,
                    basic_close_pct TEXT NOT NULL,
                    medium_r_multiple TEXT NOT NULL,
                    medium_close_pct TEXT NOT NULL,
                    high_r_multiple TEXT NOT NULL,
                    high_close_pct TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS execution_plans (
                    intent_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    bybit_order_ids_json TEXT,
                    created_at TEXT NOT NULL
                );

                INSERT OR IGNORE INTO execution_policy(
                    id,
                    trading_capital_usdt,
                    risk_per_trade_pct,
                    range_order_count
                )
                VALUES (
                    1,
                    '6800',
                    '1',
                    3
                );

                INSERT OR IGNORE INTO execution_exit_policy(
                    id,
                    minimum_reward_bps,
                    basic_r_multiple,
                    basic_close_pct,
                    medium_r_multiple,
                    medium_close_pct,
                    high_r_multiple,
                    high_close_pct
                )
                VALUES (
                    1,
                    '20',
                    '0.5',
                    '25',
                    '1',
                    '35',
                    '2',
                    '40'
                );
                """
            )

            await db.commit()

    async def save_source(
        self,
        source: SourceMessage,
    ) -> bool:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO source_messages(
                    channel_id,
                    message_id,
                    payload_json
                )
                VALUES (?, ?, ?)
                """,
                (
                    source.channel_id,
                    source.message_id,
                    source.model_dump_json(),
                ),
            )

            await db.commit()
            return cursor.rowcount == 1

    async def create_intent_with_plan(
        self,
        intent: TradingIntent,
        plan: ExecutionPlan,
    ) -> None:
        if plan.intent_id != intent.intent_id:
            raise ValueError(
                "ExecutionPlan intent_id "
                "does not match TradingIntent"
            )

        now = intent.created_at.isoformat()

        async with aiosqlite.connect(
            self._database_path
        ) as db:
            await db.execute(
                """
                INSERT INTO intents(
                    intent_id,
                    channel_id,
                    message_id,
                    payload_json,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
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

            await db.commit()

    async def get_intent(
        self,
        intent_id: UUID,
    ) -> TradingIntent | None:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            db.row_factory = (
                aiosqlite.Row
            )

            cursor = await db.execute(
                """
                SELECT payload_json, status
                FROM intents
                WHERE intent_id = ?
                """,
                (str(intent_id),),
            )

            row = await cursor.fetchone()

        if row is None:
            return None

        intent = (
            TradingIntent.model_validate_json(
                row["payload_json"]
            )
        )

        return intent.model_copy(
            update={
                "status": IntentStatus(
                    row["status"]
                )
            }
        )

    async def get_execution_plan(
        self,
        intent_id: UUID,
    ) -> ExecutionPlan | None:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            cursor = await db.execute(
                """
                SELECT payload_json
                FROM execution_plans
                WHERE intent_id = ?
                """,
                (str(intent_id),),
            )

            row = await cursor.fetchone()

        if row is None:
            return None

        return (
            ExecutionPlan.model_validate_json(
                row[0]
            )
        )

    async def claim_for_execution(
        self,
        intent_id: UUID,
        user_id: int,
    ) -> bool:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            cursor = await db.execute(
                """
                UPDATE intents
                SET
                    status = ?,
                    decision_user_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    intent_id = ?
                    AND status = ?
                """,
                (
                    IntentStatus.EXECUTING.value,
                    user_id,
                    str(intent_id),
                    IntentStatus.PENDING.value,
                ),
            )

            await db.commit()
            return cursor.rowcount == 1

    async def mark_skipped(
        self,
        intent_id: UUID,
        user_id: int,
    ) -> bool:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            cursor = await db.execute(
                """
                UPDATE intents
                SET
                    status = ?,
                    decision_user_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    intent_id = ?
                    AND status = ?
                """,
                (
                    IntentStatus.SKIPPED.value,
                    user_id,
                    str(intent_id),
                    IntentStatus.PENDING.value,
                ),
            )

            await db.commit()
            return cursor.rowcount == 1

    async def get_guidance(
        self,
        channel_id: int,
    ) -> tuple[
        str | None,
        str | None,
    ]:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            cursor = await db.execute(
                """
                SELECT scope, content
                FROM signal_guidance
                WHERE
                    scope = 'global'
                    OR (
                        scope = 'channel'
                        AND channel_id = ?
                    )
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

        return (
            global_guidance,
            channel_guidance,
        )

    async def set_guidance(
        self,
        content: str,
        *,
        channel_id: int | None = None,
    ) -> None:
        content = content.strip()

        if not content:
            raise ValueError(
                "Guidance must not be empty"
            )

        scope = (
            "global"
            if channel_id is None
            else "channel"
        )

        async with aiosqlite.connect(
            self._database_path
        ) as db:
            if scope == "global":
                await db.execute(
                    """
                    DELETE FROM signal_guidance
                    WHERE scope = 'global'
                    """
                )
            else:
                await db.execute(
                    """
                    DELETE FROM signal_guidance
                    WHERE
                        scope = 'channel'
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
                VALUES (
                    ?,
                    ?,
                    ?,
                    CURRENT_TIMESTAMP
                )
                """,
                (
                    scope,
                    channel_id,
                    content,
                ),
            )

            await db.commit()

    async def get_execution_policy(
        self,
    ) -> ExecutionPolicy:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            cursor = await db.execute(
                """
                SELECT
                    trading_capital_usdt,
                    risk_per_trade_pct,
                    range_order_count
                FROM execution_policy
                WHERE id = 1
                """
            )

            policy_row = (
                await cursor.fetchone()
            )

            cursor = await db.execute(
                """
                SELECT
                    minimum_reward_bps,
                    basic_r_multiple,
                    basic_close_pct,
                    medium_r_multiple,
                    medium_close_pct,
                    high_r_multiple,
                    high_close_pct
                FROM execution_exit_policy
                WHERE id = 1
                """
            )

            exit_row = (
                await cursor.fetchone()
            )

        if policy_row is None:
            raise RuntimeError(
                "Execution policy "
                "is not initialized"
            )

        if exit_row is None:
            raise RuntimeError(
                "Execution exit policy "
                "is not initialized"
            )

        return ExecutionPolicy(
            trading_capital_usdt=(
                policy_row[0]
            ),
            risk_per_trade_pct=(
                policy_row[1]
            ),
            range_order_count=(
                policy_row[2]
            ),
            exit_policy=ExitPolicy(
                minimum_reward_bps=(
                    exit_row[0]
                ),
                basic_r_multiple=(
                    exit_row[1]
                ),
                basic_close_pct=(
                    exit_row[2]
                ),
                medium_r_multiple=(
                    exit_row[3]
                ),
                medium_close_pct=(
                    exit_row[4]
                ),
                high_r_multiple=(
                    exit_row[5]
                ),
                high_close_pct=(
                    exit_row[6]
                ),
            ),
        )

    async def set_execution_policy(
        self,
        policy: ExecutionPolicy,
    ) -> None:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            await db.execute(
                """
                INSERT INTO execution_policy(
                    id,
                    trading_capital_usdt,
                    risk_per_trade_pct,
                    range_order_count,
                    updated_at
                )
                VALUES (
                    1,
                    ?,
                    ?,
                    ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(id) DO UPDATE SET
                    trading_capital_usdt =
                        excluded.trading_capital_usdt,
                    risk_per_trade_pct =
                        excluded.risk_per_trade_pct,
                    range_order_count =
                        excluded.range_order_count,
                    updated_at =
                        CURRENT_TIMESTAMP
                """,
                (
                    str(
                        policy.trading_capital_usdt
                    ),
                    str(
                        policy.risk_per_trade_pct
                    ),
                    policy.range_order_count,
                ),
            )

            exit_policy = (
                policy.exit_policy
            )

            await db.execute(
                """
                INSERT INTO execution_exit_policy(
                    id,
                    minimum_reward_bps,
                    basic_r_multiple,
                    basic_close_pct,
                    medium_r_multiple,
                    medium_close_pct,
                    high_r_multiple,
                    high_close_pct,
                    updated_at
                )
                VALUES (
                    1,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(id) DO UPDATE SET
                    minimum_reward_bps =
                        excluded.minimum_reward_bps,
                    basic_r_multiple =
                        excluded.basic_r_multiple,
                    basic_close_pct =
                        excluded.basic_close_pct,
                    medium_r_multiple =
                        excluded.medium_r_multiple,
                    medium_close_pct =
                        excluded.medium_close_pct,
                    high_r_multiple =
                        excluded.high_r_multiple,
                    high_close_pct =
                        excluded.high_close_pct,
                    updated_at =
                        CURRENT_TIMESTAMP
                """,
                (
                    str(
                        exit_policy.minimum_reward_bps
                    ),
                    str(
                        exit_policy.basic_r_multiple
                    ),
                    str(
                        exit_policy.basic_close_pct
                    ),
                    str(
                        exit_policy.medium_r_multiple
                    ),
                    str(
                        exit_policy.medium_close_pct
                    ),
                    str(
                        exit_policy.high_r_multiple
                    ),
                    str(
                        exit_policy.high_close_pct
                    ),
                ),
            )

            await db.commit()

    async def mark_executed(
        self,
        intent_id: UUID,
        order_ids: tuple[str, ...],
    ) -> None:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
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
                    json.dumps(
                        list(order_ids)
                    ),
                    str(intent_id),
                ),
            )

            await db.commit()

    async def mark_failed(
        self,
        intent_id: UUID,
        error: str,
    ) -> None:
        await self._set_terminal(
            intent_id,
            IntentStatus.FAILED,
            error=error[:2000],
        )

    async def _set_terminal(
        self,
        intent_id: UUID,
        status: IntentStatus,
        *,
        error: str | None = None,
    ) -> None:
        async with aiosqlite.connect(
            self._database_path
        ) as db:
            await db.execute(
                """
                UPDATE intents
                SET
                    status = ?,
                    error = ?,
                    updated_at =
                        CURRENT_TIMESTAMP
                WHERE intent_id = ?
                """,
                (
                    status.value,
                    error,
                    str(intent_id),
                ),
            )

            await db.commit()
