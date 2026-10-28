"""Zone-execution recording from observed fills (#123's ledger substrate)."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from fakes import FakeAccountGateway
from test_zone_ledger_integration import zones

from cautious_crypto_bro.domain import Side
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import ZoneExecution, ZoneOrder

pytestmark = pytest.mark.integration


class RecordingGateway(FakeAccountGateway):
    def __init__(self, rows):
        super().__init__()
        self.execution_rows["BTCUSDT"] = rows


def _row(
    exec_id,
    order_id,
    side,
    qty,
    closed,
    price,
    exec_time,
    exec_type="Trade",
):
    return {
        "execId": exec_id,
        "execType": exec_type,
        "orderId": order_id,
        "orderLinkId": f"link-{order_id}",
        "symbol": "BTCUSDT",
        "side": side,
        "execQty": str(qty),
        "closedSize": str(closed),
        "execPrice": str(price),
        "execFee": "0.01",
        "execTime": str(exec_time),
    }


def _supervisor(store, gateway):
    return PositionSupervisor(
        store=store,
        executor=gateway,
        mutation_lock=asyncio.Lock(),
    )


async def _bind_entries(store, group_id, plans):
    for plan in plans:
        for order in plan.orders:
            await store.bind_zone_order(
                ZoneOrder(
                    zone_id=plan.intent_id,
                    order_id=f"id-{plan.intent_id.hex[:6]}-{order.name.lower()}",
                    order_link_id=f"ccb-v2-{plan.intent_id.hex[:20]}-{order.name.lower()}",
                    role="ENTRY",
                    quantity=order.quantity,
                )
            )


async def _recorded_rows(store, group_id):
    with sqlite3.connect(store._database_path) as db:
        rows = db.execute(
            "SELECT e.payload_json FROM zone_executions e "
            "JOIN zone_orders o USING(order_id) "
            "JOIN zone_allocations z USING(zone_id) WHERE z.group_id=?",
            (str(group_id),),
        ).fetchall()
    return [ZoneExecution.model_validate_json(row[0]) for row in rows]


def test_observed_fills_bind_to_enrolled_zones_idempotently(tmp_path) -> None:
    async def run():
        database = tmp_path / "zones.sqlite3"
        store, group_id, plans = await zones(database)
        await _bind_entries(store, group_id, plans)
        first_order_id = f"id-{plans[0].intent_id.hex[:6]}-e1"
        second_order_id = f"id-{plans[1].intent_id.hex[:6]}-e1"
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        gateway = RecordingGateway(
            [
                _row("exec-1", first_order_id, "Buy", "1", "0", "100", now_ms - 1000),
                # A funding settlement is not an order execution.
                _row(
                    "exec-f",
                    first_order_id,
                    "Buy",
                    "0",
                    "0",
                    "100",
                    now_ms - 900,
                    exec_type="Funding",
                ),
                # An unknown order identity is never adopted.
                _row("exec-x", "foreign-order", "Buy", "5", "0", "100", now_ms - 800),
                _row("exec-2", second_order_id, "Buy", "2", "0", "100", now_ms - 500),
            ]
        )
        supervisor = _supervisor(store, gateway)

        await supervisor.reconcile_once()
        recorded = await _recorded_rows(store, group_id)
        assert sorted(row.execution_id for row in recorded) == [
            "exec-1",
            "exec-2",
        ]
        entry_row = next(r for r in recorded if r.execution_id == "exec-1")
        assert entry_row.side is Side.LONG
        assert entry_row.closed_quantity == D("0")
        assert entry_row.quantity == D("1")
        assert entry_row.executed_at_ms == now_ms - 1000

        # A second pass inside the same window is throttled away: no new
        # exchange read, no duplicate evidence.
        calls_after_first = len(gateway.execution_history_calls)
        await supervisor.reconcile_once()
        assert len(gateway.execution_history_calls) == calls_after_first
        assert sorted(
            r.execution_id for r in await _recorded_rows(store, group_id)
        ) == [
            "exec-1",
            "exec-2",
        ]

        # A restarted process catches up incrementally from the durable
        # watermark: only newer evidence is fetched or recorded.
        gateway.execution_rows["BTCUSDT"].append(
            _row("exec-3", second_order_id, "Buy", "2", "0", "100", now_ms - 100)
        )
        restarted = _supervisor(store, gateway)
        await restarted.reconcile_once()
        assert gateway.execution_history_calls[-1][1] == now_ms - 499
        final = await _recorded_rows(store, group_id)
        assert sorted(r.execution_id for r in final) == [
            "exec-1",
            "exec-2",
            "exec-3",
        ]

    asyncio.run(run())


def test_direction_conflict_halts_the_group_without_losing_evidence(tmp_path) -> None:
    async def run():
        database = tmp_path / "conflict.sqlite3"
        store, group_id, plans = await zones(database)
        await _bind_entries(store, group_id, plans)
        first_order_id = f"id-{plans[0].intent_id.hex[:6]}-e1"
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        gateway = RecordingGateway(
            [
                # Wrong direction for an ENTRY binding of a LONG group: the
                # durable conflict must halt the group's evidence ordering.
                _row("exec-bad", first_order_id, "Sell", "1", "1", "100", now_ms - 700),
                _row(
                    "exec-ok",
                    f"id-{plans[1].intent_id.hex[:6]}-e1",
                    "Buy",
                    "2",
                    "0",
                    "100",
                    now_ms - 600,
                ),
            ]
        )
        supervisor = _supervisor(store, gateway)

        await supervisor.reconcile_once()

        # The later good row stays unrecorded: the watermark cannot jump past
        # the conflicting evidence, so no evidence is silently dropped.
        recorded = await _recorded_rows(store, group_id)
        assert [row.execution_id for row in recorded] == []

        # A restarted process retries the same window and halts again:
        # the failure stays visible instead of disappearing.
        restarted = _supervisor(store, gateway)
        await restarted.reconcile_once()
        assert [row.execution_id for row in await _recorded_rows(store, group_id)] == []

        # Once the conflicting source row no longer appears in history, the
        # same catch-up records the good evidence without a gap.
        gateway.execution_rows["BTCUSDT"] = [
            row
            for row in gateway.execution_rows["BTCUSDT"]
            if row["execId"] != "exec-bad"
        ]
        third = _supervisor(store, gateway)
        await third.reconcile_once()
        final = await _recorded_rows(store, group_id)
        assert [row.execution_id for row in final] == ["exec-ok"]

    asyncio.run(run())


def test_empty_ledger_makes_no_exchange_reads(tmp_path) -> None:
    async def run():
        database = tmp_path / "empty.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        gateway = FakeAccountGateway()
        supervisor = _supervisor(store, gateway)

        await supervisor.reconcile_once()

        assert gateway.execution_history_calls == []

    asyncio.run(run())


def test_exit_binding_records_opposite_side_with_closed_quantity(tmp_path) -> None:
    async def run():
        database = tmp_path / "exit.sqlite3"
        store, group_id, plans = await zones(database)
        await _bind_entries(store, group_id, plans)
        entry_order_id = f"id-{plans[0].intent_id.hex[:6]}-e1"
        await store.bind_zone_order(
            ZoneOrder(
                zone_id=plans[0].intent_id,
                order_id="exit-order-1",
                order_link_id=f"ccb-v2-{plans[0].intent_id.hex[:20]}-t1r1",
                role="EXIT",
                quantity=plans[0].orders[0].quantity,
            )
        )
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        gateway = RecordingGateway(
            [
                _row(
                    "exec-e1",
                    entry_order_id,
                    "Buy",
                    "1",
                    "0",
                    "100",
                    now_ms - 2000,
                ),
                _row(
                    "exec-x1",
                    "exit-order-1",
                    "Sell",
                    "1",
                    "1",
                    "120",
                    now_ms - 1000,
                ),
            ]
        )
        supervisor = _supervisor(store, gateway)

        await supervisor.reconcile_once()

        recorded = {
            row.execution_id: row for row in await _recorded_rows(store, group_id)
        }
        assert recorded["exec-e1"].side is Side.LONG
        exit_row = recorded["exec-x1"]
        assert exit_row.side is Side.SHORT
        assert exit_row.closed_quantity == D("1")
        assert exit_row.quantity == D("1")

    asyncio.run(run())
