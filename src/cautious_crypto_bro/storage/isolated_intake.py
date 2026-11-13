"""Durable account assignment for an opt-in isolated Demo intake."""

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import aiosqlite

from ..domain import ExecutionPlan, IntentStatus, StoreError, TradingIntent
from ._connection import sqlite_connection
from .schema import LATEST_SCHEMA_SQL

ISOLATED_INTAKE_SCHEMA_VERSION = 19


@dataclass(frozen=True, slots=True)
class DemoIntentAssignment:
    uid: int = field(repr=False)
    intent: TradingIntent = field(repr=False)
    plan: ExecutionPlan = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.uid) is not int or self.uid <= 0:
            raise ValueError("A verified Demo account identity is required")
        if (
            self.intent.status is not IntentStatus.PENDING
            or self.plan.strategy_version != 2
            or self.plan.intent_id != self.intent.intent_id
            or self.plan.symbol != self.intent.symbol
            or self.plan.side is not self.intent.side
        ):
            raise ValueError(
                "Assignment requires a matching pending Strategy V2 intent"
            )

    def payload(self) -> tuple[str, str, str]:
        return str(self.uid), self.intent.model_dump_json(), self.plan.model_dump_json()


class IsolatedDemoIntake:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("PRAGMA user_version") as cursor:
                version = await cursor.fetchone()
            if version == (ISOLATED_INTAKE_SCHEMA_VERSION,):
                await self._initialize_source_schema(db)
                await db.commit()
                return
            async with db.execute(
                "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ) as cursor:
                has_schema = await cursor.fetchone() is not None
            if version != (0,) or has_schema:
                raise StoreError("Isolated Demo intake requires a dedicated database")
            await db.execute(
                "CREATE TABLE demo_intent_assignments ("
                "intent_id TEXT PRIMARY KEY, uid TEXT NOT NULL, "
                "intent_json TEXT NOT NULL, plan_json TEXT NOT NULL)"
            )
            await self._initialize_source_schema(db)
            await db.execute(f"PRAGMA user_version = {ISOLATED_INTAKE_SCHEMA_VERSION}")
            await db.commit()

    @staticmethod
    async def _initialize_source_schema(db: aiosqlite.Connection) -> None:
        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_messages'"
        ) as cursor:
            if await cursor.fetchone() is not None:
                return
        for statement in LATEST_SCHEMA_SQL.split(";"):
            if statement.strip():
                await db.execute(statement)

    async def assign(self, assignment: DemoIntentAssignment) -> None:
        """Commit the frozen assignment before any worker import."""
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await insert_assignment(db, assignment)
            await db.commit()

    async def get_assignment(self, intent_id: UUID) -> DemoIntentAssignment | None:
        async with (
            sqlite_connection(self._database_path) as db,
            db.execute(
                "SELECT uid, intent_json, plan_json FROM demo_intent_assignments "
                "WHERE intent_id=?",
                (str(intent_id),),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        uid, intent_json, plan_json = row
        return DemoIntentAssignment(
            int(uid),
            TradingIntent.model_validate_json(intent_json),
            ExecutionPlan.model_validate_json(plan_json),
        )


async def insert_assignment(
    db: aiosqlite.Connection, assignment: DemoIntentAssignment
) -> None:
    key = str(assignment.intent.intent_id)
    payload = assignment.payload()
    await db.execute(
        "INSERT INTO demo_intent_assignments VALUES (?, ?, ?, ?) "
        "ON CONFLICT(intent_id) DO NOTHING",
        (key, *payload),
    )
    async with db.execute(
        "SELECT uid, intent_json, plan_json FROM demo_intent_assignments WHERE intent_id=?",
        (key,),
    ) as cursor:
        if await cursor.fetchone() != payload:
            raise StoreError("Frozen Demo intent assignment cannot be replaced")
