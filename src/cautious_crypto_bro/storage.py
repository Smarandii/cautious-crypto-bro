from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite

from .domain import (
    ExecutionPlan,
    ExecutionPolicy,
    ExitPolicy,
    IntentStatus,
    PositionActionIntent,
    SourceMessage,
    TradingIntent,
)

LATEST_SCHEMA_VERSION = 2


async def _source_message_columns(
    db: aiosqlite.Connection,
) -> set[str]:
    cursor = await db.execute("PRAGMA table_info(source_messages)")
    return {str(row[1]) for row in await cursor.fetchall()}


async def _migrate_to_v1(
    db: aiosqlite.Connection,
) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS source_messages (
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL
                DEFAULT 'COMPLETED',
            attempt_count INTEGER NOT NULL
                DEFAULT 1,
            last_error TEXT,
            claim_token TEXT,
            updated_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (channel_id, message_id)
        )
        """,
        """
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
        )
        """,
        """
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
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            ux_signal_guidance_global
        ON signal_guidance(scope)
        WHERE scope = 'global'
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            ux_signal_guidance_channel
        ON signal_guidance(channel_id)
        WHERE scope = 'channel'
        """,
        """
        CREATE TABLE IF NOT EXISTS execution_policy (
            id INTEGER PRIMARY KEY
                CHECK(id = 1),
            trading_capital_usdt TEXT NOT NULL,
            risk_per_trade_pct TEXT NOT NULL,
            range_order_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
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
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS execution_plans (
            intent_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            bybit_order_ids_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
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
        )
        """,
        """
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
        )
        """,
    )

    for statement in statements:
        await db.execute(statement)

    source_columns = await _source_message_columns(db)

    missing_columns = (
        (
            "status",
            """
            ALTER TABLE source_messages
            ADD COLUMN status TEXT NOT NULL
            DEFAULT 'COMPLETED'
            """,
        ),
        (
            "attempt_count",
            """
            ALTER TABLE source_messages
            ADD COLUMN attempt_count INTEGER
            NOT NULL DEFAULT 1
            """,
        ),
        (
            "last_error",
            """
            ALTER TABLE source_messages
            ADD COLUMN last_error TEXT
            """,
        ),
        (
            "claim_token",
            """
            ALTER TABLE source_messages
            ADD COLUMN claim_token TEXT
            """,
        ),
        (
            "updated_at",
            """
            ALTER TABLE source_messages
            ADD COLUMN updated_at TEXT
            NOT NULL DEFAULT ''
            """,
        ),
    )

    for column, statement in missing_columns:
        if column not in source_columns:
            await db.execute(statement)

    await db.execute(
        """
        UPDATE source_messages
        SET updated_at = CURRENT_TIMESTAMP
        WHERE updated_at = ''
        """
    )


async def _migrate_to_v2(
    db: aiosqlite.Connection,
) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS position_actions (
            action_id TEXT PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            decision_user_id INTEGER,
            bybit_order_id TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS
            ix_position_actions_source
        ON position_actions(
            channel_id,
            message_id
        )
        """
    )


MIGRATIONS = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
}


class IntentStore:
    def __init__(
        self,
        database_path: Path,
    ) -> None:
        self._database_path = database_path

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")

            cursor = await db.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            current_version = int(row[0]) if row is not None else 0

            if current_version > LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    "Database schema is newer than this application: "
                    f"{current_version} > {LATEST_SCHEMA_VERSION}"
                )

            for version in range(
                current_version + 1,
                LATEST_SCHEMA_VERSION + 1,
            ):
                migration = MIGRATIONS[version]

                await db.execute("BEGIN IMMEDIATE")

                try:
                    await migration(db)
                    await db.execute(f"PRAGMA user_version = {version}")
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

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
            cursor = await db.execute(
                """
                UPDATE source_messages
                SET
                    status = 'COMPLETED',
                    last_error = NULL,
                    claim_token = NULL,
                    updated_at =
                        CURRENT_TIMESTAMP
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

            await db.commit()
            return cursor.rowcount == 1

    async def mark_source_failed(
        self,
        source: SourceMessage,
        claim_token: str,
        error: str,
    ) -> bool:
        error = (error.strip() or "unknown processing failure")[:2000]

        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE source_messages
                SET
                    status = 'FAILED',
                    last_error = ?,
                    claim_token = NULL,
                    updated_at =
                        CURRENT_TIMESTAMP
                WHERE
                    channel_id = ?
                    AND message_id = ?
                    AND status = 'PROCESSING'
                    AND claim_token = ?
                """,
                (
                    error,
                    source.channel_id,
                    source.message_id,
                    claim_token,
                ),
            )

            await db.commit()
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
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(action.action_id),
                action.source.channel_id,
                action.source.message_id,
                action.model_dump_json(),
                action.status.value,
                now,
                now,
            ),
        )

    async def create_position_action(
        self,
        action: PositionActionIntent,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await self._insert_position_action(
                db,
                action,
            )
            await db.commit()

    async def create_intent_with_plan(
        self,
        intent: TradingIntent,
        plan: ExecutionPlan,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await self._insert_intent_with_plan(
                db,
                intent,
                plan,
            )
            await db.commit()

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

                cursor = await db.execute(
                    """
                    UPDATE source_messages
                    SET
                        status = 'COMPLETED',
                        last_error = NULL,
                        claim_token = NULL,
                        updated_at = CURRENT_TIMESTAMP
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

                if cursor.rowcount != 1:
                    await db.rollback()
                    return False

                await db.commit()
                return True

            except Exception:
                await db.rollback()
                raise

    async def create_intents_with_plans_and_complete_source(
        self,
        items: Sequence[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ],
        claim_token: str,
    ) -> bool:
        return await self.create_signal_batch_and_complete_source(
            items,
            (),
            claim_token,
        )

    async def create_intent_with_plan_and_complete_source(
        self,
        intent: TradingIntent,
        plan: ExecutionPlan,
        claim_token: str,
    ) -> bool:
        return await self.create_intents_with_plans_and_complete_source(
            (
                (
                    intent,
                    plan,
                ),
            ),
            claim_token,
        )

    async def get_intent(
        self,
        intent_id: UUID,
    ) -> TradingIntent | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row

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

        intent = TradingIntent.model_validate_json(row["payload_json"])

        return intent.model_copy(update={"status": IntentStatus(row["status"])})

    async def get_execution_plan(
        self,
        intent_id: UUID,
    ) -> ExecutionPlan | None:
        async with aiosqlite.connect(self._database_path) as db:
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

        return ExecutionPlan.model_validate_json(row[0])

    async def claim_for_execution(
        self,
        intent_id: UUID,
        user_id: int,
    ) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
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
        async with aiosqlite.connect(self._database_path) as db:
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

    async def get_position_action(
        self,
        action_id: UUID,
    ) -> PositionActionIntent | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """
                SELECT payload_json, status
                FROM position_actions
                WHERE action_id = ?
                """,
                (str(action_id),),
            )

            row = await cursor.fetchone()

        if row is None:
            return None

        action = PositionActionIntent.model_validate_json(row["payload_json"])

        return action.model_copy(update={"status": IntentStatus(row["status"])})

    async def claim_position_action_for_execution(
        self,
        action_id: UUID,
        user_id: int,
    ) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE position_actions
                SET
                    status = ?,
                    decision_user_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    action_id = ?
                    AND status = ?
                """,
                (
                    IntentStatus.EXECUTING.value,
                    user_id,
                    str(action_id),
                    IntentStatus.PENDING.value,
                ),
            )

            await db.commit()
            return cursor.rowcount == 1

    async def mark_position_action_skipped(
        self,
        action_id: UUID,
        user_id: int,
    ) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE position_actions
                SET
                    status = ?,
                    decision_user_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    action_id = ?
                    AND status = ?
                """,
                (
                    IntentStatus.SKIPPED.value,
                    user_id,
                    str(action_id),
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
        async with aiosqlite.connect(self._database_path) as db:
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
            raise ValueError("Guidance must not be empty")

        scope = "global" if channel_id is None else "channel"

        async with aiosqlite.connect(self._database_path) as db:
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
        async with aiosqlite.connect(self._database_path) as db:
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

            policy_row = await cursor.fetchone()

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

            exit_row = await cursor.fetchone()

        if policy_row is None:
            raise RuntimeError("Execution policy is not initialized")

        if exit_row is None:
            raise RuntimeError("Execution exit policy is not initialized")

        return ExecutionPolicy(
            trading_capital_usdt=(policy_row[0]),
            risk_per_trade_pct=(policy_row[1]),
            range_order_count=(policy_row[2]),
            exit_policy=ExitPolicy(
                minimum_reward_bps=(exit_row[0]),
                basic_r_multiple=(exit_row[1]),
                basic_close_pct=(exit_row[2]),
                medium_r_multiple=(exit_row[3]),
                medium_close_pct=(exit_row[4]),
                high_r_multiple=(exit_row[5]),
                high_close_pct=(exit_row[6]),
            ),
        )

    async def set_execution_policy(
        self,
        policy: ExecutionPolicy,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
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
                    str(policy.trading_capital_usdt),
                    str(policy.risk_per_trade_pct),
                    policy.range_order_count,
                ),
            )

            exit_policy = policy.exit_policy

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
                    str(exit_policy.minimum_reward_bps),
                    str(exit_policy.basic_r_multiple),
                    str(exit_policy.basic_close_pct),
                    str(exit_policy.medium_r_multiple),
                    str(exit_policy.medium_close_pct),
                    str(exit_policy.high_r_multiple),
                    str(exit_policy.high_close_pct),
                ),
            )

            await db.commit()

    async def mark_position_action_executed(
        self,
        action_id: UUID,
        order_id: str,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE position_actions
                SET
                    status = ?,
                    bybit_order_id = ?,
                    error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE action_id = ?
                """,
                (
                    IntentStatus.EXECUTED.value,
                    order_id,
                    str(action_id),
                ),
            )

            await db.commit()

    async def mark_position_action_failed(
        self,
        action_id: UUID,
        error: str,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE position_actions
                SET
                    status = ?,
                    error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE action_id = ?
                """,
                (
                    IntentStatus.FAILED.value,
                    error[:2000],
                    str(action_id),
                ),
            )

            await db.commit()

    async def mark_executed(
        self,
        intent_id: UUID,
        order_ids: tuple[str, ...],
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
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
        async with aiosqlite.connect(self._database_path) as db:
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
