import asyncio
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

from cautious_crypto_bro.domain import (
    ClosedPnlRecord,
    Side,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)


def test_account_pnl_ledger_is_idempotent(
    tmp_path,
) -> None:
    async def run() -> None:
        database = tmp_path / "state.sqlite3"

        store = IntentStore(database)
        await store.initialize()

        first_at = datetime(
            2026,
            9,
            12,
            12,
            tzinfo=UTC,
        )

        second_at = datetime(
            2026,
            9,
            13,
            12,
            tzinfo=UTC,
        )

        records = (
            ClosedPnlRecord(
                record_id="BTCUSDT:one",
                order_id="one",
                symbol="BTCUSDT",
                position_side=Side.LONG,
                closed_pnl=Decimal("-25.5"),
                closed_size=Decimal("0.1"),
                avg_entry_price=Decimal("78000"),
                avg_exit_price=Decimal("77500"),
                updated_at=first_at,
            ),
            ClosedPnlRecord(
                record_id="ETHUSDT:two",
                order_id="two",
                symbol="ETHUSDT",
                position_side=Side.SHORT,
                closed_pnl=Decimal("40.25"),
                closed_size=Decimal("1"),
                avg_entry_price=Decimal("4500"),
                avg_exit_price=Decimal("4400"),
                updated_at=second_at,
            ),
        )

        await store.upsert_closed_pnl(records)

        # A repeated overlapping API sync must
        # never double count the records.
        await store.upsert_closed_pnl(records)

        await store.mark_account_pnl_synced(
            history_start_at=datetime(
                2026,
                9,
                1,
                tzinfo=UTC,
            ),
            last_synced_at=datetime(
                2026,
                9,
                14,
                tzinfo=UTC,
            ),
        )

        summary = await store.get_account_pnl_summary()

        assert summary is not None
        assert summary.realized_pnl == Decimal("14.75")
        assert summary.record_count == 2
        assert summary.positive_count == 1
        assert summary.negative_count == 1

    asyncio.run(run())


def test_closed_pnl_sell_means_closed_long_position() -> None:
    from cautious_crypto_bro.bybit import (
        BybitDemoExecutor,
    )

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    try:
        record = executor._closed_pnl_record_from_item(
            {
                "symbol": "PUMPFUNUSDT",
                "orderId": "close-1",
                "side": "Sell",
                "closedPnl": "-28.5",
                "closedSize": "1000",
                "avgEntryPrice": "0.0037",
                "avgExitPrice": "0.0036",
                "updatedTime": "1789320000000",
            }
        )

        assert record.position_side is Side.LONG

    finally:
        executor.close()


def test_closed_pnl_buy_means_closed_short_position() -> None:
    from cautious_crypto_bro.bybit import (
        BybitDemoExecutor,
    )

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    try:
        record = executor._closed_pnl_record_from_item(
            {
                "symbol": "BTCUSDT",
                "orderId": "close-2",
                "side": "Buy",
                "closedPnl": "5",
                "closedSize": "0.01",
                "avgEntryPrice": "80000",
                "avgExitPrice": "79000",
                "updatedTime": "1789320000000",
            }
        )

        assert record.position_side is Side.SHORT

    finally:
        executor.close()
