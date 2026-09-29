from __future__ import annotations

from pathlib import Path

import aiosqlite

from .schema import LATEST_SCHEMA_SQL, LATEST_SCHEMA_VERSION

MIGRATION_4_TO_5_SQL = "\nCREATE TABLE IF NOT EXISTS position_strategies (\n    strategy_id TEXT PRIMARY KEY,\n    symbol TEXT NOT NULL,\n    side TEXT NOT NULL,\n    status TEXT NOT NULL,\n    entry_frozen INTEGER NOT NULL DEFAULT 0,\n    base_position_qty TEXT,\n    last_position_qty TEXT,\n    last_avg_price TEXT,\n    tp1_done INTEGER NOT NULL DEFAULT 0,\n    tp2_done INTEGER NOT NULL DEFAULT 0,\n    tp3_done INTEGER NOT NULL DEFAULT 0,\n    trailing_active INTEGER NOT NULL DEFAULT 0,\n    exit_revision INTEGER NOT NULL DEFAULT 0,\n    rebalance_needed INTEGER NOT NULL DEFAULT 0,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL\n);\n\nCREATE INDEX IF NOT EXISTS ix_position_strategies_active\nON position_strategies(status, symbol);\n"

MIGRATION_5_TO_6_SQL = "\nALTER TABLE position_strategies\nADD COLUMN protected_stop_loss TEXT;\n\nALTER TABLE position_strategies\nADD COLUMN trailing_distance TEXT;\n"

MIGRATION_6_TO_7_SQL = "\nALTER TABLE execution_policy\nRENAME TO execution_policy_v1;\n\nCREATE TABLE execution_policy (\n    id INTEGER PRIMARY KEY CHECK(id = 1),\n    risk_per_trade_pct TEXT NOT NULL,\n    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\n\nINSERT INTO execution_policy(\n    id,\n    risk_per_trade_pct,\n    updated_at\n)\nSELECT\n    id,\n    risk_per_trade_pct,\n    updated_at\nFROM execution_policy_v1;\n\nDROP TABLE execution_policy_v1;\nDROP TABLE IF EXISTS execution_exit_policy;\n"

MIGRATION_7_TO_8_SQL = "\nALTER TABLE position_strategies ADD COLUMN installing_exits INTEGER NOT NULL DEFAULT 0;\nCREATE TABLE manual_deliveries (\n    record_id TEXT PRIMARY KEY,\n    kind TEXT NOT NULL CHECK(kind IN ('OPEN', 'ACTION')),\n    claim_token TEXT,\n    retry_after TEXT,\n    delivered_at TEXT\n);\n"


async def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(database_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")

        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = int(row[0]) if row is not None else 0

        if version == LATEST_SCHEMA_VERSION:
            return

        if version in {4, 5, 6, 7}:
            try:
                migration_sql = ""

                if version == 4:
                    migration_sql += MIGRATION_4_TO_5_SQL + "\n"

                if version in {4, 5}:
                    migration_sql += MIGRATION_5_TO_6_SQL + "\n"

                if version in {4, 5, 6}:
                    migration_sql += MIGRATION_6_TO_7_SQL
                migration_sql += MIGRATION_7_TO_8_SQL

                await db.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + migration_sql
                    + f"\nPRAGMA user_version = {LATEST_SCHEMA_VERSION};\n"
                    + "COMMIT;"
                )

            except Exception:
                await db.rollback()
                raise

            return

        if version != 0:
            raise RuntimeError(
                "Unsupported database schema version: "
                f"{version}; expected 0, 4, 5, 6, 7, "
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
