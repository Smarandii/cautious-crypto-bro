import asyncio
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

import aiosqlite
import pytest

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
from cautious_crypto_bro.storage import IntentStore


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


async def _source_row(database_path, item: SourceMessage):
    async with aiosqlite.connect(database_path) as db:
        cursor = await db.execute(
            """
            SELECT status, claim_token, attempt_count, last_error
            FROM source_messages
            WHERE channel_id = ? AND message_id = ?
            """,
            (item.channel_id, item.message_id),
        )
        return await cursor.fetchone()


def test_claim_source_rejects_non_positive_lease(tmp_path) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        for lease in (0, -1):
            with pytest.raises(ValueError, match="lease must be positive"):
                await store.claim_source(source(), lease_seconds=lease)

    asyncio.run(run())


def test_mark_source_completed_finalizes_and_blocks_reclaim(
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"
        store = IntentStore(database_path)
        await store.initialize()

        item = source()
        claim = await store.claim_source(item, lease_seconds=300)
        assert claim is not None

        assert await store.mark_source_completed(item, claim) is True

        row = await _source_row(database_path, item)
        assert row[0] == "COMPLETED"
        assert row[1] is None

        # A completed post is never re-claimed, so extraction cannot run twice
        # for the same message.
        assert await store.claim_source(item, lease_seconds=300) is None

    asyncio.run(run())


def test_mark_source_completed_rejects_foreign_claim(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        item = source()
        claim = await store.claim_source(item, lease_seconds=300)
        assert claim is not None

        assert await store.mark_source_completed(item, "not-the-token") is False

        row = await _source_row(tmp_path / "state.sqlite3", item)
        assert row[0] == "PROCESSING"
        assert row[1] == claim

    asyncio.run(run())


def test_mark_source_failed_records_error_and_allows_retry(
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"
        store = IntentStore(database_path)
        await store.initialize()

        item = source()
        claim = await store.claim_source(item, lease_seconds=300)
        assert claim is not None

        assert await store.mark_source_failed(item, claim, "download failed")

        row = await _source_row(database_path, item)
        assert row[0] == "FAILED"
        assert row[1] is None
        assert row[2] == 1
        assert row[3] == "download failed"

        # The retry path the README promises for failed history and image
        # downloads: a failed row is claimable again and the attempt advances.
        retry = await store.claim_source(item, lease_seconds=300)
        assert retry is not None
        assert retry != claim

        row = await _source_row(database_path, item)
        assert row[0] == "PROCESSING"
        assert row[2] == 2
        assert row[3] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("  ", "unknown processing failure"),
        ("", "unknown processing failure"),
        ("\n\t ", "unknown processing failure"),
        ("real failure", "real failure"),
    ],
)
def test_mark_source_failed_normalises_blank_errors(
    tmp_path,
    reported,
    expected,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        item = source()
        claim = await store.claim_source(item, lease_seconds=300)
        assert claim is not None

        assert await store.mark_source_failed(item, claim, reported) is True

        row = await _source_row(tmp_path / "state.sqlite3", item)
        assert row[3] == expected

    asyncio.run(run())


def test_mark_source_failed_caps_error_length(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"
        store = IntentStore(database_path)
        await store.initialize()

        item = source()
        claim = await store.claim_source(item, lease_seconds=300)
        assert claim is not None

        await store.mark_source_failed(item, claim, "x" * 5000)

        row = await _source_row(database_path, item)
        assert row[3] is not None
        assert len(row[3]) == 2000

    asyncio.run(run())


def test_reset_stale_processing_sources_fails_expired_leases(
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"
        store = IntentStore(database_path)
        await store.initialize()

        stale = source()
        assert await store.claim_source(stale, lease_seconds=300) is not None

        async with aiosqlite.connect(database_path) as db:
            await db.execute(
                """
                UPDATE source_messages
                SET updated_at = datetime('now', '-10 minutes')
                WHERE channel_id = ? AND message_id = ?
                """,
                (stale.channel_id, stale.message_id),
            )
            await db.commit()

        assert await store.reset_stale_processing_sources(60) == 1

        row = await _source_row(database_path, stale)
        assert row[0] == "FAILED"
        assert row[3] == "Source processing lease expired during previous run"

        # A lease longer than the elapsed time leaves the row alone.
        assert await store.reset_stale_processing_sources(3600) == 0
        assert (await _source_row(database_path, stale))[0] == "FAILED"

    asyncio.run(run())


def test_reset_stale_processing_sources_rejects_non_positive_lease(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        for lease in (0, -1):
            with pytest.raises(ValueError, match="lease_seconds must be positive"):
                await store.reset_stale_processing_sources(lease)

    asyncio.run(run())


def test_batch_requires_at_least_one_output(tmp_path) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        item = source()
        claim = await store.claim_source(item, lease_seconds=300)
        assert claim is not None

        with pytest.raises(ValueError, match="At least one signal output"):
            await store.create_signal_batch_and_complete_source((), (), claim)

    asyncio.run(run())


def test_batch_rejects_outputs_from_different_posts(tmp_path) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        first_post = source()
        other_post = source().model_copy(update={"message_id": 999})

        claim = await store.claim_source(first_post, lease_seconds=300)
        assert claim is not None

        pair = _trade_pair(
            first_post,
            symbol="BTCUSDT",
            side=Side.LONG,
            entry="100",
            stop="90",
            target="120",
        )
        stray = _trade_pair(
            other_post,
            symbol="ETHUSDT",
            side=Side.LONG,
            entry="200",
            stop="180",
            target="240",
        )

        with pytest.raises(ValueError, match="same Telegram post"):
            await store.create_signal_batch_and_complete_source(
                (pair, stray),
                (),
                claim,
            )

    asyncio.run(run())


def test_insert_rejects_plan_for_another_intent(tmp_path) -> None:
    """Guards the write itself, not just the pre-flight validation."""

    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        item = source()
        intent, plan = _trade_pair(
            item,
            symbol="BTCUSDT",
            side=Side.LONG,
            entry="100",
            stop="90",
            target="120",
        )

        stray = _trade_pair(
            item,
            symbol="BTCUSDT",
            side=Side.LONG,
            entry="100",
            stop="90",
            target="120",
        )

        async with aiosqlite.connect(store._database_path) as db:
            with pytest.raises(ValueError, match="does not match"):
                await store._insert_intent_with_plan(db, intent, stray[1])

    asyncio.run(run())


def test_batch_discards_inserts_when_claim_cannot_be_finished(
    tmp_path,
    monkeypatch,
) -> None:
    """A claim lost between the check and the finish must persist nothing."""

    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"
        store = IntentStore(database_path)
        await store.initialize()

        item = source()
        claim = await store.claim_source(item, lease_seconds=300)
        assert claim is not None

        pair = _trade_pair(
            item,
            symbol="BTCUSDT",
            side=Side.LONG,
            entry="100",
            stop="90",
            target="120",
        )

        async def refuse(*args, **kwargs) -> bool:
            return False

        monkeypatch.setattr(store, "_finish_source", refuse)

        assert (
            await store.create_signal_batch_and_complete_source((pair,), (), claim)
            is False
        )

        assert await store.get_intent(pair[0].intent_id) is None

        row = await _source_row(database_path, item)
        assert row[0] == "PROCESSING"
        assert row[1] == claim

    asyncio.run(run())


def test_batch_rolls_back_when_insert_raises(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "state.sqlite3"
        store = IntentStore(database_path)
        await store.initialize()

        item = source()
        claim = await store.claim_source(item, lease_seconds=300)
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

        original = store._insert_intent_with_plan
        calls = 0

        async def flaky(db, intent, plan) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("storage exploded")
            await original(db, intent, plan)

        monkeypatch.setattr(store, "_insert_intent_with_plan", flaky)

        with pytest.raises(RuntimeError, match="storage exploded"):
            await store.create_signal_batch_and_complete_source(
                (first, second),
                (),
                claim,
            )

        # The first insert happened before the failure and must not survive.
        assert await store.get_intent(first[0].intent_id) is None
        assert await store.get_intent(second[0].intent_id) is None

        row = await _source_row(database_path, item)
        assert row[0] == "PROCESSING"
        assert row[1] == claim

    asyncio.run(run())
