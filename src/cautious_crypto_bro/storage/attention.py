from __future__ import annotations

from uuid import UUID

from ..domain import IntentStatus, OperatorAttentionItem, StrategyStatus
from ._connection import _Store


class OperatorAttentionRepositoryImpl(_Store):
    async def get_operator_attention(
        self, *, limit: int, offset: int
    ) -> tuple[OperatorAttentionItem, ...]:
        rows = await self._fetch(
            """
            SELECT 'strategy' AS kind, s.strategy_id AS record_id, s.symbol,
                   s.status, COALESCE(s.pause_reason, i.error) AS reason, s.updated_at
            FROM position_strategies AS s
            LEFT JOIN intents AS i ON i.intent_id = s.strategy_id
            WHERE s.status IN (?, ?)
            UNION ALL
            SELECT 'entry', intent_id, json_extract(payload_json, '$.symbol'),
                   status, error, updated_at
            FROM intents WHERE status = ?
            UNION ALL
            SELECT 'action', action_id, json_extract(payload_json, '$.symbol'),
                   status, error, updated_at
            FROM position_actions WHERE status = ?
            ORDER BY updated_at, kind, record_id
            LIMIT ? OFFSET ?
            """,
            (
                StrategyStatus.MANUAL_OVERRIDE.value,
                StrategyStatus.UNCERTAIN.value,
                IntentStatus.UNCERTAIN.value,
                IntentStatus.UNCERTAIN.value,
                limit,
                offset,
            ),
            many=True,
            row_factory=True,
        )
        return tuple(
            OperatorAttentionItem(
                kind=row["kind"],
                record_id=UUID(row["record_id"]),
                symbol=row["symbol"],
                status=row["status"],
                reason=row["reason"],
                updated_at=row["updated_at"],
            )
            for row in rows
        )
