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
            await store.create_signal_batch_and_complete_source(
                ((intent, plan),),
                (),
                first_claim,
            )
        )

        assert await store.get_intent(intent.intent_id) is None

        assert await store.create_signal_batch_and_complete_source(
            ((intent, plan),),
            (),
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


def _trade_pair(
    item: SourceMessage,
    *,
    symbol: str,
    side: Side,
    entry: str,
    stop: str,
    target: str,
) -> tuple[
    TradingIntent,
    ExecutionPlan,
]:
    entry_decimal = Decimal(entry)
    stop_decimal = Decimal(stop)
    target_decimal = Decimal(target)

    intent = TradingIntent(
        source=item,
        symbol=symbol,
        side=side,
        entry=Entry(
            type=EntryType.LIMIT,
            price=float(entry_decimal),
        ),
        stop_loss=float(stop_decimal),
        take_profit=float(target_decimal),
        summary=symbol,
        confidence=1,
    )

    policy = ExecutionPolicy(
        trading_capital_usdt=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        range_order_count=1,
    )

    plan = ExecutionPlan(
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        side=intent.side,
        orders=(
            PlannedOrder(
                order_type=ExecutionOrderType.LIMIT,
                quantity=Decimal("0.1"),
                price=entry_decimal,
                reference_price=entry_decimal,
                take_profit=target_decimal,
            ),
        ),
        stop_loss=stop_decimal,
        take_profit=target_decimal,
        policy=policy,
        planned_max_loss_usdt=Decimal("1"),
    )

    return intent, plan


def test_multi_intent_persistence_rolls_back_entire_batch(
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"

        store = IntentStore(database_path)
        await store.initialize()

        item = source()

        claim = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert claim is not None

        first = _trade_pair(
            item,
            symbol="BTCUSDT",
            side=Side.LONG,
            entry="100",
            stop="90",
            target="120",
        )

        second = _trade_pair(
            item,
            symbol="ETHUSDT",
            side=Side.SHORT,
            entry="200",
            stop="220",
            target="170",
        )

        bad_second = (
            second[0],
            second[1].model_copy(update={"intent_id": (first[0].intent_id)}),
        )

        try:
            await store.create_signal_batch_and_complete_source(
                (
                    first,
                    bad_second,
                ),
                (),
                claim,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Expected mismatched plan to fail")

        assert await store.get_intent(first[0].intent_id) is None

        async with aiosqlite.connect(database_path) as db:
            cursor = await db.execute(
                """
                SELECT status, claim_token
                FROM source_messages
                WHERE channel_id = ?
                  AND message_id = ?
                """,
                (
                    item.channel_id,
                    item.message_id,
                ),
            )

            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == "PROCESSING"
        assert row[1] == claim

    asyncio.run(run())
