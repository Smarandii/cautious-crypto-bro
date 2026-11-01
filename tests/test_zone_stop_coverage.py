import asyncio
import sqlite3
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_zone_ledger_integration import account, fill, zones

from cautious_crypto_bro.bybit.normalize import account_order_from_item
from cautious_crypto_bro.domain import Side
from cautious_crypto_bro.storage import LATEST_SCHEMA_VERSION, IntentStore
from cautious_crypto_bro.zone_ledger import ZoneOrder

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_matching_quantity_without_stops_does_not_verify_native_coverage(
    tmp_path, side
):
    async def run():
        store, group, plans = await zones(tmp_path / "state.db", side)
        parent = ZoneOrder(
            zone_id=plans[0].intent_id,
            order_id="entry",
            order_link_id="entry-link",
            role="ENTRY",
            quantity=D("1"),
        )
        await store.bind_zone_order(parent)
        await store.record_zone_execution(fill(parent, "entry-fill", "1", side))
        snapshot = await store.zone_group_snapshot(group, account("1", side))
        assert snapshot["quantity_reconciled"]
        assert not snapshot["native_stop_coverage_verified"]
        assert "NATIVE_STOP_UNDERCOVERED" in snapshot["native_stop_issues"]
        assert not snapshot["protection_verified"] and not snapshot["trading_enabled"]

    asyncio.run(run())


async def protected_group(database, side):
    store, group, plans = await zones(database, side)
    parents = []
    stops = []
    for n, (plan, quantity) in enumerate(
        ((plans[0], "1"), (plans[0], "1"), (plans[1], "2"))
    ):
        parent = ZoneOrder(
            zone_id=plan.intent_id,
            order_id=f"entry-{n}",
            order_link_id=f"parent-{n}",
            role="ENTRY",
            quantity=D(quantity),
        )
        await store.bind_zone_order(parent)
        await store.record_zone_execution(fill(parent, f"fill-{n}", quantity, side))
        child = ZoneOrder(
            zone_id=parent.zone_id,
            order_id=f"sl-{n}",
            order_link_id="",
            parent_order_link_id=parent.order_link_id,
            role="STOP",
            quantity=D(quantity),
        )
        await store.bind_zone_order(child)
        stops.append(
            account_order_from_item(
                {
                    "symbol": "BTCUSDT",
                    "side": "Sell" if side is Side.LONG else "Buy",
                    "orderType": "Market",
                    "orderStatus": "Untriggered",
                    "qty": quantity,
                    "leavesQty": quantity,
                    "cumExecQty": "0",
                    "orderId": child.order_id,
                    "orderLinkId": "",
                    "parentOrderLinkId": parent.order_link_id,
                    "stopOrderType": "PartialStopLoss",
                    "triggerPrice": str(plan.stop_loss),
                    "reduceOnly": True,
                    "closeOnTrigger": True,
                    "updatedTime": "1",
                }
            )
        )
        parents.append(parent)
    return store, group, parents, stops


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_distinct_stops_and_parent_quantities_survive_restart_without_writes(
    tmp_path, side
):
    async def run():
        database = tmp_path / "state.db"
        _, group, parents, stops = await protected_group(database, side)
        assert stops[0].trigger_price != stops[2].trigger_price
        reopened = IntentStore(database)
        await reopened.initialize()
        with sqlite3.connect(database) as db:
            before = list(db.iterdump())
        result = await reopened.zone_group_snapshot(
            group, account("4", side, tuple(stops))
        )
        assert result["native_stop_coverage_verified"] and result["quantity_reconciled"]
        assert not result["native_stop_issues"]
        assert {
            r["entry_order_id"]: r["covered_quantity"]
            for r in result["native_stop_coverage"]
        } == {p.order_id: p.quantity for p in parents}
        assert result["total_risk_budget_usdt"] == D("20")
        assert not result["protection_verified"] and not result["trading_enabled"]
        assert (await reopened.zone_group_snapshot(group))[
            "native_stop_coverage_verified"
        ] is None
        with sqlite3.connect(database) as db:
            assert list(db.iterdump()) == before
            assert db.execute("PRAGMA user_version").fetchone() == (
                LATEST_SCHEMA_VERSION,
            )

    asyncio.run(run())


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    "drift",
    [
        "missing",
        "trigger",
        "side",
        "flags",
        "limit",
        "terminal",
        "duplicate",
        "under",
        "parent",
        "foreign",
        "full_stop",
        "full_tp",
        "trailing",
    ],
)
def test_native_stop_drift_never_certifies_coverage(tmp_path, side, drift):
    async def run():
        store, group, _, stops = await protected_group(tmp_path / "state.db", side)
        first = stops[0]
        changes = {
            "trigger": {"trigger_price": first.trigger_price + D("1")},
            "side": {"side": side},
            "flags": {"reduce_only": False, "close_on_trigger": False},
            "limit": {"order_type": "Limit"},
            "terminal": {"status": "Deactivated"},
            "under": {"remaining_quantity": D("0.5")},
            "parent": {"parent_order_link_id": stops[1].parent_order_link_id},
            "foreign": {"order_id": "unbound-id"},
        }
        live = [replace(first, **changes.get(drift, {})), *stops[1:]]
        if drift == "missing":
            live = live[1:]
        if drift == "duplicate":
            live.append(first)
        state = account("4", side, tuple(live))
        if drift in {"full_stop", "full_tp", "trailing"}:
            field = {
                "full_stop": "stop_loss",
                "full_tp": "take_profit",
                "trailing": "trailing_stop",
            }[drift]
            state = replace(
                state, positions=(replace(state.positions[0], **{field: D("1")}),)
            )
        result = await store.zone_group_snapshot(group, state)
        assert not result["native_stop_coverage_verified"]
        assert result["native_stop_issues"]
        # Price, state and coverage have their own diagnostics: do not redefine
        # quantity reconciliation to imply protection.
        if drift in {
            "trigger",
            "flags",
            "limit",
            "terminal",
            "duplicate",
            "under",
            "full_stop",
            "full_tp",
            "trailing",
        }:
            assert result["quantity_reconciled"]

    asyncio.run(run())


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_partial_exit_updates_only_exact_parent_and_rejects_stale_peer(tmp_path, side):
    async def run():
        store, group, parents, stops = await protected_group(
            tmp_path / "state.db", side
        )
        opposite = Side.SHORT if side is Side.LONG else Side.LONG
        exit_order = ZoneOrder(
            zone_id=parents[0].zone_id,
            order_id="tp-0",
            order_link_id="",
            parent_order_link_id=parents[0].order_link_id,
            role="EXIT",
            quantity=D("1"),
        )
        await store.bind_zone_order(exit_order)
        await store.record_zone_execution(
            fill(exit_order, "partial", "0.5", opposite, closed=True)
        )
        before = await store.zone_group_snapshot(
            group, account("3.5", side, tuple(stops))
        )
        assert before["quantity_reconciled"]
        assert "NATIVE_STOP_OVERCOVERED" in before["native_stop_issues"]
        pooled = [stops[0], replace(stops[1], remaining_quantity=D("0.5")), stops[2]]
        # Same zone's total stop quantity is now correct, but its two parents
        # are individually over- and under-covered. Never pool their coverage.
        mismatch = await store.zone_group_snapshot(
            group, account("3.5", side, tuple(pooled))
        )
        assert mismatch["quantity_reconciled"]
        assert "NATIVE_STOP_OVERCOVERED" in mismatch["native_stop_issues"]
        assert "NATIVE_STOP_UNDERCOVERED" in mismatch["native_stop_issues"]
        replacement = ZoneOrder(
            zone_id=parents[0].zone_id,
            order_id="new-sl-0",
            order_link_id="",
            parent_order_link_id=parents[0].order_link_id,
            role="STOP",
            quantity=D("0.5"),
        )
        await store.bind_zone_order(replacement)
        live = [
            replace(
                stops[0],
                order_id=replacement.order_id,
                quantity=D("0.5"),
                remaining_quantity=D("0.5"),
            ),
            *stops[1:],
        ]
        result = await store.zone_group_snapshot(
            group, account("3.5", side, tuple(live))
        )
        assert result["native_stop_coverage_verified"]
        assert [r["remaining_quantity"] for r in result["native_stop_coverage"]] == [
            D("0.5"),
            D("1"),
            D("2"),
        ]
        await store.record_zone_execution(
            fill(exit_order, "rest", "0.5", opposite, closed=True)
        )
        flat = await store.zone_group_snapshot(group, account("3", side, tuple(live)))
        assert flat["quantity_reconciled"]
        assert "STALE_NATIVE_CHILD" in flat["native_stop_issues"]
        clean = await store.zone_group_snapshot(
            group, account("3", side, tuple(live[1:]))
        )
        assert clean["native_stop_coverage_verified"]

    asyncio.run(run())


def test_unscoped_reduction_cannot_guess_parent_even_when_zone_quantity_matches(
    tmp_path,
):
    async def run():
        store, group, parents, stops = await protected_group(
            tmp_path / "state.db", Side.LONG
        )
        reduction = ZoneOrder(
            zone_id=parents[0].zone_id,
            order_id="reduce",
            order_link_id="reduce-link",
            role="EXIT",
            quantity=D("0.5"),
        )
        await store.bind_zone_order(reduction)
        await store.record_zone_execution(
            fill(reduction, "reduction", "0.5", Side.SHORT, closed=True)
        )
        result = await store.zone_group_snapshot(
            group, account("3.5", orders=tuple(stops))
        )
        assert result["quantity_reconciled"]
        assert "UNALLOCATED_PARENT_REDUCTION" in result["native_stop_issues"]
        for row in result["native_stop_coverage"]:
            if row["zone_id"] == str(parents[0].zone_id):
                assert row["remaining_quantity"] is None

    asyncio.run(run())


def test_flat_group_does_not_certify_live_protection(tmp_path):
    async def run():
        store, group, _ = await zones(tmp_path / "state.db")
        result = await store.zone_group_snapshot(group, account("0"))
        assert result["quantity_reconciled"]
        assert not result["native_stop_coverage_verified"]
        assert result["native_stop_issues"] == ("NO_LIVE_ALLOCATION",)

    asyncio.run(run())


@pytest.mark.parametrize("role", ["EXIT", "STOP"])
def test_bound_unscoped_live_closing_order_does_not_certify_coverage(tmp_path, role):
    async def run():
        store, group, parents, stops = await protected_group(
            tmp_path / "state.db", Side.LONG
        )
        binding = ZoneOrder(
            zone_id=parents[0].zone_id,
            order_id="unscoped",
            order_link_id="closing-link",
            role=role,
            quantity=D("1"),
        )
        await store.bind_zone_order(binding)
        closing = replace(
            stops[0],
            order_id=binding.order_id,
            order_link_id=binding.order_link_id,
            parent_order_link_id="",
            stop_order_type="",
            trigger_price=None,
        )
        result = await store.zone_group_snapshot(
            group, account("4", orders=(*stops, closing))
        )
        assert result["quantity_reconciled"]
        assert not result["native_stop_coverage_verified"]
        assert "UNSCOPED_CLOSING_ORDER" in result["native_stop_issues"]

    asyncio.run(run())


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("scenario", ["extra_stop", "overclosed_parent"])
def test_parent_conservation_cannot_be_replaced_by_zone_totals(
    tmp_path, side, scenario
):
    async def run():
        store, group, parents, stops = await protected_group(
            tmp_path / "state.db", side
        )
        parent = parents[0]
        live = list(stops)
        net = "4"
        if scenario == "extra_stop":
            extra = ZoneOrder(
                zone_id=parent.zone_id,
                order_id="extra-sl",
                order_link_id="",
                parent_order_link_id=parent.order_link_id,
                role="STOP",
                quantity=D("1"),
            )
            await store.bind_zone_order(extra)
            live.append(replace(stops[0], order_id=extra.order_id))
            expected = "NATIVE_STOP_OVERCOVERED"
        else:
            opposite = Side.SHORT if side is Side.LONG else Side.LONG
            for n in range(2):
                closing = ZoneOrder(
                    zone_id=parent.zone_id,
                    order_id=f"exit-{n}",
                    order_link_id="",
                    parent_order_link_id=parent.order_link_id,
                    role="EXIT",
                    quantity=D("0.75"),
                )
                await store.bind_zone_order(closing)
                await store.record_zone_execution(
                    fill(closing, f"close-{n}", "0.75", opposite, closed=True)
                )
            net = "2.5"
            expected = "PARENT_OVERCLOSED"
        result = await store.zone_group_snapshot(group, account(net, side, tuple(live)))
        assert result["quantity_reconciled"]
        assert not result["native_stop_coverage_verified"]
        assert expected in result["native_stop_issues"]

    asyncio.run(run())
