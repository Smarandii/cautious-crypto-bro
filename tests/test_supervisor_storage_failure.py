"""Real SQLite write failures must stop the remaining symbol sweep."""

import asyncio
import sqlite3
from dataclasses import replace

import pytest
from test_position_supervisor import Executor, _entry_order, _with_entry_ttl, plan
from test_remaining_concerns import make_intent

from cautious_crypto_bro.domain import StoreError
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure", ["trigger", "locked"])
def test_sqlite_write_failure_after_cancel_stops_other_symbols(tmp_path, failure):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        plans = [
            _with_entry_ttl(plan(symbol), ttl_minutes=1, age_minutes=10)
            for symbol in ("BTCUSDT", "ETHUSDT")
        ]
        for index, item in enumerate(plans, 1):
            intent = make_intent().model_copy(
                update={
                    "intent_id": item.intent_id,
                    "symbol": item.symbol,
                    "source": make_intent().source.model_copy(
                        update={"message_id": index}
                    ),
                }
            )
            claim = await store.claim_source(intent.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source(
                ((intent, item),), (), claim
            )
            await store.ensure_position_strategy(item)

        blocker = sqlite3.connect(database)

        class FailingStorageExchange(Executor):
            async def cancel_order(self, symbol, order_id):
                await super().cancel_order(symbol, order_id)
                if symbol == "BTCUSDT":
                    if failure == "locked":
                        blocker.execute("BEGIN IMMEDIATE")
                    else:
                        blocker.execute(
                            "CREATE TRIGGER reject_strategy_write "
                            "BEFORE UPDATE ON position_strategies "
                            "BEGIN SELECT RAISE(ABORT, 'strategy write rejected'); END"
                        )
                        blocker.commit()

        executor = FailingStorageExchange()
        entries = tuple(
            _entry_order(item, "e1", order_id=f"{item.symbol}-entry") for item in plans
        )
        executor.state = replace(executor.state, positions=(), open_orders=entries)
        lock = asyncio.Lock()
        supervisor = PositionSupervisor(
            store=store, executor=executor, mutation_lock=lock
        )
        try:
            with pytest.raises(StoreError) as caught:
                await supervisor.reconcile_once()
            assert isinstance(caught.value.__cause__, sqlite3.Error)
            assert executor.cancelled_orders == [entries[0].order_id]
            assert executor.state.open_orders == (entries[1],)
            assert not lock.locked()
        finally:
            blocker.rollback()
            blocker.close()

    asyncio.run(run())
