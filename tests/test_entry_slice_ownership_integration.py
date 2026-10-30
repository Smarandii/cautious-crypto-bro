"""Physical child identities retain logical risk and durable ownership boundaries."""

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from test_remaining_concerns import Exchange, executor_for, prepare, state_for

from cautious_crypto_bro.domain import (
    EntryPreflightError,
    ExecutionPlan,
    StrategyStatus,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.portfolio_risk import open_stop_risk_usdt
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import zone_group_id

pytestmark = pytest.mark.integration


def mapped(plan):
    slices = [
        {"leg_name": order.name, "index": index, "quantity": order.quantity / 2}
        for order in plan.orders
        for index in (1, 2)
    ]
    return ExecutionPlan.model_validate(plan.model_dump() | {"entry_slices": slices})


class SliceExchange(Exchange):
    def handle(self, request):
        response = super().handle(request)
        if request.url.path == "/v5/order/realtime" and not any(
            k in request.url.params for k in ("orderId", "orderLinkId")
        ):
            body = response.json()
            body["result"]["list"].extend(
                row
                for row in self.orders.values()
                if row["orderStatus"] == "Untriggered"
            )
            return httpx.Response(200, json=body)
        return response


async def provision(database, exchange, executor):
    store, intent, original, coordinator, _ = await prepare(
        database, exchange, executor
    )
    plan = mapped(original)
    await store.update_execution_plan(plan)
    await store.ensure_position_strategy(plan)
    ids = []
    for order in plan.entry_orders:
        result = exchange.create(
            {
                "symbol": plan.symbol,
                "side": "Buy",
                "orderType": order.order_type.value.title(),
                "qty": str(order.quantity),
                "price": str(order.price or order.reference_price),
                "orderLinkId": f"ccb-v2-{plan.intent_id.hex[:20]}-{order.name.lower()}",
                "stopLoss": str(plan.stop_loss),
            }
        )
        ids.append(result["result"]["orderId"])
    await store.mark_executed(intent.intent_id, tuple(ids))
    await coordinator._enroll_executed_zone(plan, tuple(ids))
    return store, plan


def test_legacy_json_and_mapped_ownership_round_trip_without_new_risk(tmp_path):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = SliceExchange()
        with executor_for(exchange) as executor:
            store, intent, legacy, _, _ = await prepare(database, exchange, executor)
            assert "entry_slices" not in json.loads(legacy.model_dump_json())
            assert legacy.entry_orders == legacy.orders
            plan = mapped(legacy)
            await store.update_execution_plan(plan)
            restored = await IntentStore(database).get_execution_plan(intent.intent_id)
            assert restored == plan and len(restored.orders) == 3
            assert len(restored.entry_orders) == 6
            assert restored.model_dump(exclude={"entry_slices"}) == legacy.model_dump()
            assert sum(o.risk_pct for o in restored.orders) == 100
            assert all(o.risk_pct is None for o in restored.entry_orders)
            assert all(
                len(f"ccb-v2-{plan.intent_id.hex[:20]}-{o.name.lower()}") <= 36
                for o in restored.entry_orders
            )
            with pytest.raises(EntryPreflightError, match="compatibility checkpoint"):
                await executor.prepare_entry_plan(restored)
            assert not exchange.requests and not exchange.orders

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    [
        "missing_leg",
        "gap",
        "duplicate",
        "interleaved",
        "quantity",
        "nan",
        "bool_index",
        "unknown",
        "overfill",
        "mixed_fill",
        "limit_fill",
        "wrong_filled_total",
    ],
)
def test_corrupt_persisted_slice_mapping_cannot_be_loaded(tmp_path, fault):
    async def run():
        database = tmp_path / "state.sqlite3"
        with executor_for(Exchange()) as executor:
            _, intent, legacy, _, _ = await prepare(database, Exchange(), executor)
            payload = mapped(legacy).model_dump(mode="json")
            children = payload["entry_slices"]
            if fault == "missing_leg":
                payload["entry_slices"] = children[:4]
            elif fault == "gap":
                children[1]["index"] = 3
            elif fault == "duplicate":
                children[1]["index"] = 1
            elif fault == "interleaved":
                children[1], children[2] = children[2], children[1]
            elif fault == "quantity":
                children[1]["quantity"] = "0.001"
            elif fault == "nan":
                children[0]["quantity"] = "NaN"
            elif fault == "bool_index":
                children[0]["index"] = True
            elif fault == "unknown":
                children[0]["other_budget"] = "1000"
            elif fault == "overfill":
                children[0]["filled_quantity"] = str(
                    Decimal(children[0]["quantity"]) + 1
                )
            elif fault == "mixed_fill":
                children[0]["filled_quantity"] = children[0]["quantity"]
            elif fault == "limit_fill":
                children[2]["filled_quantity"] = "0"
            else:
                children[0]["filled_quantity"] = "0"
                children[1]["filled_quantity"] = "0"
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "UPDATE execution_plans SET payload_json=?", (json.dumps(payload),)
                )
                db.commit()
            with pytest.raises(ValidationError):
                await IntentStore(database).get_execution_plan(intent.intent_id)

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["none", "quantity", "unknown", "partial_stop", "handoff", "expiry"]
)
def test_restarted_supervisor_owns_every_child_and_keeps_drift_fences(tmp_path, change):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = SliceExchange()
        with executor_for(exchange) as executor:
            store, plan = await provision(database, exchange, executor)
            prefix = f"ccb-v2-{plan.intent_id.hex[:20]}-"
            child = exchange.orders[prefix + "e2c2"]
            if change == "quantity":
                child["qty"] = str(Decimal(child["qty"]) + 1)
            elif change == "unknown":
                child["orderLinkId"] = prefix + "e2c9"
            elif change in {"partial_stop", "handoff"}:
                exchange.orders["attached"] = {
                    "orderId": "attached",
                    "orderLinkId": "",
                    "parentOrderLinkId": prefix + "e1c2",
                    "symbol": plan.symbol,
                    "side": "Sell",
                    "orderType": "Market",
                    "orderStatus": "Untriggered",
                    "qty": "1",
                    "leavesQty": "1",
                    "cumExecQty": "0",
                    "price": "0",
                    "avgPrice": "0",
                    "reduceOnly": True,
                    "stopOrderType": "PartialStopLoss",
                    "triggerPrice": str(
                        plan.stop_loss + (1 if change == "partial_stop" else 0)
                    ),
                }
            elif change == "expiry":
                policy = plan.policy.model_copy(
                    update={
                        "strategy_v2": plan.policy.strategy_v2.model_copy(
                            update={"entry_order_ttl_minutes": 1}
                        )
                    }
                )
                plan = plan.model_copy(
                    update={
                        "created_at": datetime.now(UTC) - timedelta(days=1),
                        "policy": policy,
                    }
                )
                await store.update_execution_plan(plan)
            before = len(exchange.requests)
            restarted_store = IntentStore(database)
            supervisor = PositionSupervisor(
                store=restarted_store, executor=executor, mutation_lock=asyncio.Lock()
            )
            await supervisor.reconcile_once()
            if change in {"quantity", "unknown", "partial_stop"}:
                alerts = await restarted_store.get_pending_strategy_alerts()
                assert len(alerts) == 1
                state = alerts[0]
                assert state.status is StrategyStatus.MANUAL_OVERRIDE
                assert not any(
                    path
                    in {
                        "/v5/order/create",
                        "/v5/order/create-batch",
                        "/v5/order/cancel",
                        "/v5/position/trading-stop",
                    }
                    for path, _ in exchange.requests[before:]
                )
            else:
                state = await state_for(restarted_store)
                assert state.status is not StrategyStatus.MANUAL_OVERRIDE
                assert exchange.stop == plan.stop_loss
                if change == "expiry":
                    assert not exchange.open_entries()
                else:
                    assert len(exchange.open_entries()) == 4
                if change == "handoff":
                    assert exchange.orders["attached"]["orderStatus"] == "Cancelled"
            bindings = await restarted_store.zone_bound_orders(
                zone_group_id(plan.symbol, plan.side)
            )
            entries = [b for b in bindings if b.role == "ENTRY"]
            assert len(entries) == 6
            assert {b.order_link_id: b.quantity for b in entries} == {
                prefix + o.name.lower(): o.quantity for o in plan.entry_orders
            }

    asyncio.run(run())


def test_child_market_price_fallback_uses_frozen_logical_reference(tmp_path):
    async def run():
        exchange = SliceExchange()
        with executor_for(exchange) as executor:
            store, plan = await provision(
                tmp_path / "state.sqlite3", exchange, executor
            )
            account = await executor.account_state()
            without_prices = replace(
                account,
                open_orders=tuple(replace(o, price=None) for o in account.open_orders),
            )
            active = await store.get_active_position_strategies()
            assert open_stop_risk_usdt(account, active) == open_stop_risk_usdt(
                without_prices, active
            )

    asyncio.run(run())


def test_confirmed_primary_child_fills_retain_original_request_ownership(tmp_path):
    async def run():
        database = tmp_path / "state.sqlite3"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, legacy, _, _ = await prepare(database, exchange, executor)
            primary_quantity = legacy.orders[0].quantity
            rebased = ExecutionPlanner().rebase_market_plan(
                legacy,
                fill_price=Decimal("100"),
                filled_quantity=primary_quantity / 2,
                context=await executor.market_context(legacy.symbol),
            )
            children = mapped(rebased).model_dump(mode="json")["entry_slices"]
            for index, filled in enumerate((primary_quantity / 2, Decimal("0"))):
                children[index]["quantity"] = str(primary_quantity / 2)
                children[index]["filled_quantity"] = str(filled)
            confirmed = ExecutionPlan.model_validate(
                rebased.model_dump() | {"entry_slices": children}
            )
            await store.update_execution_plan(confirmed)
            restored = await IntentStore(database).get_execution_plan(intent.intent_id)
            assert restored == confirmed
            assert (
                sum(o.quantity for o in restored.entry_orders[:2]) == primary_quantity
            )
            assert restored.orders[0].quantity == primary_quantity / 2
            assert restored.entry_slices[1].filled_quantity == 0
            assert restored.stop_loss == legacy.stop_loss
            assert restored.planned_max_loss_usdt <= legacy.policy.risk_budget_usdt

    asyncio.run(run())
