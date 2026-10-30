"""Exchange order ceilings must not discard an otherwise valid logical strategy."""

import asyncio
import json
import sqlite3
from contextlib import closing
from decimal import Decimal

import httpx
import pytest
from test_flat_tier_preflight_integration import FlatTierExchange
from test_tier_preflight_integration import run_entry

from cautious_crypto_bro.domain import (
    ApprovalMode,
    EntryType,
    IntentStatus,
    StrategyStatus,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import zone_group_id

pytestmark = pytest.mark.integration


class QuantityLimitedExchange(FlatTierExchange):
    max_market_qty = Decimal("0.25")
    max_limit_qty = Decimal("0.25")

    def __init__(self, database):
        super().__init__(database)
        self.accepted_entries = []
        self.quantity_rejections = 0
        self.failure = ""
        self.failed_once = False
        self.fill_mode = "full"
        self.minimum_notional = "5"

    def fill(self, link, quantity=None, price=None):
        order = self.orders[link]
        primary = not order["reduceOnly"] and order["orderType"] == "Market"
        if primary and self.fill_mode != "full":
            if self.fill_mode == "none" or (
                self.fill_mode == "one_empty" and link.endswith("e1c1")
            ):
                order["orderStatus"] = "Cancelled"
                return
            if self.fill_mode == "partial":
                super().fill(link, quantity=Decimal(order["qty"]) / 2, price=price)
                order["orderStatus"] = "PartiallyFilledCanceled"
                return
        super().fill(link, quantity=quantity, price=price)

    def create(self, body):
        if not body.get("reduceOnly", False):
            if self.failure == "batch_item" and body["orderLinkId"].endswith("e2c2"):
                return {
                    "retCode": 10001,
                    "retMsg": "Synthetic child rejection",
                    "result": {},
                }
            with closing(sqlite3.connect(self.database)) as db:
                payload = json.loads(
                    db.execute("SELECT payload_json FROM execution_plans").fetchone()[0]
                )
                slices = payload.get("entry_slices", ())
                if slices:
                    planned = {
                        f"{s['leg_name'].lower()}{'c' + str(s['index']) if s['index'] else ''}": Decimal(
                            s["quantity"]
                        )
                        for s in slices
                    }
                    assert (
                        Decimal(body["qty"])
                        == planned[body["orderLinkId"].split("-")[-1]]
                    )
                    assert (
                        db.execute("SELECT count(*) FROM zone_allocations").fetchone()[
                            0
                        ]
                        == 1
                    )
            ceiling = (
                self.max_market_qty
                if body["orderType"] == "Market"
                else self.max_limit_qty
            )
            if Decimal(body["qty"]) > ceiling:
                self.quantity_rejections += 1
                return {
                    "retCode": 10001,
                    "retMsg": "Synthetic per-order quantity ceiling",
                    "result": {},
                }
            self.accepted_entries.append(dict(body))
        return super().create(body)

    def handle(self, request):
        if (
            request.url.path in {"/v5/order/create", "/v5/order/create-batch"}
            and not self.failed_once
        ):
            target = (
                "primary_ack" if request.url.path == "/v5/order/create" else "batch_ack"
            )
            if self.failure == target:
                self.failed_once = True
                super().handle(request)
                raise httpx.ReadTimeout(
                    "Synthetic lost acknowledgement", request=request
                )
        if request.url.path == "/v5/order/cancel-batch":
            payload = json.loads(request.content)
            self.requests.append((request.url.path, payload))
            results, statuses = [], []
            for item in payload["request"]:
                order = self.orders.get(item["orderLinkId"])
                if order is not None and order["orderStatus"] in {
                    "New",
                    "PartiallyFilled",
                }:
                    order["orderStatus"] = "Cancelled"
                    results.append(
                        {
                            "orderId": order["orderId"],
                            "orderLinkId": item["orderLinkId"],
                        }
                    )
                    statuses.append({"code": 0, "msg": "OK"})
                else:
                    results.append({"orderId": "", "orderLinkId": item["orderLinkId"]})
                    statuses.append({"code": 110001, "msg": "Order is not cancellable"})
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"list": results},
                    "retExtInfo": {"list": statuses},
                },
            )
        if request.url.path == "/v5/order/create-batch":
            assert len(json.loads(request.content)["request"]) <= 20
        response = super().handle(request)
        if request.url.path != "/v5/market/instruments-info":
            return response
        body = response.json()
        lot = body["result"]["list"][0]["lotSizeFilter"]
        lot.update(
            maxMktOrderQty=str(self.max_market_qty), maxOrderQty=str(self.max_limit_qty)
        )
        lot["minNotionalValue"] = self.minimum_notional
        return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    "entry_type", [EntryType.MARKET, EntryType.LIMIT, EntryType.RANGE]
)
@pytest.mark.parametrize("ceiling", ["0.25", "0.36"])
def test_oversize_entry_preserves_reference_strategy_and_all_physical_ownership(
    tmp_path, entry_type, ceiling
):
    async def run():
        reference_db = tmp_path / "reference.sqlite3"
        reference = QuantityLimitedExchange(reference_db)
        reference.max_market_qty = reference.max_limit_qty = Decimal("1000000")
        _, _, _, expected = await run_entry(reference_db, reference, entry_type)
        assert expected.status is IntentStatus.EXECUTED

        database = tmp_path / "limited.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.max_market_qty = exchange.max_limit_qty = Decimal(ceiling)
        store, intent, _, outcome = await run_entry(database, exchange, entry_type)
        assert outcome.status is IntentStatus.EXECUTED
        assert exchange.quantity_rejections == 0
        assert len(exchange.accepted_entries) > 3
        saved = await store.get_execution_plan(intent.intent_id)
        assert saved is not None and saved.entry_slices and len(saved.orders) == 3
        assert saved.model_dump(
            exclude={"intent_id", "created_at", "entry_slices"}
        ) == expected.plan.model_dump(
            exclude={"intent_id", "created_at", "entry_slices"}
        )
        assert len(outcome.order_ids) == len(saved.entry_orders)
        assert len(set(outcome.order_ids)) == len(outcome.order_ids)
        expected_links = {
            f"ccb-v2-{intent.intent_id.hex[:20]}-{o.name.lower()}": o.quantity
            for o in saved.entry_orders
        }
        assert {
            r["orderLinkId"]: Decimal(r["qty"]) for r in exchange.accepted_entries
        } == expected_links
        bindings = await store.zone_bound_orders(
            zone_group_id(saved.symbol, saved.side)
        )
        assert {
            b.order_link_id: b.quantity for b in bindings if b.role == "ENTRY"
        } == expected_links
        if entry_type is EntryType.MARKET:
            assert exchange.stop == saved.stop_loss

    asyncio.run(run())


@pytest.mark.parametrize("fill_mode", ["partial", "one_empty", "none"])
def test_terminal_primary_children_preserve_submitted_and_actual_quantities(
    tmp_path, fill_mode
):
    async def run():
        database = tmp_path / "partial.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.fill_mode = fill_mode
        store, intent, _, outcome = await run_entry(
            database, exchange, EntryType.MARKET
        )
        saved = await store.get_execution_plan(intent.intent_id)
        assert saved is not None
        if fill_mode == "none":
            assert outcome.status is IntentStatus.FAILED
            assert not any(o["orderType"] == "Limit" for o in exchange.accepted_entries)
            return
        assert outcome.status is IntentStatus.EXECUTED
        primary = [s for s in saved.entry_slices if s.leg_name == "E1"]
        assert len(primary) == 2
        assert (
            sum(s.filled_quantity for s in primary)
            == saved.orders[0].quantity
            == Decimal("0.2")
        )
        assert sum(s.quantity for s in primary) == Decimal("0.4")
        assert all(s.filled_quantity is not None for s in primary)
        saved.validate_risk_budget()
        assert await IntentStore(database).get_execution_plan(intent.intent_id) == saved
        bound = await store.zone_bound_orders(zone_group_id(saved.symbol, saved.side))
        assert len([b for b in bound if b.role == "ENTRY"]) == len(saved.entry_orders)

    asyncio.run(run())


@pytest.mark.parametrize("ceiling", ["NaN", "0", "-1", "Infinity", "0.0001"])
def test_invalid_or_unrepresentable_limits_reject_before_mutation(tmp_path, ceiling):
    async def run():
        database = tmp_path / "invalid.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.max_limit_qty = Decimal(ceiling)
        _, _, _, outcome = await run_entry(database, exchange, EntryType.LIMIT)
        assert outcome.status is IntentStatus.FAILED
        assert not exchange.accepted_entries
        assert not any(
            path
            in {
                "/v5/order/create",
                "/v5/order/create-batch",
                "/v5/position/set-leverage",
            }
            for path, _ in exchange.requests
        )

    asyncio.run(run())


def test_unprotectable_child_count_rejects_before_exchange_mutation(tmp_path):
    async def run():
        database = tmp_path / "batches.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.max_limit_qty = Decimal("0.04")
        exchange.minimum_notional = "0.01"
        store, intent, _, outcome = await run_entry(database, exchange, EntryType.LIMIT)
        assert outcome.status is IntentStatus.FAILED
        assert "partial-stop capacity" in outcome.message
        assert not exchange.accepted_entries

    asyncio.run(run())


def test_actual_fill_can_require_slicing_only_the_rebased_scale_ins(tmp_path):
    async def run():
        database = tmp_path / "rebased.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.max_market_qty = Decimal("1000000")
        exchange.max_limit_qty = Decimal("0.45")
        exchange.fill_price = Decimal("99")
        store, intent, original, outcome = await run_entry(
            database, exchange, EntryType.MARKET
        )
        assert all(o.quantity <= Decimal("0.45") for o in original.orders)
        assert outcome.status is IntentStatus.EXECUTED
        assert exchange.quantity_rejections == 0
        saved = await store.get_execution_plan(intent.intent_id)
        assert saved.entry_slices and len(saved.entry_orders) == 4
        primary = next(s for s in saved.entry_slices if s.leg_name == "E1")
        assert (
            primary.index == 0
            and primary.quantity == primary.filled_quantity == Decimal("0.4")
        )
        assert (
            len(await store.zone_bound_orders(zone_group_id(saved.symbol, saved.side)))
            == 4
        )

    asyncio.run(run())


def test_successful_entries_recover_a_missing_ledger_write_without_resubmission(
    tmp_path,
):
    async def run():
        database = tmp_path / "bindings.sqlite3"
        exchange = QuantityLimitedExchange(database)
        store, _, _, outcome = await run_entry(database, exchange, EntryType.LIMIT)
        assert outcome.status is IntentStatus.EXECUTED
        with closing(sqlite3.connect(database)) as db:
            db.execute("DELETE FROM zone_orders")
            db.commit()
        from test_remaining_concerns import executor_for

        with executor_for(exchange) as executor:
            before = len(exchange.accepted_entries)
            await PositionSupervisor(
                store=store, executor=executor, mutation_lock=asyncio.Lock()
            ).reconcile_once()
            bound = await store.zone_bound_orders(
                zone_group_id(outcome.plan.symbol, outcome.plan.side)
            )
            assert {b.order_link_id for b in bound} == {
                r["orderLinkId"] for r in exchange.accepted_entries
            }
            assert len(exchange.accepted_entries) == before

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["primary_ack", "batch_ack", "batch_item"])
def test_lost_acknowledgement_retains_ownership_after_restart_without_replay(
    tmp_path, failure
):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = QuantityLimitedExchange(database)
        exchange.failure = failure
        store, intent, _, outcome = await run_entry(
            database, exchange, EntryType.MARKET
        )
        assert outcome.status is IntentStatus.FAILED
        assert (
            exchange.failed_once or failure == "batch_item"
        ) and exchange.accepted_entries
        records = await store.get_active_position_strategies()
        assert len(records) == 1 and records[0][0].status is StrategyStatus.UNCERTAIN
        plan = records[0][1]
        links = {r["orderLinkId"] for r in exchange.accepted_entries}
        bound = await store.zone_bound_orders(zone_group_id(plan.symbol, plan.side))
        assert {b.order_link_id for b in bound} == links
        # A crash may lose the acknowledgement-to-ledger write. Recovery only reads.
        with closing(sqlite3.connect(database)) as db:
            db.execute("DELETE FROM zone_orders")
            db.commit()
        from test_remaining_concerns import executor_for

        with executor_for(exchange) as executor:
            restarted = IntentStore(database)
            supervisor = PositionSupervisor(
                store=restarted, executor=executor, mutation_lock=asyncio.Lock()
            )
            before = len(exchange.accepted_entries)
            await supervisor.reconcile_once()
            bound = await restarted.zone_bound_orders(
                zone_group_id(plan.symbol, plan.side)
            )
            assert {b.order_link_id for b in bound} == links
            coordinator = ExecutionCoordinator(
                store=restarted,
                executor=executor,
                planner=ExecutionPlanner(),
                max_age_seconds=3600,
            )
            again = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.AUTO
            )
            assert again.status is IntentStatus.FAILED
            assert len(exchange.accepted_entries) == before

    asyncio.run(run())
