import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from uuid import uuid4

import pytest
from test_remaining_concerns import make_intent

from cautious_crypto_bro.domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    Entry,
    EntryType,
    ExecutionPolicy,
    InstrumentContext,
    Side,
    StoreError,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.storage import LATEST_SCHEMA_SQL, IntentStore
from cautious_crypto_bro.storage.schema import ZONE_LEDGER_SCHEMA_SQL
from cautious_crypto_bro.zone_ledger import ZoneExecution, ZoneOrder

pytestmark = pytest.mark.integration


async def zones(database, side=Side.LONG):
    store = IntentStore(database)
    await store.initialize()
    first = make_intent(side=side).model_copy(
        update={
            "entry": Entry(type=EntryType.RANGE, range_low=98, range_high=100),
            "stop_loss": None,
        }
    )
    second = first.model_copy(
        update={
            "intent_id": uuid4(),
            "entry": Entry(type=EntryType.RANGE, range_low=92, range_high=94),
        }
    )
    context = InstrumentContext(
        D("110") if side is Side.LONG else D("80"),
        D("0.1"),
        D("0.001"),
        D("0.001"),
        D("5"),
    )
    policy = ExecutionPolicy(trading_capital_usdt=D("1000"), risk_per_trade_pct=D("1"))
    plans = [
        ExecutionPlanner().plan(intent, policy, context) for intent in (first, second)
    ]
    claim = await store.claim_source(first.source, lease_seconds=300)
    assert await store.create_signal_batch_and_complete_source(
        tuple(zip((first, second), plans, strict=True)), (), claim
    )
    group = uuid4()
    for intent in (first, second):
        await store.register_zone(group, intent.intent_id)
        await store.register_zone(group, intent.intent_id)
    return store, group, plans


def account(quantity, side=Side.LONG, orders=()):
    positions = (
        ()
        if quantity == 0
        else (
            AccountPosition(
                symbol="BTCUSDT",
                side=side,
                size=D(quantity),
                avg_price=D("100"),
                mark_price=D("100"),
                unrealised_pnl=D("0"),
                status="Normal",
                take_profit=None,
                stop_loss=None,
            ),
        )
    )
    return AccountStateSummary(
        as_of=datetime.now(UTC), positions=positions, open_orders=orders
    )


def fill(order, execution_id, quantity, side, closed=False, time=1):
    return ZoneExecution(
        execution_id=execution_id,
        order_id=order.order_id,
        quantity=D(quantity),
        closed_quantity=D(quantity) if closed else D("0"),
        price=D("100"),
        side=side,
        executed_at_ms=time,
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_independent_budgets_and_out_of_order_fills_survive_restart(tmp_path, side):
    async def run():
        database = tmp_path / "state.db"
        store, group, plans = await zones(database, side)
        assert plans[0].stop_loss != plans[1].stop_loss
        initial = await store.zone_group_snapshot(group)
        assert [zone["risk_budget_usdt"] for zone in initial["zones"]] == [
            D("10"),
            D("10"),
        ]
        assert initial["total_risk_budget_usdt"] == D("20")
        assert initial["quantity_reconciled"] is None
        entries = [
            ZoneOrder(
                zone_id=plan.intent_id,
                order_id=f"entry-{n}",
                order_link_id=f"entry-link-{n}",
                role="ENTRY",
                quantity=D("2"),
            )
            for n, plan in enumerate(plans)
        ]
        exit_order = ZoneOrder(
            zone_id=plans[0].intent_id,
            order_id="exit-0",
            order_link_id="exit-link-0",
            role="EXIT",
            quantity=D("2"),
        )
        for order in (*entries, exit_order):
            await store.bind_zone_order(order)
            await store.bind_zone_order(order)
        opposite = Side.SHORT if side is Side.LONG else Side.LONG
        closing = fill(exit_order, "close", "1", opposite, closed=True, time=20)
        await store.record_zone_execution(closing)
        assert (
            "ZONE_OVERCLOSED"
            in (await store.zone_group_snapshot(group, account("0", side)))["issues"]
        )
        for event in (
            fill(entries[1], "second", "2", side, time=15),
            fill(entries[0], "first-a", "0.4", side, time=10),
            fill(entries[0], "first-b", "0.6", side, time=12),
            closing,
        ):
            await store.record_zone_execution(event)
            await store.record_zone_execution(event)
        reopened = IntentStore(database)
        await reopened.initialize()
        result = await reopened.zone_group_snapshot(group, account("2", side))
        by_id = {zone["zone_id"]: zone for zone in result["zones"]}
        assert by_id[str(plans[0].intent_id)]["remaining_quantity"] == 0
        assert by_id[str(plans[1].intent_id)]["remaining_quantity"] == 2
        assert result["quantity_reconciled"]
        assert not result["protection_verified"] and not result["trading_enabled"]
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT COUNT(*) FROM zone_executions").fetchone() == (4,)
            assert db.execute(
                "SELECT COUNT(*) FROM position_strategies"
            ).fetchone() == (0,)

    asyncio.run(run())


def test_identity_conflicts_never_rewrite_frozen_evidence(tmp_path):
    async def run():
        database = tmp_path / "state.db"
        store, group, plans = await zones(database)
        order = ZoneOrder(
            zone_id=plans[0].intent_id,
            order_id="entry",
            order_link_id="known-link",
            role="ENTRY",
            quantity=D("2"),
        )
        await store.bind_zone_order(order)
        event = fill(order, "execution", "1", Side.LONG)
        await asyncio.gather(*(store.record_zone_execution(event) for _ in range(4)))
        for conflict in (
            order.model_copy(update={"zone_id": plans[1].intent_id}),
            order.model_copy(update={"order_id": "other"}),
        ):
            with pytest.raises(StoreError):
                await store.bind_zone_order(conflict)
        with pytest.raises(StoreError):
            await store.record_zone_execution(
                event.model_copy(update={"quantity": D("2")})
            )
        with pytest.raises(StoreError):
            await store.record_zone_execution(
                event.model_copy(
                    update={"execution_id": "unknown", "order_id": "not-bound"}
                )
            )
        with pytest.raises(StoreError):
            await store.record_zone_execution(
                event.model_copy(
                    update={"execution_id": "wrong-side", "side": Side.SHORT}
                )
            )
        with pytest.raises(StoreError):
            await store.register_zone(uuid4(), plans[0].intent_id)
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT COUNT(*) FROM zone_groups").fetchone() == (1,)
            assert db.execute("SELECT COUNT(*) FROM zone_orders").fetchone() == (1,)
            assert db.execute("SELECT COUNT(*) FROM zone_executions").fetchone() == (1,)
        result = await store.zone_group_snapshot(group, account("1"))
        assert result["quantity_reconciled"]
        with sqlite3.connect(database) as db:
            db.execute(
                "UPDATE execution_plans SET payload_json=? WHERE intent_id=?",
                (
                    plans[0]
                    .model_copy(update={"stop_loss": D("80")})
                    .model_dump_json(),
                    str(plans[0].intent_id),
                ),
            )
        with pytest.raises(StoreError):
            await store.register_zone(group, plans[0].intent_id)
        assert (await store.zone_group_snapshot(group))["total_risk_budget_usdt"] == D(
            "20"
        )

    asyncio.run(run())


def test_quantity_matches_do_not_hide_unknown_or_changed_orders(tmp_path):
    async def run():
        store, group, plans = await zones(tmp_path / "state.db")
        binding = ZoneOrder(
            zone_id=plans[0].intent_id,
            order_id="entry",
            order_link_id="known-link",
            role="ENTRY",
            quantity=D("2"),
        )
        await store.bind_zone_order(binding)
        live = AccountOrder(
            symbol="BTCUSDT",
            side=Side.LONG,
            order_type="Limit",
            status="New",
            quantity=D("2"),
            remaining_quantity=D("2"),
            price=D("100"),
            avg_price=None,
            order_id="entry",
            order_link_id="known-link",
            reduce_only=False,
            updated_at=datetime.now(UTC),
        )
        assert (await store.zone_group_snapshot(group, account("0", orders=(live,))))[
            "quantity_reconciled"
        ]
        for changed in (
            replace(live, order_id="external"),
            replace(live, order_link_id="spoofed"),
            replace(live, side=Side.SHORT),
            replace(live, quantity=D("3")),
            replace(live, reduce_only=True),
            replace(live, executed_quantity=D("1"), remaining_quantity=D("1")),
        ):
            result = await store.zone_group_snapshot(
                group, account("0", orders=(changed,))
            )
            assert not result["quantity_reconciled"]
        assert (
            "POSITION_QUANTITY_MISMATCH"
            in (await store.zone_group_snapshot(group, account("1")))["issues"]
        )
        await store.record_zone_execution(fill(binding, "overfill", "3", Side.LONG))
        result = await store.zone_group_snapshot(group, account("3"))
        assert result["issues"] == ("ORDER_OVERFILLED",)

    asyncio.run(run())


def test_schema11_upgrade_preserves_old_rows_and_does_not_invent_allocations(tmp_path):
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as db:
        db.executescript(LATEST_SCHEMA_SQL.replace(ZONE_LEDGER_SCHEMA_SQL, ""))
        db.execute("PRAGMA user_version=11")
        before = {
            row[0]: db.execute("SELECT * FROM " + row[0] + " ORDER BY rowid").fetchall()
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    asyncio.run(IntentStore(database).initialize())
    asyncio.run(IntentStore(database).initialize())
    with sqlite3.connect(database) as db:
        for table, rows in before.items():
            assert (
                db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                == rows
            )
        assert db.execute("SELECT COUNT(*) FROM zone_allocations").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
