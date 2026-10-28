"""Read original production decisions without reconstructing model input."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import aiosqlite


async def production_decision(
    db: aiosqlite.Connection, channel_id: int, message_id: int
) -> dict | None:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_decisions'"
    )
    if await cursor.fetchone() is None:
        return None
    cursor = await db.execute(
        """
        SELECT d.payload_json FROM source_decisions d
        JOIN source_messages s USING(channel_id, message_id)
        WHERE d.channel_id=? AND d.message_id=? AND d.attempt_count=s.attempt_count
        """,
        (channel_id, message_id),
    )
    row = await cursor.fetchone()
    return json.loads(row[0]) if row is not None else None


def record_disposition_status(status: str, approval_mode: str) -> str:
    if status == "PENDING" and approval_mode == "SKIPPED":
        return "SKIPPED"
    if status == "PENDING" and approval_mode == "AUTO":
        return "AUTO_PENDING"
    return status


def planning_rejection_kind(error: str) -> str:
    """Classify a terminal planning rejection without retaining source text."""
    text = error.lower()
    if (
        "too small to support any fixed partial exit" in text
        or "below bybit minimum quantity" in text
        or "minimum order" in text
    ):
        return "minimum_size_or_fixed_exit"
    if "geometry is invalid" in text:
        return "price_geometry"
    if "portfolio stop-risk cap" in text:
        return "portfolio_risk_cap"
    if "bybit rejected request" in text or "symbol invalid" in text:
        return "exchange_rejected_request"
    return "other"


def decision_disposition(
    decision: dict | None,
    source_status: str,
    record_statuses: list[str],
    source_reason: str | None = None,
    planning_statuses: list[str] | None = None,
) -> str:
    if decision is None:
        return (
            "SOURCE_REJECTED"
            if source_status == "COMPLETED" and source_reason
            else "UNKNOWN"
        )
    if source_status == "PROCESSING":
        return "UNRESOLVED"
    normalized = decision["normalized"]
    if not normalized["open_intents"] and not normalized["position_actions"]:
        raw = decision["extraction"]
        return (
            "NORMALIZATION_FILTERED"
            if raw["intents"] or raw["position_actions"]
            else "PROCESSED_NO_OP"
        )
    planning = decision.get("planning")
    if planning_statuses and "PENDING" in planning_statuses:
        return "UNRESOLVED"
    if planning and planning.get("retryable_errors") and not planning_statuses:
        return "UNRESOLVED"
    if planning_statuses and {"EXPIRED", "REJECTED"}.intersection(planning_statuses):
        return "PARTIAL_PLANNING_FAILURE" if record_statuses else "PLANNING_REJECTED"
    if (
        planning is not None
        and not (planning["planned_intent_ids"] or planning["position_action_ids"])
        and not planning_statuses
    ):
        return "PLANNING_REJECTED" if planning["errors"] else "CONTEXT_FILTERED"
    return _record_disposition(record_statuses)


def _record_disposition(record_statuses: list[str]) -> str:
    if {"UNCERTAIN", "EXECUTING", "AUTO_PENDING"}.intersection(record_statuses):
        return "UNRESOLVED"
    if "PENDING" in record_statuses:
        return "AWAITING_DECISION"
    if "EXECUTED" in record_statuses:
        return "EXECUTED"
    if record_statuses and all(status == "SKIPPED" for status in record_statuses):
        return "SKIPPED"
    if record_statuses and all(
        status in {"FAILED", "SKIPPED"} for status in record_statuses
    ):
        return "FAILED"
    return "UNRESOLVED"


async def coverage_summary(database_path: Path, *, since: datetime) -> dict:
    """Return aggregate counts only; raw model/source evidence stays in SQLite."""
    uri = database_path.resolve().as_uri() + "?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as db:
        cursor = await db.execute(
            """
            SELECT channel_id, message_id, status, last_error FROM source_messages
            WHERE updated_at >= ? ORDER BY channel_id, message_id
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        sources = list(await cursor.fetchall())
        dispositions: Counter[str] = Counter()
        rejection_reasons: Counter[str] = Counter()
        statuses: Counter[str] = Counter()
        source_statuses: Counter[str] = Counter()
        approval_modes: Counter[str] = Counter()
        missing = 0
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='planning_candidates'"
        )
        has_candidates = await cursor.fetchone() is not None
        candidate_counts: Counter[str] = Counter()
        for channel_id, message_id, status, reason in sources:
            source_statuses[status] += 1
            decision = await production_decision(db, channel_id, message_id)
            records = []
            for table in ("intents", "position_actions"):
                cursor = await db.execute(
                    f"SELECT status, approval_mode FROM {table} WHERE channel_id=? AND message_id=?",
                    (channel_id, message_id),
                )
                for record_status, approval_mode in await cursor.fetchall():
                    statuses[record_status] += 1
                    approval_modes[approval_mode] += 1
                    records.append(
                        record_disposition_status(record_status, approval_mode)
                    )
            candidate_statuses = []
            if has_candidates:
                cursor = await db.execute(
                    "SELECT status FROM planning_candidates WHERE channel_id=? AND message_id=?",
                    (channel_id, message_id),
                )
                candidate_statuses = [row[0] for row in await cursor.fetchall()]
                candidate_counts.update(candidate_statuses)
            disposition = decision_disposition(
                decision, status, records, reason, candidate_statuses
            )
            missing += disposition == "UNKNOWN"
            dispositions[disposition] += 1
            if decision is not None and disposition in {
                "PLANNING_REJECTED",
                "PARTIAL_PLANNING_FAILURE",
            }:
                planning = decision.get("planning") or {}
                errors = (
                    planning.get("rejections")
                    if "rejections" in planning
                    else planning.get("errors")
                )
                for error in errors or []:
                    rejection_reasons[planning_rejection_kind(str(error))] += 1
        return {
            "window_basis": "source processing updated_at (not publication time)",
            "sources": len(sources),
            "missing_original_decisions": missing,
            "source_statuses": dict(sorted(source_statuses.items())),
            "dispositions": dict(sorted(dispositions.items())),
            "planning_rejection_reasons": dict(sorted(rejection_reasons.items())),
            "linked_record_statuses": dict(sorted(statuses.items())),
            "planning_candidate_statuses": dict(sorted(candidate_counts.items())),
            "linked_approval_modes": dict(sorted(approval_modes.items())),
        }
