"""Observed HTTP executions must produce truthful allocation/protection snapshots."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D

import httpx
import pytest
from test_remaining_concerns import executor_for
from test_workflow_integration import ExchangeTransport
from test_zone_execution_recording import (
    _make_replay_due,
    _recorded_rows,
    _row,
    _supervisor,
)
from test_zone_ledger_integration import zones

from cautious_crypto_bro.domain import Side
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import ZoneOrder

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("side", list(Side))
def test_http_fills_survive_restart_into_independent_protection_snapshots(
    tmp_path, side
):
    class Exchange(ExchangeTransport):
        pages = ()
        stops = ()

        def handle(self, request):
            assert request.method == "GET", "Evidence recording must not mutate Bybit"
            if request.url.path == "/v5/execution/list":
                second = request.url.params.get("cursor") == "second"
                result = {
                    "list": self.pages[int(second)],
                    "nextPageCursor": "" if second else "second",
                }
                return httpx.Response(200, json={"retCode": 0, "result": result})
            if request.url.path == "/v5/order/realtime":
                return httpx.Response(
                    200,
                    json={
                        "retCode": 0,
                        "result": {"list": self.stops, "nextPageCursor": ""},
                    },
                )
            return super().handle(request)

    async def run():
        database = tmp_path / "snapshots.sqlite3"
        store, group, plans = await zones(database, side)
        parents = []
        stops = []
        exit_side = "Sell" if side is Side.LONG else "Buy"
        for index, (plan, remaining) in enumerate(
            zip(plans, ("0.4", "0.7"), strict=True)
        ):
            parent = ZoneOrder(
                zone_id=plan.intent_id,
                order_id=f"entry-{index}",
                order_link_id=f"parent-{index}",
                role="ENTRY",
                quantity=D("1"),
            )
            await store.bind_zone_order(parent)
            await store.bind_zone_order(
                ZoneOrder(
                    zone_id=plan.intent_id,
                    order_id=f"stop-{index}",
                    order_link_id="",
                    parent_order_link_id=parent.order_link_id,
                    role="STOP",
                    quantity=D(remaining),
                )
            )
            parents.append(parent)
            stops.append(
                {
                    "symbol": "BTCUSDT",
                    "side": exit_side,
                    "orderType": "Market",
                    "orderStatus": "Untriggered",
                    "qty": remaining,
                    "leavesQty": remaining,
                    "cumExecQty": "0",
                    "orderId": f"stop-{index}",
                    "orderLinkId": "",
                    "parentOrderLinkId": parent.order_link_id,
                    "stopOrderType": "PartialStopLoss",
                    "triggerPrice": str(plan.stop_loss),
                    "reduceOnly": True,
                    "closeOnTrigger": True,
                    "updatedTime": "1",
                }
            )
        await store.bind_zone_order(
            ZoneOrder(
                zone_id=plans[0].intent_id,
                order_id="owned-exit",
                order_link_id="",
                parent_order_link_id=parents[0].order_link_id,
                role="EXIT",
                quantity=D("0.1"),
            )
        )
        now = int(datetime.now(UTC).timestamp() * 1000) - 1000
        entry_side = "Buy" if side is Side.LONG else "Sell"
        first = _row(
            "first-fill", parents[0].order_id, entry_side, ".5", "0", "100", now
        )
        second = _row(
            "second-fill", parents[1].order_id, entry_side, ".7", "0", "101", now + 1
        )
        closing = _row("exit-fill", "owned-exit", exit_side, ".1", ".1", "102", now + 2)
        foreign = _row("unbound-fill", "foreign-order", entry_side, "9", "0", "99", now)
        exchange = Exchange(side)
        exchange.size = D("1.1")
        exchange.stops = stops
        exchange.pages = ([second, foreign, first], [second, closing])
        with executor_for(exchange) as executor:
            await _supervisor(store, executor).reconcile_once()
            assert {r.execution_id for r in await _recorded_rows(store, group)} == {
                "first-fill",
                "second-fill",
            }
            for _ in range(3):
                _make_replay_due(store)
                store = IntentStore(database)
                await _supervisor(store, executor).reconcile_once()
            account = await executor.account_state()
        rows = {row.execution_id: row for row in await _recorded_rows(store, group)}
        assert set(rows) == {"first-fill", "second-fill", "exit-fill"}
        assert rows["exit-fill"].closed_quantity == D("0.1")
        assert rows["second-fill"].price == D("101")
        assert rows["first-fill"].executed_at_ms == now
        assert sum(row.fee for row in rows.values()) == D("0.03")
        snapshot = await store.zone_group_snapshot(group, account)
        quantities = {z["zone_id"]: z["remaining_quantity"] for z in snapshot["zones"]}
        assert quantities == {
            str(plans[0].intent_id): D("0.4"),
            str(plans[1].intent_id): D("0.7"),
        }
        assert snapshot["total_risk_budget_usdt"] == D("20")
        assert all(z["risk_budget_usdt"] == D("10") for z in snapshot["zones"])
        assert (
            snapshot["quantity_reconciled"]
            and snapshot["native_stop_coverage_verified"]
        )
        assert not snapshot["native_stop_issues"]
        assert not snapshot["protection_verified"] and not snapshot["trading_enabled"]
        missing = await store.zone_group_snapshot(
            group, replace(account, open_orders=account.open_orders[:1])
        )
        assert not missing["native_stop_coverage_verified"]
        assert "NATIVE_STOP_UNDERCOVERED" in missing["native_stop_issues"]
        changed = replace(account.open_orders[0], trigger_price=D("1"))
        drifted = await store.zone_group_snapshot(
            group, replace(account, open_orders=(changed, *account.open_orders[1:]))
        )
        assert not drifted["native_stop_coverage_verified"]
        assert not drifted["trading_enabled"]
        assert not exchange.orders

    asyncio.run(run())
