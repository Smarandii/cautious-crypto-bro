from __future__ import annotations

import hashlib
import json

import aiosqlite

from ..domain import IntentStatus, PositionActionIntent, StoreError
from ._connection import SQLiteConnectionMixin, sqlite_connection
from .attention import OperatorAttentionRepositoryImpl
from .auto_outcome import AutoOutcomeRepositoryImpl
from .entry_review import EntryReviewRepositoryImpl
from .execution_policy import ExecutionPolicyRepositoryImpl
from .guidance import GuidanceRepositoryImpl
from .intent import IntentRepositoryImpl
from .isolated_intake import DemoIntentAssignment
from .manual_delivery import ManualDeliveryRepositoryImpl
from .migrations import (
    ISOLATED_DEMO_SCHEMA_VERSION,
    initialize_database,
    initialize_isolated_demo_database,
)
from .planning_candidate import PlanningCandidateRepositoryImpl
from .pnl import PnlRepositoryImpl
from .position_action import PositionActionRepositoryImpl
from .position_strategy import PositionStrategyRepositoryImpl
from .recovery import RecoveryRepositoryImpl
from .zone import ZoneLedgerRepositoryImpl
from .zone_history import ZoneHistoryRepositoryImpl


class SQLiteStore(
    SQLiteConnectionMixin,
    EntryReviewRepositoryImpl,
    OperatorAttentionRepositoryImpl,
    AutoOutcomeRepositoryImpl,
    IntentRepositoryImpl,
    PositionActionRepositoryImpl,
    PositionStrategyRepositoryImpl,
    ManualDeliveryRepositoryImpl,
    GuidanceRepositoryImpl,
    ExecutionPolicyRepositoryImpl,
    PnlRepositoryImpl,
    PlanningCandidateRepositoryImpl,
    RecoveryRepositoryImpl,
    ZoneLedgerRepositoryImpl,
    ZoneHistoryRepositoryImpl,
):
    async def initialize(self) -> None:
        await initialize_database(self._database_path)

    async def initialize_isolated_demo(self, verified_uid: int) -> None:
        await initialize_isolated_demo_database(self._database_path, verified_uid)

    async def import_isolated_intent(self, assignment: DemoIntentAssignment) -> None:
        """Copy an intake assignment once without resetting execution state."""
        key = str(assignment.intent.intent_id)
        digest = hashlib.sha256(json.dumps(assignment.payload()).encode()).hexdigest()
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._verify_worker_binding(db, assignment.uid)
            await db.execute(
                "CREATE TABLE IF NOT EXISTS demo_intent_imports ("
                "intent_id TEXT PRIMARY KEY, digest TEXT NOT NULL)"
            )
            async with db.execute(
                "SELECT d.digest FROM intents i "
                "JOIN execution_plans p USING(intent_id) "
                "LEFT JOIN demo_intent_imports d USING(intent_id) WHERE i.intent_id=?",
                (key,),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                if existing != (digest,):
                    raise StoreError("Frozen Demo intent import cannot be replaced")
                return
            await self._insert_intent_with_plan(db, assignment.intent, assignment.plan)
            await db.execute(
                "INSERT INTO demo_intent_imports VALUES (?, ?)", (key, digest)
            )
            await db.commit()

    async def import_isolated_position_action(
        self, uid: int, action: PositionActionIntent
    ) -> None:
        if (
            action.target_strategy_id is None
            or (action.target_demo_uid is not None and action.target_demo_uid != uid)
            or action.status is not IntentStatus.PENDING
            or action.contract_review_reason is not None
        ):
            raise StoreError(
                "Worker import requires a pending action with a saved target"
            )
        payload = action.model_dump_json()
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._verify_worker_binding(db, uid)
            async with db.execute(
                "SELECT payload_json, approval_mode FROM position_actions WHERE action_id=?",
                (str(action.action_id),),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                if tuple(existing) != (payload, action.approval_mode.value):
                    raise StoreError("Frozen Demo lifecycle action cannot be replaced")
                return
            await self._insert_position_action(db, action)
            await db.commit()

    @staticmethod
    async def _verify_worker_binding(db: aiosqlite.Connection, uid: int) -> None:
        async with db.execute("PRAGMA user_version") as cursor:
            if await cursor.fetchone() != (ISOLATED_DEMO_SCHEMA_VERSION,):
                raise StoreError("Import requires an isolated Demo worker")
        async with db.execute(
            "SELECT uid FROM demo_account_binding WHERE id=1"
        ) as cursor:
            if await cursor.fetchone() != (str(uid),):
                raise StoreError("Assignment does not match the Demo worker")
