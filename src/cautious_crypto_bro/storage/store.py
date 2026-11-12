from __future__ import annotations

import hashlib
import json

from ..domain import StoreError
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
            async with db.execute("PRAGMA user_version") as cursor:
                if await cursor.fetchone() != (ISOLATED_DEMO_SCHEMA_VERSION,):
                    raise StoreError("Intent import requires an isolated Demo worker")
            async with db.execute(
                "SELECT uid FROM demo_account_binding WHERE id=1"
            ) as cursor:
                if await cursor.fetchone() != (str(assignment.uid),):
                    raise StoreError("Assignment does not match the Demo worker")
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
