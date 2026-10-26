from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from ..domain import ApprovalMode, ExecutionPlan, TradingIntent
from ._connection import sqlite_connection
from .source import SourceRepositoryImpl


@dataclass(frozen=True)
class PlanningCandidate:
    intent: TradingIntent
    duplicate_in_batch: bool
    deadline: datetime
    claim_token: str


class PlanningCandidateRepositoryImpl(SourceRepositoryImpl):
    async def pending_transferred_auto_candidates(self) -> tuple[UUID, ...]:
        rows = await self._fetch(
            "SELECT i.intent_id FROM intents i JOIN planning_candidates p USING(intent_id) "
            "WHERE p.status='TRANSFERRED' AND i.status='PENDING' AND i.approval_mode='AUTO' "
            "ORDER BY i.created_at LIMIT 10",
            many=True,
        )
        return tuple(UUID(row[0]) for row in rows)

    async def claim_planning_candidate(self) -> PlanningCandidate | None:
        """Lease one due candidate; interrupted planning becomes due after five minutes."""
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT intent_id,payload_json,duplicate_in_batch,deadline "
                    "FROM planning_candidates WHERE status='PENDING' "
                    "AND retry_after <= CURRENT_TIMESTAMP "
                    "ORDER BY retry_after,intent_id LIMIT 1"
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.rollback()
                    return None
                token = str(uuid4())
                candidate = PlanningCandidate(
                    intent=TradingIntent.model_validate_json(row[1]),
                    duplicate_in_batch=bool(row[2]),
                    deadline=datetime.fromisoformat(row[3]),
                    claim_token=token,
                )
                await db.execute(
                    "UPDATE planning_candidates SET claim_token=?, "
                    "retry_after=datetime('now','+300 seconds'), "
                    "attempt_count=attempt_count+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE intent_id=?",
                    (token, row[0]),
                )
                await db.commit()
                return candidate
            except BaseException:
                await db.rollback()
                raise

    async def finish_planning_candidate(
        self, intent_id: UUID, claim_token: str, *, status: str, error: str
    ) -> bool:
        if status not in {"PENDING", "REJECTED", "EXPIRED"}:
            raise ValueError("Invalid planning result")
        async with sqlite_connection(self._database_path) as db:
            cursor = await db.execute(
                "UPDATE planning_candidates SET status=?,last_error=?,claim_token=NULL, "
                "retry_after=datetime('now','+300 seconds'),updated_at=CURRENT_TIMESTAMP "
                "WHERE intent_id=? AND claim_token=? AND status='PENDING' "
                "AND retry_after>CURRENT_TIMESTAMP",
                (status, error[:2000], str(intent_id), claim_token),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def transfer_planning_candidate(
        self, candidate: PlanningCandidate, intent: TradingIntent, plan: ExecutionPlan
    ) -> bool:
        if (
            intent.intent_id != candidate.intent.intent_id
            or plan.intent_id != intent.intent_id
        ):
            raise ValueError("Recovered candidate identity changed")
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE planning_candidates SET status='TRANSFERRED',claim_token=NULL, "
                    "last_error=NULL,updated_at=CURRENT_TIMESTAMP "
                    "WHERE intent_id=? AND claim_token=? AND status='PENDING' "
                    "AND retry_after>CURRENT_TIMESTAMP AND julianday(deadline)>julianday('now')",
                    (str(intent.intent_id), candidate.claim_token),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return False
                await self._insert_intent_with_plan(db, intent, plan)
                if intent.approval_mode in {ApprovalMode.MANUAL, ApprovalMode.AUTO}:
                    await db.execute(
                        "INSERT INTO manual_deliveries(record_id,kind) VALUES (?,'OPEN')",
                        (str(intent.intent_id),),
                    )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise
