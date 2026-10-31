import asyncio
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal as D

import pytest
from pydantic import ValidationError
from test_zone_ledger_integration import account, fill, zones

from cautious_crypto_bro.bybit.normalize import account_order_from_item
from cautious_crypto_bro.domain import Side, StoreError
from cautious_crypto_bro.storage import (
    LATEST_SCHEMA_SQL,
    LATEST_SCHEMA_VERSION,
    IntentStore,
)
from cautious_crypto_bro.storage.schema import ZONE_HISTORY_SCHEMA_SQL
from cautious_crypto_bro.zone_ledger import ZoneOrder

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_native_siblings_keep_exact_ownership_after_restart(tmp_path, side):
    async def run():
        database = tmp_path / "state.db"
        store, group, plans = await zones(database, side)
        children = []
        for n, plan in enumerate(plans):
            quantity = D(n + 1)
            parent = ZoneOrder(
                zone_id=plan.intent_id,
                order_id=f"entry-{n}",
                order_link_id=f"parent-{n}",
                role="ENTRY",
                quantity=quantity,
            )
            await store.bind_zone_order(parent)
            await store.record_zone_execution(
                fill(parent, f"fill-{n}", str(quantity), side)
            )
            for kind, role in (("TakeProfit", "EXIT"), ("StopLoss", "STOP")):
                child = ZoneOrder(
                    zone_id=plan.intent_id,
                    order_id=f"child-{n}-{role}",
                    order_link_id="",
                    parent_order_link_id=parent.order_link_id,
                    role=role,
                    quantity=quantity,
                )
                await store.bind_zone_order(child)
                await store.bind_zone_order(child)
                children.append(
                    account_order_from_item(
                        {
                            "symbol": "BTCUSDT",
                            "side": "Sell" if side is Side.LONG else "Buy",
                            "orderType": "Market",
                            "orderStatus": "Untriggered",
                            "qty": str(quantity),
                            "leavesQty": str(quantity),
                            "cumExecQty": "0",
                            "orderId": child.order_id,
                            "orderLinkId": "",
                            "parentOrderLinkId": parent.order_link_id,
                            "stopOrderType": "Partial" + kind,
                            "triggerPrice": "110",
                            "reduceOnly": True,
                            "closeOnTrigger": True,
                            "updatedTime": "1",
                        }
                    )
                )
        reopened = IntentStore(database)
        await reopened.initialize()
        snapshot = await reopened.zone_group_snapshot(
            group, account("3", side, tuple(children))
        )
        assert snapshot["quantity_reconciled"]
        assert snapshot["total_risk_budget_usdt"] == D("20")
        assert not snapshot["protection_verified"] and not snapshot["trading_enabled"]
        for drift in (
            {"parent_order_link_id": "foreign"},
            {"stop_order_type": "StopLoss"},
            {"order_link_id": "invented"},
        ):
            changed = [replace(children[0], **drift), *children[1:]]
            result = await reopened.zone_group_snapshot(
                group, account("3", side, tuple(changed))
            )
            assert "ORDER_BINDING_MISMATCH" in result["issues"]
        for zone_id, parent_link in (
            (plans[1].intent_id, "parent-0"),
            (plans[0].intent_id, "unknown"),
        ):
            foreign = ZoneOrder(
                zone_id=zone_id,
                order_id="foreign-child",
                order_link_id="",
                parent_order_link_id=parent_link,
                role="STOP",
                quantity=D("1"),
            )
            with pytest.raises(StoreError, match="parent"):
                await reopened.bind_zone_order(foreign)
        original = ZoneOrder(
            zone_id=plans[0].intent_id,
            order_id="child-0-STOP",
            order_link_id="",
            parent_order_link_id="parent-0",
            role="STOP",
            quantity=D("1"),
        )
        with pytest.raises(StoreError, match="identity"):
            await reopened.bind_zone_order(
                original.model_copy(update={"quantity": D("0.5")})
            )
        with pytest.raises(StoreError, match="parent"):
            await reopened.bind_zone_order(
                original.model_copy(
                    update={"order_id": "oversized", "quantity": D("2")}
                )
            )
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT COUNT(*) FROM zone_orders").fetchone() == (6,)
            assert db.execute(
                "SELECT COUNT(*) FROM zone_orders WHERE order_link_id IS NULL"
            ).fetchone() == (4,)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "role,link,parent",
    [
        ("ENTRY", "", "parent"),
        ("STOP", "", ""),
        ("EXIT", "invented", "parent"),
        ("STOP", "", "has space"),
    ],
)
def test_invalid_native_identity_is_rejected(role, link, parent):
    from uuid import uuid4

    with pytest.raises(ValidationError):
        ZoneOrder(
            zone_id=uuid4(),
            order_id="id",
            order_link_id=link,
            parent_order_link_id=parent,
            role=role,
            quantity=D("1"),
        )


def test_populated_schema13_upgrade_preserves_payloads_and_execution_links(tmp_path):
    async def run():
        database = tmp_path / "legacy.db"
        store, group, plans = await zones(database)
        entry = ZoneOrder(
            zone_id=plans[0].intent_id,
            order_id="entry",
            order_link_id="link",
            role="ENTRY",
            quantity=D("1"),
        )
        await store.bind_zone_order(entry)
        await store.record_zone_execution(fill(entry, "fill", "1", Side.LONG))
        with sqlite3.connect(database) as db:
            payload = entry.model_dump(exclude={"parent_order_link_id"}, mode="json")
            db.execute("UPDATE zone_orders SET payload_json=?", (json.dumps(payload),))
            db.executescript("""
                CREATE TABLE old_orders(order_id TEXT PRIMARY KEY,
                    zone_id TEXT NOT NULL REFERENCES zone_allocations(zone_id),
                    order_link_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL);
                INSERT INTO old_orders SELECT * FROM zone_orders;
                DROP TABLE zone_orders;
                ALTER TABLE old_orders RENAME TO zone_orders;
                DROP TABLE zone_history_queries;
                PRAGMA user_version=13;
            """)
            tables = (
                "zone_groups",
                "zone_allocations",
                "zone_orders",
                "zone_executions",
            )
            before = {t: db.execute("SELECT * FROM " + t).fetchall() for t in tables}
        reopened = IntentStore(database)
        await reopened.initialize()
        await reopened.initialize()
        with sqlite3.connect(database) as db:
            for table, rows in before.items():
                assert db.execute("SELECT * FROM " + table).fetchall() == rows
            assert db.execute("PRAGMA user_version").fetchone() == (
                LATEST_SCHEMA_VERSION,
            )
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        await reopened.bind_zone_order(entry)
        child = ZoneOrder(
            zone_id=entry.zone_id,
            order_id="native",
            order_link_id="",
            parent_order_link_id="link",
            role="STOP",
            quantity=D("1"),
        )
        await reopened.bind_zone_order(child)
        await reopened.record_zone_execution(
            fill(child, "close", "1", Side.SHORT, closed=True)
        )
        assert (await reopened.zone_group_snapshot(group, account("0")))[
            "quantity_reconciled"
        ]

    asyncio.run(run())


def test_failed_migration_rolls_back_schema_and_all_rows_then_can_retry(tmp_path):
    database = tmp_path / "invalid.db"
    old_sql = LATEST_SCHEMA_SQL.replace(
        "order_link_id TEXT UNIQUE CHECK(order_link_id IS NULL OR length(order_link_id)>0)",
        "order_link_id TEXT NOT NULL UNIQUE",
    ).replace(ZONE_HISTORY_SCHEMA_SQL, "")
    with sqlite3.connect(database) as db:
        db.executescript(old_sql)
        db.execute(
            "INSERT INTO zone_orders VALUES ('orphan','missing-zone','link','{}')"
        )
        db.execute("PRAGMA user_version=13")
        before = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='zone_orders'"
        ).fetchone()
    with pytest.raises(RuntimeError, match="foreign keys"):
        asyncio.run(IntentStore(database).initialize())
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (13,)
        assert (
            db.execute(
                "SELECT sql FROM sqlite_master WHERE name='zone_orders'"
            ).fetchone()
            == before
        )
        assert db.execute("SELECT order_id FROM zone_orders").fetchall() == [
            ("orphan",)
        ]
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='zone_orders_v14'"
        ).fetchall()
        db.execute("DELETE FROM zone_orders")
    asyncio.run(IntentStore(database).initialize())
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (LATEST_SCHEMA_VERSION,)
