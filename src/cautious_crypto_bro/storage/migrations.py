from __future__ import annotations

from pathlib import Path

from ._connection import sqlite_connection
from .schema import (
    LATEST_SCHEMA_SQL,
    LATEST_SCHEMA_VERSION,
    PLANNING_CANDIDATE_SCHEMA_SQL,
    RECOVERY_NOTICE_SCHEMA_SQL,
    SOURCE_CURSOR_SCHEMA_SQL,
    SOURCE_DECISION_SCHEMA_SQL,
    ZONE_HISTORY_SCHEMA_SQL,
    ZONE_LEDGER_SCHEMA_SQL,
)

MIGRATION_4_TO_5_SQL = "\nCREATE TABLE IF NOT EXISTS position_strategies (\n    strategy_id TEXT PRIMARY KEY,\n    symbol TEXT NOT NULL,\n    side TEXT NOT NULL,\n    status TEXT NOT NULL,\n    entry_frozen INTEGER NOT NULL DEFAULT 0,\n    base_position_qty TEXT,\n    last_position_qty TEXT,\n    last_avg_price TEXT,\n    tp1_done INTEGER NOT NULL DEFAULT 0,\n    tp2_done INTEGER NOT NULL DEFAULT 0,\n    tp3_done INTEGER NOT NULL DEFAULT 0,\n    trailing_active INTEGER NOT NULL DEFAULT 0,\n    exit_revision INTEGER NOT NULL DEFAULT 0,\n    rebalance_needed INTEGER NOT NULL DEFAULT 0,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL\n);\n\nCREATE INDEX IF NOT EXISTS ix_position_strategies_active\nON position_strategies(status, symbol);\n"

MIGRATION_5_TO_6_SQL = "\nALTER TABLE position_strategies\nADD COLUMN protected_stop_loss TEXT;\n\nALTER TABLE position_strategies\nADD COLUMN trailing_distance TEXT;\n"

MIGRATION_6_TO_7_SQL = "\nALTER TABLE execution_policy\nRENAME TO execution_policy_v1;\n\nCREATE TABLE execution_policy (\n    id INTEGER PRIMARY KEY CHECK(id = 1),\n    risk_per_trade_pct TEXT NOT NULL,\n    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\n\nINSERT INTO execution_policy(\n    id,\n    risk_per_trade_pct,\n    updated_at\n)\nSELECT\n    id,\n    risk_per_trade_pct,\n    updated_at\nFROM execution_policy_v1;\n\nDROP TABLE execution_policy_v1;\nDROP TABLE IF EXISTS execution_exit_policy;\n"

MIGRATION_7_TO_8_SQL = "\nALTER TABLE position_strategies ADD COLUMN installing_exits INTEGER NOT NULL DEFAULT 0;\nCREATE TABLE manual_deliveries (\n    record_id TEXT PRIMARY KEY,\n    kind TEXT NOT NULL CHECK(kind IN ('OPEN', 'ACTION')),\n    claim_token TEXT,\n    retry_after TEXT,\n    delivered_at TEXT\n);\n"


MIGRATION_8_TO_9_SQL = """
ALTER TABLE position_strategies ADD COLUMN expected_exits_json TEXT;
ALTER TABLE position_strategies ADD COLUMN pause_reason TEXT;
ALTER TABLE position_strategies ADD COLUMN pause_notified INTEGER NOT NULL DEFAULT 0;
"""

MIGRATION_9_TO_10_SQL = (
    RECOVERY_NOTICE_SCHEMA_SQL
    + """
INSERT INTO interrupted_execution_notices(kind, record_id)
SELECT 'OPEN', intent_id FROM intents
WHERE status = 'UNCERTAIN'
  AND error = 'Process restarted during execution; exchange outcome is unknown; not retried automatically';
INSERT INTO interrupted_execution_notices(kind, record_id)
SELECT 'ACTION', action_id FROM position_actions
WHERE status = 'UNCERTAIN'
  AND error = 'Process restarted during execution; exchange outcome is unknown; not retried automatically';
"""
)


MIGRATION_13_TO_14_SQL = """
CREATE TABLE zone_orders_v14 (
    order_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL REFERENCES zone_allocations(zone_id),
    order_link_id TEXT UNIQUE CHECK(order_link_id IS NULL OR length(order_link_id)>0),
    payload_json TEXT NOT NULL
);
INSERT INTO zone_orders_v14 SELECT order_id,zone_id,order_link_id,payload_json FROM zone_orders;
DROP TABLE zone_orders;
ALTER TABLE zone_orders_v14 RENAME TO zone_orders;
"""


async def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)

    async with sqlite_connection(database_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")

        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = int(row[0]) if row is not None else 0

        if version == LATEST_SCHEMA_VERSION:
            return

        if version in {4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}:
            try:
                steps = (
                    (4, MIGRATION_4_TO_5_SQL),
                    (5, MIGRATION_5_TO_6_SQL),
                    (6, MIGRATION_6_TO_7_SQL),
                    (7, MIGRATION_7_TO_8_SQL),
                    (8, MIGRATION_8_TO_9_SQL),
                    (9, MIGRATION_9_TO_10_SQL),
                    (10, SOURCE_DECISION_SCHEMA_SQL),
                    (11, ZONE_LEDGER_SCHEMA_SQL),
                    (12, PLANNING_CANDIDATE_SCHEMA_SQL),
                    (13, MIGRATION_13_TO_14_SQL),
                    (14, ZONE_HISTORY_SCHEMA_SQL),
                    (15, SOURCE_CURSOR_SCHEMA_SQL),
                )
                migration_sql = "\n".join(
                    sql for source_version, sql in steps if version <= source_version
                )

                await db.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + migration_sql
                    + f"\nPRAGMA user_version = {LATEST_SCHEMA_VERSION};\n"
                )
                cursor = await db.execute("PRAGMA foreign_key_check")
                if await cursor.fetchall():
                    raise RuntimeError("Migrated database has invalid foreign keys")
                await db.commit()

            except Exception:
                await db.rollback()
                raise

            return

        if version != 0:
            raise RuntimeError(
                "Unsupported database schema version: "
                f"{version}; expected 0, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, "
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
