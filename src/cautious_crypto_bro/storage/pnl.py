from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

import aiosqlite

from ..domain import AccountPnlSummary, AccountPnlSyncState, ClosedPnlRecord
from ._connection import _Store


class PnlRepositoryImpl(_Store):
    async def upsert_closed_pnl(
        self,
        records: Sequence[ClosedPnlRecord],
    ) -> None:
        if not records:
            return

        async with aiosqlite.connect(self._database_path) as db:
            await db.executemany(
                """
                INSERT INTO account_closed_pnl(
                    record_id,
                    order_id,
                    symbol,
                    position_side,
                    closed_pnl,
                    closed_size,
                    avg_entry_price,
                    avg_exit_price,
                    closed_at,
                    synced_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(record_id)
                DO UPDATE SET
                    order_id =
                        excluded.order_id,
                    symbol =
                        excluded.symbol,
                    position_side =
                        excluded.position_side,
                    closed_pnl =
                        excluded.closed_pnl,
                    closed_size =
                        excluded.closed_size,
                    avg_entry_price =
                        excluded.avg_entry_price,
                    avg_exit_price =
                        excluded.avg_exit_price,
                    closed_at =
                        excluded.closed_at,
                    synced_at =
                        CURRENT_TIMESTAMP
                """,
                [
                    (
                        record.record_id,
                        record.order_id,
                        record.symbol,
                        record.position_side.value,
                        str(record.closed_pnl),
                        str(record.closed_size),
                        (
                            str(record.avg_entry_price)
                            if record.avg_entry_price is not None
                            else None
                        ),
                        (
                            str(record.avg_exit_price)
                            if record.avg_exit_price is not None
                            else None
                        ),
                        (record.updated_at.isoformat()),
                    )
                    for record in records
                ],
            )

            await db.commit()

    async def get_account_pnl_sync_state(
        self,
    ) -> AccountPnlSyncState | None:
        row = await self._fetch(
            """
            SELECT history_start_at, last_synced_at
            FROM account_pnl_sync
            WHERE id = 1
            """
        )

        if row is None:
            return None

        return AccountPnlSyncState(
            history_start_at=datetime.fromisoformat(row[0]),
            last_synced_at=datetime.fromisoformat(row[1]),
        )

    async def mark_account_pnl_synced(
        self,
        *,
        history_start_at: datetime,
        last_synced_at: datetime,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO account_pnl_sync(
                    id,
                    history_start_at,
                    last_synced_at,
                    updated_at
                )
                VALUES (
                    1,
                    ?,
                    ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(id)
                DO UPDATE SET
                    history_start_at =
                        CASE
                            WHEN
                                excluded.history_start_at
                                <
                                account_pnl_sync.history_start_at
                            THEN
                                excluded.history_start_at
                            ELSE
                                account_pnl_sync.history_start_at
                        END,
                    last_synced_at =
                        CASE
                            WHEN
                                excluded.last_synced_at
                                >
                                account_pnl_sync.last_synced_at
                            THEN
                                excluded.last_synced_at
                            ELSE
                                account_pnl_sync.last_synced_at
                        END,
                    updated_at =
                        CURRENT_TIMESTAMP
                """,
                (
                    history_start_at.isoformat(),
                    last_synced_at.isoformat(),
                ),
            )

            await db.commit()

    async def get_account_pnl_summary(
        self,
    ) -> AccountPnlSummary | None:
        sync_state = await self.get_account_pnl_sync_state()

        if sync_state is None:
            return None

        rows = await self._fetch(
            """
            SELECT closed_pnl
            FROM account_closed_pnl
            """,
            many=True,
        )

        values = [Decimal(row[0]) for row in rows]
        realized = sum(values, Decimal("0"))

        return AccountPnlSummary(
            realized_pnl=realized,
            record_count=len(values),
            positive_count=sum(value > 0 for value in values),
            negative_count=sum(value < 0 for value in values),
            history_start_at=sync_state.history_start_at,
            last_synced_at=sync_state.last_synced_at,
        )
