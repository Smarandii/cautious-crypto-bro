from __future__ import annotations

import aiosqlite

from ..domain import ExecutionPolicy
from ._connection import _Store


class ExecutionPolicyRepositoryImpl(_Store):
    async def get_execution_policy(
        self,
    ) -> ExecutionPolicy:
        row = await self._fetch(
            """
            SELECT risk_per_trade_pct
            FROM execution_policy
            WHERE id = 1
            """
        )

        if row is None:
            raise RuntimeError("Execution policy is not initialized")

        return ExecutionPolicy(
            risk_per_trade_pct=row[0],
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
                    risk_per_trade_pct,
                    updated_at
                )
                VALUES (
                    1,
                    ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(id)
                DO UPDATE SET
                    risk_per_trade_pct =
                        excluded.risk_per_trade_pct,
                    updated_at =
                        CURRENT_TIMESTAMP
                """,
                (str(policy.risk_per_trade_pct),),
            )

            await db.commit()
