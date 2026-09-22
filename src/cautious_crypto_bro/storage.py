from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from .domain import (
    ApprovalMode,
    ClosedPnlRecord,
    ExecutionPlan,
    ExecutionPolicy,
    ExitPolicy,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    PositionStrategy,
    SourceMessage,
    StrategyStatus,
    TradingIntent,
)

LATEST_SCHEMA_VERSION = 5

LATEST_SCHEMA_SQL = """
CREATE TABLE source_messages (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'COMPLETED',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    claim_token TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, message_id)
);

CREATE TABLE intents (
    intent_id TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_mode TEXT NOT NULL DEFAULT 'MANUAL',
    decision_user_id INTEGER,
    bybit_order_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX ix_intents_approval_status
ON intents(approval_mode, status);

CREATE TABLE signal_guidance (
    scope TEXT NOT NULL CHECK(scope IN ('global', 'channel')),
    channel_id INTEGER,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(
        (scope = 'global' AND channel_id IS NULL)
        OR
        (scope = 'channel' AND channel_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX ux_signal_guidance_global
ON signal_guidance(scope)
WHERE scope = 'global';

CREATE UNIQUE INDEX ux_signal_guidance_channel
ON signal_guidance(channel_id)
WHERE scope = 'channel';

CREATE TABLE execution_policy (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    trading_capital_usdt TEXT NOT NULL,
    risk_per_trade_pct TEXT NOT NULL,
    range_order_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE execution_exit_policy (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    minimum_reward_bps TEXT NOT NULL,
    basic_r_multiple TEXT NOT NULL,
    basic_close_pct TEXT NOT NULL,
    medium_r_multiple TEXT NOT NULL,
    medium_close_pct TEXT NOT NULL,
    high_r_multiple TEXT NOT NULL,
    high_close_pct TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE execution_plans (
    intent_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    bybit_order_ids_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE position_strategies (
    strategy_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_frozen INTEGER NOT NULL DEFAULT 0,
    base_position_qty TEXT,
    last_position_qty TEXT,
    last_avg_price TEXT,
    tp1_done INTEGER NOT NULL DEFAULT 0,
    tp2_done INTEGER NOT NULL DEFAULT 0,
    tp3_done INTEGER NOT NULL DEFAULT 0,
    trailing_active INTEGER NOT NULL DEFAULT 0,
    exit_revision INTEGER NOT NULL DEFAULT 0,
    rebalance_needed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX ix_position_strategies_active
ON position_strategies(status, symbol);

CREATE TABLE position_actions (
    action_id TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_mode TEXT NOT NULL DEFAULT 'MANUAL',
    decision_user_id INTEGER,
    bybit_order_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX ix_position_actions_source
ON position_actions(channel_id, message_id);

CREATE INDEX ix_position_actions_approval_status
ON position_actions(approval_mode, status);

CREATE TABLE account_closed_pnl (
    record_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    position_side TEXT NOT NULL,
    closed_pnl TEXT NOT NULL,
    closed_size TEXT NOT NULL,
    avg_entry_price TEXT,
    avg_exit_price TEXT,
    closed_at TEXT NOT NULL,
    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX ix_account_closed_pnl_closed_at
ON account_closed_pnl(closed_at);

CREATE TABLE account_pnl_sync (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    history_start_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO execution_policy(
    id,
    trading_capital_usdt,
    risk_per_trade_pct,
    range_order_count
)
VALUES (1, '6800', '1', 3);

INSERT INTO execution_exit_policy(
    id,
    minimum_reward_bps,
    basic_r_multiple,
    basic_close_pct,
    medium_r_multiple,
    medium_close_pct,
    high_r_multiple,
    high_close_pct
)
VALUES (1, '20', '0.5', '25', '1', '35', '2', '40');
"""

MIGRATION_4_TO_5_SQL = """
CREATE TABLE IF NOT EXISTS position_strategies (
    strategy_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_frozen INTEGER NOT NULL DEFAULT 0,
    base_position_qty TEXT,
    last_position_qty TEXT,
    last_avg_price TEXT,
    tp1_done INTEGER NOT NULL DEFAULT 0,
    tp2_done INTEGER NOT NULL DEFAULT 0,
    tp3_done INTEGER NOT NULL DEFAULT 0,
    trailing_active INTEGER NOT NULL DEFAULT 0,
    exit_revision INTEGER NOT NULL DEFAULT 0,
    rebalance_needed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_position_strategies_active
ON position_strategies(status, symbol);
"""


@dataclass(frozen=True, slots=True)
class AccountPnlSyncState:
    history_start_at: datetime
    last_synced_at: datetime


@dataclass(frozen=True, slots=True)
class AccountPnlSummary:
    realized_pnl: Decimal
    record_count: int
    positive_count: int
    negative_count: int
    history_start_at: datetime
    last_synced_at: datetime


class IntentStore:
    def __init__(
        self,
        database_path: Path,
    ) -> None:
        self._database_path = database_path

    async def _fetch(
        self,
        query: str,
        parameters: Sequence[object] = (),
        *,
        many: bool = False,
        row_factory: bool = False,
    ) -> Any:
        async with aiosqlite.connect(self._database_path) as db:
            if row_factory:
                db.row_factory = aiosqlite.Row

            cursor = await db.execute(query, parameters)

            return await (cursor.fetchall() if many else cursor.fetchone())

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")

            cursor = await db.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            version = int(row[0]) if row is not None else 0

            if version == LATEST_SCHEMA_VERSION:
                return

            if version == 4:
                try:
                    await db.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + MIGRATION_4_TO_5_SQL
                        + "\nPRAGMA user_version = 5;\n"
                        + "COMMIT;"
                    )
                except Exception:
                    await db.rollback()
                    raise

                return

            if version != 0:
                raise RuntimeError(
                    "Unsupported database schema version: "
                    f"{version}; expected 0, 4, "
                    f"or {LATEST_SCHEMA_VERSION}"
                )

            cursor = await db.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                LIMIT 1
                """
            )

            if await cursor.fetchone() is not None:
                raise RuntimeError("Unversioned non-empty database is unsupported")

            try:
                await db.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + LATEST_SCHEMA_SQL
                    + f"\nPRAGMA user_version = {LATEST_SCHEMA_VERSION};\n"
                    + "COMMIT;"
                )
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

    async def mark_source_completed(
        self,
        source: SourceMessage,
        claim_token: str,
    ) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            finished = await self._finish_source(
                db,
                source,
                claim_token,
                status="COMPLETED",
                error=None,
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

        async with aiosqlite.connect(self._database_path) as db:
            finished = await self._finish_source(
                db,
                source,
                claim_token,
                status="FAILED",
                error=error,
            )
            await db.commit()
            return finished

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

    async def _quarantine_auto_executing_records(
        self,
        db: aiosqlite.Connection,
        *,
        table: str,
        id_column: str,
        error: str,
    ) -> tuple[UUID, ...]:
        cursor = await db.execute(
            f"""
            SELECT {id_column}
            FROM {table}
            WHERE approval_mode = ? AND status = ?
            """,
            (
                ApprovalMode.AUTO.value,
                IntentStatus.EXECUTING.value,
            ),
        )
        record_ids = tuple(UUID(row[0]) for row in await cursor.fetchall())

        await db.execute(
            f"""
            UPDATE {table}
            SET
                status = ?,
                error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE approval_mode = ? AND status = ?
            """,
            (
                IntentStatus.UNCERTAIN.value,
                error,
                ApprovalMode.AUTO.value,
                IntentStatus.EXECUTING.value,
            ),
        )
        return record_ids

    async def quarantine_auto_executing(
        self,
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
        error = (
            "Process restarted while AUTO execution was in progress; "
            "not retried automatically"
        )

        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")

            try:
                intent_ids = await self._quarantine_auto_executing_records(
                    db,
                    table="intents",
                    id_column="intent_id",
                    error=error,
                )
                action_ids = await self._quarantine_auto_executing_records(
                    db,
                    table="position_actions",
                    id_column="action_id",
                    error=error,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        return intent_ids, action_ids

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

    async def get_guidance(
        self,
        channel_id: int,
    ) -> tuple[str | None, str | None]:
        rows = await self._fetch(
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
            many=True,
        )

        global_guidance = None
        channel_guidance = None

        for scope, content in rows:
            if scope == "global":
                global_guidance = content
            else:
                channel_guidance = content

        return global_guidance, channel_guidance

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

    async def upsert_closed_pnl(
        self,
        records: Sequence[ClosedPnlRecord],
    ) -> None:
        if not records:
            return

        async with aiosqlite.connect(self._database_path) as db:
            await db.executemany(
                """
                INSERT INTO account_closed_pnl(
                    record_id,
                    order_id,
                    symbol,
                    position_side,
                    closed_pnl,
                    closed_size,
                    avg_entry_price,
                    avg_exit_price,
                    closed_at,
                    synced_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(record_id)
                DO UPDATE SET
                    order_id =
                        excluded.order_id,
                    symbol =
                        excluded.symbol,
                    position_side =
                        excluded.position_side,
                    closed_pnl =
                        excluded.closed_pnl,
                    closed_size =
                        excluded.closed_size,
                    avg_entry_price =
                        excluded.avg_entry_price,
                    avg_exit_price =
                        excluded.avg_exit_price,
                    closed_at =
                        excluded.closed_at,
                    synced_at =
                        CURRENT_TIMESTAMP
                """,
                [
                    (
                        record.record_id,
                        record.order_id,
                        record.symbol,
                        record.position_side.value,
                        str(record.closed_pnl),
                        str(record.closed_size),
                        (
                            str(record.avg_entry_price)
                            if record.avg_entry_price is not None
                            else None
                        ),
                        (
                            str(record.avg_exit_price)
                            if record.avg_exit_price is not None
                            else None
                        ),
                        (record.updated_at.isoformat()),
                    )
                    for record in records
                ],
            )

            await db.commit()

    async def get_account_pnl_sync_state(
        self,
    ) -> AccountPnlSyncState | None:
        row = await self._fetch(
            """
            SELECT history_start_at, last_synced_at
            FROM account_pnl_sync
            WHERE id = 1
            """
        )

        if row is None:
            return None

        return AccountPnlSyncState(
            history_start_at=datetime.fromisoformat(row[0]),
            last_synced_at=datetime.fromisoformat(row[1]),
        )

    async def mark_account_pnl_synced(
        self,
        *,
        history_start_at: datetime,
        last_synced_at: datetime,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO account_pnl_sync(
                    id,
                    history_start_at,
                    last_synced_at,
                    updated_at
                )
                VALUES (
                    1,
                    ?,
                    ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(id)
                DO UPDATE SET
                    history_start_at =
                        CASE
                            WHEN
                                excluded.history_start_at
                                <
                                account_pnl_sync.history_start_at
                            THEN
                                excluded.history_start_at
                            ELSE
                                account_pnl_sync.history_start_at
                        END,
                    last_synced_at =
                        CASE
                            WHEN
                                excluded.last_synced_at
                                >
                                account_pnl_sync.last_synced_at
                            THEN
                                excluded.last_synced_at
                            ELSE
                                account_pnl_sync.last_synced_at
                        END,
                    updated_at =
                        CURRENT_TIMESTAMP
                """,
                (
                    history_start_at.isoformat(),
                    last_synced_at.isoformat(),
                ),
            )

            await db.commit()

    async def get_account_pnl_summary(
        self,
    ) -> AccountPnlSummary | None:
        sync_state = await self.get_account_pnl_sync_state()

        if sync_state is None:
            return None

        rows = await self._fetch(
            """
            SELECT closed_pnl
            FROM account_closed_pnl
            """,
            many=True,
        )

        values = [Decimal(row[0]) for row in rows]
        realized = sum(values, Decimal("0"))

        return AccountPnlSummary(
            realized_pnl=realized,
            record_count=len(values),
            positive_count=sum(value > 0 for value in values),
            negative_count=sum(value < 0 for value in values),
            history_start_at=sync_state.history_start_at,
            last_synced_at=sync_state.last_synced_at,
        )

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
        await self._mark_failed(
            table="position_actions",
            id_column="action_id",
            record_id=action_id,
            error=error,
        )

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
        await self._mark_failed(
            table="intents",
            id_column="intent_id",
            record_id=intent_id,
            error=error,
        )

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
            exit_revision=int(row["exit_revision"]),
            rebalance_needed=bool(row["rebalance_needed"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

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
                p.payload_json AS plan_json
            FROM position_strategies AS s
            JOIN execution_plans AS p
              ON p.intent_id = s.strategy_id
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
                ExecutionPlan.model_validate_json(row["plan_json"]),
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
                    exit_revision = ?,
                    rebalance_needed = ?,
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
                    state.exit_revision,
                    int(state.rebalance_needed),
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
