import asyncio
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

import aiosqlite

from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    PlannedOrder,
    Side,
    SourceMessage,
    TradingIntent,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)


def source() -> SourceMessage:
    now = datetime.now(UTC)

    return SourceMessage(
        channel_id=-1001234567890,
        channel_title="Trader",
        channel_username=None,
        message_id=123,
        published_at=now,
        received_at=now,
        text="signal",
    )


def test_failed_source_can_be_reclaimed(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        item = source()

        first = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert first is not None

        duplicate = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert duplicate is None

        assert await store.mark_source_failed(
            item,
            first,
            "temporary error",
        )

        second = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert second is not None
        assert second != first

        assert await store.mark_source_completed(
            item,
            second,
        )

        assert (
            await store.claim_source(
                item,
                lease_seconds=300,
            )
            is None
        )

    asyncio.run(run())


def test_concurrent_claim_has_one_winner(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        item = source()

        results = await asyncio.gather(
            *(
                store.claim_source(
                    item,
                    lease_seconds=300,
                )
                for _ in range(10)
            )
        )

        winners = [result for result in results if result is not None]

        assert len(winners) == 1

    asyncio.run(run())


def test_stale_processing_claim_can_be_recovered(
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"

        store = IntentStore(database_path)
        await store.initialize()

        item = source()

        first = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert first is not None

        async with aiosqlite.connect(database_path) as db:
            await db.execute(
                """
                UPDATE source_messages
                SET updated_at =
                    datetime(
                        'now',
                        '-10 minutes'
                    )
                WHERE
                    channel_id = ?
                    AND message_id = ?
                """,
                (
                    item.channel_id,
                    item.message_id,
                ),
            )
            await db.commit()

        second = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert second is not None
        assert second != first

        # The stale worker must not be able to
        # overwrite the newer claim.
        assert not (
            await store.mark_source_failed(
                item,
                first,
                "old worker",
            )
        )

        assert await store.mark_source_completed(
            item,
            second,
        )

    asyncio.run(run())


def test_stale_worker_cannot_persist_intent(
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"

        store = IntentStore(database_path)
        await store.initialize()

        item = source()

        first_claim = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert first_claim is not None

        async with aiosqlite.connect(database_path) as db:
            await db.execute(
                """
                UPDATE source_messages
                SET updated_at =
                    datetime(
                        'now',
                        '-10 minutes'
                    )
                WHERE
                    channel_id = ?
                    AND message_id = ?
                """,
                (
                    item.channel_id,
                    item.message_id,
                ),
            )
            await db.commit()

        second_claim = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert second_claim is not None
        assert second_claim != first_claim

        intent = TradingIntent(
            source=item,
            symbol="BTCUSDT",
            side=Side.LONG,
            entry=Entry(
                type=EntryType.LIMIT,
                price=100,
            ),
            stop_loss=90,
            take_profit=120,
            summary="test",
            confidence=1,
        )

        policy = ExecutionPolicy(
            trading_capital_usdt=(Decimal("1000")),
            risk_per_trade_pct=(Decimal("1")),
            range_order_count=1,
        )

        plan = ExecutionPlan(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            orders=(
                PlannedOrder(
                    order_type=(ExecutionOrderType.LIMIT),
                    quantity=Decimal("0.1"),
                    price=Decimal("100"),
                    reference_price=(Decimal("100")),
                    take_profit=(Decimal("120")),
                ),
            ),
            stop_loss=Decimal("90"),
            take_profit=Decimal("120"),
            policy=policy,
            planned_max_loss_usdt=(Decimal("1")),
        )

        assert not (
            await store.create_intent_with_plan_and_complete_source(
                intent,
                plan,
                first_claim,
            )
        )

        assert await store.get_intent(intent.intent_id) is None

        assert await store.create_intent_with_plan_and_complete_source(
            intent,
            plan,
            second_claim,
        )

        assert await store.get_intent(intent.intent_id) is not None

        assert (
            await store.claim_source(
                item,
                lease_seconds=300,
            )
            is None
        )

    asyncio.run(run())
