"""Claim-owned informational evidence and its independent durable delivery lease."""

from __future__ import annotations

from uuid import uuid4

from ..domain import EntryReview, SourceMessage
from ._connection import _Store, sqlite_connection


class EntryReviewRepositoryImpl(_Store):
    async def save_entry_reviews(
        self,
        source: SourceMessage,
        claim_token: str,
        reviews: tuple[EntryReview, ...],
        fingerprint: str | None,
    ) -> bool:
        if reviews and not fingerprint:
            raise ValueError("Entry review requires its extraction fingerprint")
        async with sqlite_connection(self._database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT 1 FROM source_messages WHERE channel_id=? AND message_id=? "
                "AND status='PROCESSING' AND claim_token=?",
                (source.channel_id, source.message_id, claim_token),
            )
            if await cursor.fetchone() is None:
                return False
            for review in reviews:
                if review.source != source:
                    raise ValueError("Review source does not match owning claim")
                await db.execute(
                    "INSERT OR IGNORE INTO entry_reviews "
                    "(channel_id,message_id,symbol,payload_json,evaluation_fingerprint) "
                    "VALUES (?,?,?,?,?)",
                    (
                        source.channel_id,
                        source.message_id,
                        review.symbol,
                        review.model_dump_json(),
                        fingerprint,
                    ),
                )
            await db.commit()
            return True

    async def pending_entry_reviews(self) -> tuple[EntryReview, ...]:
        rows = await self._fetch(
            "SELECT payload_json FROM entry_reviews WHERE delivered_at IS NULL "
            "AND (retry_after IS NULL OR retry_after<=CURRENT_TIMESTAMP) "
            "ORDER BY created_at,channel_id,message_id,symbol LIMIT 100",
            many=True,
        )
        return tuple(EntryReview.model_validate_json(row[0]) for row in rows)

    async def claim_entry_review(self, review: EntryReview) -> str | None:
        token = str(uuid4())
        async with sqlite_connection(self._database_path) as db:
            cursor = await db.execute(
                "UPDATE entry_reviews SET claim_token=?,retry_after=datetime('now','+5 minutes') "
                "WHERE channel_id=? AND message_id=? AND symbol=? AND delivered_at IS NULL "
                "AND (retry_after IS NULL OR retry_after<=CURRENT_TIMESTAMP)",
                (
                    token,
                    review.source.channel_id,
                    review.source.message_id,
                    review.symbol,
                ),
            )
            await db.commit()
            return token if cursor.rowcount == 1 else None

    async def finish_entry_review(
        self, review: EntryReview, token: str, *, delivered: bool
    ) -> None:
        async with sqlite_connection(self._database_path) as db:
            await db.execute(
                "UPDATE entry_reviews SET claim_token=NULL,"
                "delivered_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,"
                "retry_after=datetime('now','+30 seconds') "
                "WHERE channel_id=? AND message_id=? AND symbol=? AND claim_token=?",
                (
                    delivered,
                    review.source.channel_id,
                    review.source.message_id,
                    review.symbol,
                    token,
                ),
            )
            await db.commit()
