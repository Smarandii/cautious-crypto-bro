"""Ordinary entry acceptance must survive a lost exchange acknowledgement."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from decimal import Decimal

import pytest
from test_quantity_limited_entry_integration import QuantityLimitedExchange
from test_remaining_concerns import executor_for, prepare
from test_tier_preflight_integration import run_entry

from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    IntentStatus,
    OpenRelation,
    Side,
    StrategyStatus,
)
from cautious_crypto_bro.forensic_pnl import reconcile
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import zone_group_id

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("bound_close", [True, False])
def test_failed_ordinary_fill_reaches_forensic_pipeline(
    tmp_path, monkeypatch, side, bound_close
):
    from test_partial_entry_forensics_integration import (
        executed_attempt,
        export_attempt,
    )

    database = tmp_path / "forensic.sqlite3"
    exchange, plan, _, _ = asyncio.run(
        executed_attempt(database, side, "batch_ack", bound_close, ceiling="1000000")
    )
    assert all(s.index == 0 for s in plan.entry_slices)
    bundle = tmp_path / "ordinary.zip"
    export_attempt(database, exchange, bundle, monkeypatch)
    position = reconcile(bundle)[0]
    assert position["attempt_status"] == "FAILED"
    assert position["entry_identity_complete"] is True
    assert position["complete"] is bound_close


def test_cancellation_after_ordinary_acceptance_drains_without_replay(tmp_path):
    class BlockingExchange(QuantityLimitedExchange):
        def __init__(self, database):
            super().__init__(database)
            self.max_market_qty = self.max_limit_qty = Decimal("1000000")
            self.started, self.release = threading.Event(), threading.Event()

        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/v5/order/create" and not self.release.is_set():
                self.started.set()
                assert self.release.wait(10), "Test did not release accepted primary"
            return response

    async def run():
        database = tmp_path / "cancellation.sqlite3"
        exchange = BlockingExchange(database)
        execution = asyncio.create_task(run_entry(database, exchange, EntryType.MARKET))
        try:
            assert await asyncio.to_thread(exchange.started.wait, 3)
            execution.cancel()
            await asyncio.sleep(0)
            assert not execution.done()
            exchange.release.set()
            with pytest.raises(asyncio.CancelledError):
                await execution
            store = IntentStore(database)
            records = await store.get_active_position_strategies()
            assert len(records) == 1
            _, plan = records[0]
            bindings = await store.zone_bound_orders(
                zone_group_id(plan.symbol, plan.side)
            )
            assert len(bindings) == len(exchange.accepted_entries) == 3
            with closing(sqlite3.connect(database)) as db:
                assert (
                    db.execute("SELECT status FROM intents").fetchone()[0] == "EXECUTED"
                )
        finally:
            exchange.release.set()
            await asyncio.gather(execution, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("entry_type", "failure"),
    [
        (EntryType.MARKET, "primary_ack"),
        (EntryType.MARKET, "batch_ack"),
        (EntryType.LIMIT, "batch_ack"),
        (EntryType.RANGE, "batch_ack"),
    ],
)
def test_unsplit_accepted_entries_retain_exact_ownership(tmp_path, entry_type, failure):
    async def run():
        database = tmp_path / "ordinary.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.max_market_qty = exchange.max_limit_qty = Decimal("1000000")
        exchange.failure = failure
        store, intent, _, outcome = await run_entry(database, exchange, entry_type)
        assert outcome.status is IntentStatus.FAILED
        assert exchange.failed_once and exchange.accepted_entries
        records = await store.get_active_position_strategies()
        assert len(records) == 1
        strategy, plan = records[0]
        assert strategy.status is StrategyStatus.UNCERTAIN
        accepted = {
            request["orderLinkId"]: Decimal(request["qty"])
            for request in exchange.accepted_entries
        }
        assert all(link.endswith(("-e1", "-e2", "-e3")) for link in accepted)
        bindings = await store.zone_bound_orders(zone_group_id(plan.symbol, plan.side))
        assert {b.order_link_id: b.quantity for b in bindings} == accepted
        assert (await store.get_execution_plan(intent.intent_id)) is not None
        with closing(sqlite3.connect(database)) as db:
            db.execute("DELETE FROM zone_orders")
            db.commit()
        before = list(exchange.accepted_entries)
        with executor_for(exchange) as executor:
            restarted = IntentStore(database)
            supervisor = PositionSupervisor(
                store=restarted, executor=executor, mutation_lock=asyncio.Lock()
            )
            await supervisor.reconcile_once()
            bindings = await restarted.zone_bound_orders(
                zone_group_id(plan.symbol, plan.side)
            )
        assert {b.order_link_id: b.quantity for b in bindings} == accepted
        assert exchange.accepted_entries == before

    asyncio.run(run())


@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("entry_type", list(EntryType))
@pytest.mark.parametrize("fill_mode", ["full", "partial"])
def test_ordinary_prepared_requests_are_persisted_with_original_links(
    tmp_path, side, entry_type, fill_mode
):
    async def run():
        database = tmp_path / "prepared.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.side, exchange.mark = side, Decimal("100")
        exchange.max_market_qty = exchange.max_limit_qty = Decimal("1000000")
        exchange.failure = "batch_ack"
        exchange.fill_mode = fill_mode
        entry = Entry(type=EntryType.MARKET)
        if entry_type is EntryType.LIMIT:
            entry = Entry(type=entry_type, price=100)
        elif entry_type is EntryType.RANGE:
            entry = Entry(
                type=entry_type,
                range_low=98 if side is Side.LONG else 100,
                range_high=100 if side is Side.LONG else 102,
            )
        with executor_for(exchange) as executor:
            store, intent, original, coordinator, _ = await prepare(
                database,
                exchange,
                executor,
                side=side,
                mode=ApprovalMode.AUTO,
                relation=OpenRelation.NEW,
                entry=entry,
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.AUTO
            )
        assert exchange.failed_once and outcome.status is IntentStatus.FAILED
        plan = await store.get_execution_plan(intent.intent_id)
        assert plan is not None and len(plan.entry_slices) == 3
        assert all(s.index == 0 for s in plan.entry_slices)
        assert plan.policy == original.policy and plan.stop_loss == original.stop_loss
        bindings = await store.zone_bound_orders(zone_group_id(plan.symbol, side))
        assert {b.order_link_id: b.quantity for b in bindings} == {
            request["orderLinkId"]: Decimal(request["qty"])
            for request in exchange.accepted_entries
        }

    asyncio.run(run())


@pytest.mark.parametrize("side", list(Side))
def test_failed_remaining_plan_write_recovers_durable_primary(tmp_path, side):
    async def run():
        database = tmp_path / "remaining.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.side, exchange.mark = side, Decimal("100")
        exchange.max_market_qty = exchange.max_limit_qty = Decimal("1000000")
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, _ = await prepare(
                database,
                exchange,
                executor,
                side=side,
                mode=ApprovalMode.AUTO,
                relation=OpenRelation.NEW,
            )
            update = store.update_execution_plan

            async def fail_after_acceptance(plan):
                if exchange.accepted_entries:
                    raise RuntimeError("Synthetic remaining-plan write failure")
                await update(plan)

            store.update_execution_plan = fail_after_acceptance
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.AUTO
            )
        assert outcome.status is IntentStatus.FAILED
        assert len(exchange.accepted_entries) == 1
        plan = await store.get_execution_plan(intent.intent_id)
        bindings = await store.zone_bound_orders(zone_group_id(plan.symbol, side))
        request = exchange.accepted_entries[0]
        assert {b.order_link_id: b.quantity for b in bindings} == {
            request["orderLinkId"]: Decimal(request["qty"])
        }

    asyncio.run(run())


@pytest.mark.parametrize("entry_type", list(EntryType))
def test_unsplit_mapping_must_be_durable_before_any_exchange_mutation(
    tmp_path, entry_type
):
    async def run():
        database = tmp_path / "unavailable.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.max_market_qty = exchange.max_limit_qty = Decimal("1000000")
        _, _, _, outcome = await run_entry(
            database, exchange, entry_type, saved_leverage="5", fail_persist=True
        )
        assert outcome.status is IntentStatus.FAILED
        assert not exchange.accepted_entries
        assert not exchange.selected_leverages

    asyncio.run(run())
