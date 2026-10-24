"""Owned-exit supervision through real SQLite, HTTP adapter, and alert delivery."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, state_for

from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    StrategyStatus,
    TradeExecutionError,
)
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import (
    LATEST_SCHEMA_SQL,
    LATEST_SCHEMA_VERSION,
    IntentStore,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "change",
    [
        "increase",
        "average",
        "reduction",
        "partial-fill",
        "partial-fill-extra-reduction",
        "partial-fill-between-reads",
    ],
)
def test_interrupted_exit_install_requires_fills_to_explain_position_change(
    tmp_path, change
):
    async def run():
        database = tmp_path / "state.db"

        class FillingExchange(Exchange):
            reads_until_fill = 0

            def handle(self, request):
                if (
                    self.reads_until_fill
                    and request.url.path == "/v5/order/realtime"
                    and "orderLinkId" not in request.url.params
                ):
                    self.reads_until_fill -= 1
                    if not self.reads_until_fill:
                        self.fill(first, quantity=saved.expected_exits[first][0] / 4)
                return super().handle(request)

        exchange = FillingExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            place = executor.place_reduce_only_exit

            async def interrupted(**kwargs):
                await place(**kwargs)
                raise RuntimeError("Synthetic interrupted installation")

            executor.place_reduce_only_exit = interrupted
            with pytest.raises(RuntimeError, match="Synthetic interrupted"):
                await supervisor.reconcile_once()
            saved = await state_for(store)
            assert saved.installing_exits and len(saved.expected_exits) == 3
            first = next(iter(saved.expected_exits))
            if change == "increase":
                exchange.size += Decimal("0.1")
            elif change == "average":
                exchange.avg += Decimal("1")
            elif change == "reduction":
                exchange.size -= Decimal("0.1")
            else:
                exchange.fill(first, quantity=saved.expected_exits[first][0] / 2)
                if change == "partial-fill-extra-reduction":
                    exchange.size -= Decimal("0.1")
                if change == "partial-fill-between-reads":
                    exchange.reads_until_fill = 2
            executor.place_reduce_only_exit = place
            before = len(exchange.requests)
            restarted = PositionSupervisor(
                store=IntentStore(database),
                executor=executor,
                mutation_lock=asyncio.Lock(),
            )
            await restarted.reconcile_once()
            reopened = IntentStore(database)
            if change in {"partial-fill", "partial-fill-between-reads"}:
                resumed = await state_for(reopened)
                assert not resumed.installing_exits
                assert resumed.expected_exits == saved.expected_exits
                assert (
                    len([o for o in exchange.orders.values() if o["reduceOnly"]]) == 3
                )
            else:
                alerts = await reopened.get_pending_strategy_alerts()
                assert len(alerts) == 1
                assert alerts[0].status is StrategyStatus.MANUAL_OVERRIDE
                assert "position changed" in alerts[0].pause_reason
                assert not any(
                    path
                    in {
                        "/v5/order/create",
                        "/v5/order/cancel",
                        "/v5/position/trading-stop",
                    }
                    for path, _ in exchange.requests[before:]
                )
            assert exchange.stop == plan.stop_loss

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["missing", "null", "blank", "negative", "zero"])
def test_invalid_filled_exit_quantity_blocks_progress_until_repaired(tmp_path, fault):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await supervisor.reconcile_once()
            state = await state_for(store)
            link = next(iter(state.expected_exits))
            exchange.fill(link)
            filled = exchange.orders[link]
            valid_quantity = filled["cumExecQty"]
            if fault == "missing":
                del filled["cumExecQty"]
            else:
                filled["cumExecQty"] = {
                    "null": None,
                    "blank": "",
                    "negative": "-1",
                    "zero": "0",
                }[fault]
            before = len(exchange.requests)
            with pytest.raises(TradeExecutionError, match="cumExecQty"):
                await supervisor.reconcile_once()
            saved = await state_for(IntentStore(database))
            assert not saved.tp1_done and not saved.entry_frozen
            assert not any(
                path
                in {"/v5/order/create", "/v5/order/cancel", "/v5/position/trading-stop"}
                for path, _ in exchange.requests[before:]
            )
            filled["cumExecQty"] = valid_quantity
            restarted = PositionSupervisor(
                store=IntentStore(database),
                executor=executor,
                mutation_lock=asyncio.Lock(),
            )
            await restarted.reconcile_once()
            assert (await state_for(IntentStore(database))).tp1_done

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["during-cancel", "after-restart"])
def test_exit_fill_during_replacement_pauses_before_new_exit_submission(
    tmp_path, phase
):
    class FillDuringCancellation(Exchange):
        fill_on_cancel = False
        delay_exits = False

        def handle(self, request):
            if self.fill_on_cancel and request.url.path == "/v5/order/cancel":
                body = json.loads(request.content)
                order = next(
                    o
                    for o in self.orders.values()
                    if o["orderId"] == body.get("orderId")
                )
                if order["reduceOnly"]:
                    if self.delay_exits:
                        self.requests.append((request.url.path, body))
                        return httpx.Response(
                            200,
                            json={
                                "retCode": 0,
                                "result": {"orderId": order["orderId"]},
                            },
                        )
                    self.fill_on_cancel = False
                    self.fill(order["orderLinkId"])
                    self.requests.append((request.url.path, body))
                    return httpx.Response(
                        200,
                        json={"retCode": 0, "result": {"orderId": order["orderId"]}},
                    )
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = FillDuringCancellation()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await supervisor.reconcile_once()
            state = await state_for(store)
            await executor.cancel_pending_entries(state.symbol)
            await store.save_position_strategy(
                state.model_copy(
                    update={
                        "entry_frozen": True,
                        "base_position_qty": exchange.size,
                        "rebalance_needed": True,
                    }
                )
            )
            original_size = exchange.size
            exchange.fill_on_cancel = True
            before = len(exchange.requests)
            if phase == "after-restart":
                exchange.delay_exits = True
                with pytest.raises(
                    TradeExecutionError, match="cancellation.*confirmed"
                ):
                    await supervisor.reconcile_once()
                old_exits = [
                    o
                    for o in exchange.orders.values()
                    if o["reduceOnly"] and o["orderStatus"] == "New"
                ]
                exchange.fill(old_exits[0]["orderLinkId"])
                for order in old_exits[1:]:
                    order["orderStatus"] = "Cancelled"
                exchange.delay_exits = exchange.fill_on_cancel = False
                supervisor = PositionSupervisor(
                    store=IntentStore(database),
                    executor=executor,
                    mutation_lock=asyncio.Lock(),
                )
            await supervisor.reconcile_once()
            reopened = IntentStore(database)
            alerts = await reopened.get_pending_strategy_alerts()
            assert (
                len(alerts) == 1 and alerts[0].status is StrategyStatus.MANUAL_OVERRIDE
            )
            assert "position changed" in alerts[0].pause_reason
            assert 0 < exchange.size < original_size
            assert exchange.stop == plan.stop_loss
            assert not any(
                path == "/v5/order/create" for path, _ in exchange.requests[before:]
            )
            restarted = PositionSupervisor(
                store=reopened, executor=executor, mutation_lock=asyncio.Lock()
            )
            before = len(exchange.requests)
            await restarted.reconcile_once()
            assert not any(
                path
                in {"/v5/order/create", "/v5/order/cancel", "/v5/position/trading-stop"}
                for path, _ in exchange.requests[before:]
            )

    asyncio.run(run())


def test_rebalance_waits_for_old_exit_cancellation_before_replacement(tmp_path):
    class DelayedExitCancellation(Exchange):
        delay_exits = False

        def handle(self, request):
            if self.delay_exits and request.url.path == "/v5/order/cancel":
                body = json.loads(request.content)
                order = next(
                    o
                    for o in self.orders.values()
                    if o["orderId"] == body.get("orderId")
                    or o["orderLinkId"] == body.get("orderLinkId")
                )
                if order["reduceOnly"]:
                    self.requests.append((request.url.path, body))
                    return httpx.Response(
                        200,
                        json={"retCode": 0, "result": {"orderId": order["orderId"]}},
                    )
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = DelayedExitCancellation()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await supervisor.reconcile_once()
            state = await state_for(store)
            old_links = set(state.expected_exits)
            assert len(old_links) == 3
            await executor.cancel_pending_entries(state.symbol)
            await store.save_position_strategy(
                state.model_copy(
                    update={
                        "entry_frozen": True,
                        "base_position_qty": exchange.size,
                        "rebalance_needed": True,
                    }
                )
            )
            exchange.delay_exits = True
            before = len(exchange.requests)
            with pytest.raises(
                TradeExecutionError, match="exit.*cancellation.*confirmed"
            ):
                await supervisor.reconcile_once()
            reopened = IntentStore(database)
            state = await state_for(reopened)
            assert state.installing_exits
            assert exchange.stop == plan.stop_loss
            assert all(
                exchange.orders[link]["orderStatus"] == "New" for link in old_links
            )
            assert not any(
                path == "/v5/order/create" for path, _ in exchange.requests[before:]
            )
            exchange.delay_exits = False
            restarted = PositionSupervisor(
                store=reopened, executor=executor, mutation_lock=asyncio.Lock()
            )
            await restarted.reconcile_once()
            state = await state_for(IntentStore(database))
            assert not state.installing_exits and len(state.expected_exits) == 3
            assert set(state.expected_exits).isdisjoint(old_links)
            assert all(
                exchange.orders[link]["orderStatus"] == "Cancelled"
                for link in old_links
            )

    asyncio.run(run())


def test_expiry_freeze_includes_fill_during_cancellation_confirmation(tmp_path):
    class LateFillExchange(Exchange):
        confirmation_reads = None

        def handle(self, request):
            if request.url.path == "/v5/order/cancel":
                body = json.loads(request.content)
                self.requests.append((request.url.path, body))
                if self.confirmation_reads is None:
                    self.confirmation_reads = 0
                return httpx.Response(
                    200, json={"retCode": 0, "result": {"orderId": body["orderId"]}}
                )
            if (
                request.url.path == "/v5/order/realtime"
                and self.confirmation_reads is not None
            ):
                self.confirmation_reads += 1
                if self.confirmation_reads == 2:
                    pending = [
                        o
                        for o in self.orders.values()
                        if o["orderStatus"] == "New" and not o["reduceOnly"]
                    ]
                    self.fill(pending[0]["orderLinkId"])
                    for order in pending[1:]:
                        order["orderStatus"] = "Cancelled"
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = LateFillExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            initial_size = exchange.size
            await store.update_execution_plan(
                plan.model_copy(
                    update={
                        "created_at": datetime.now(UTC) - timedelta(days=1),
                        "policy": plan.policy.model_copy(
                            update={
                                "strategy_v2": plan.policy.strategy_v2.model_copy(
                                    update={"entry_order_ttl_minutes": 1}
                                )
                            }
                        ),
                    }
                )
            )
            await supervisor.reconcile_once()
            state = await state_for(IntentStore(database))
            assert exchange.size > initial_size
            assert state.entry_frozen and state.expected_exits
            assert state.base_position_qty == state.last_position_qty == exchange.size
            assert state.last_avg_price == exchange.avg
            assert exchange.stop >= plan.stop_loss

    asyncio.run(run())


def test_expiry_waits_for_entry_cancellation_before_freezing_quantity(tmp_path):
    class DelayedCancelExchange(Exchange):
        delay_cancellation = True

        def handle(self, request):
            if self.delay_cancellation and request.url.path == "/v5/order/cancel":
                body = json.loads(request.content)
                self.requests.append((request.url.path, body))
                return httpx.Response(
                    200, json={"retCode": 0, "result": {"orderId": body["orderId"]}}
                )
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = DelayedCancelExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await store.update_execution_plan(
                plan.model_copy(
                    update={
                        "created_at": datetime.now(UTC) - timedelta(days=1),
                        "policy": plan.policy.model_copy(
                            update={
                                "strategy_v2": plan.policy.strategy_v2.model_copy(
                                    update={"entry_order_ttl_minutes": 1}
                                )
                            }
                        ),
                    }
                )
            )
            before = len(exchange.requests)
            with pytest.raises(TradeExecutionError, match="cancellation.*confirmed"):
                await supervisor.reconcile_once()
            state = await state_for(IntentStore(database))
            assert not state.entry_frozen and state.exit_revision == 0
            assert exchange.stop == plan.stop_loss and exchange.size > 0
            assert any(
                path == "/v5/order/cancel" for path, _ in exchange.requests[before:]
            )
            assert not any(
                path in {"/v5/order/create", "/v5/position/trading-stop"}
                for path, _ in exchange.requests[before:]
            )
            exchange.delay_cancellation = False
            restarted = PositionSupervisor(
                store=IntentStore(database),
                executor=executor,
                mutation_lock=asyncio.Lock(),
            )
            await restarted.reconcile_once()
            state = await state_for(IntentStore(database))
            assert state.entry_frozen and state.expected_exits
            assert not any(
                o["orderStatus"] == "New" and not o["reduceOnly"]
                for o in exchange.orders.values()
            )

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["initial", "managed", "installing"])
@pytest.mark.parametrize("protection", ["trailing", "take profit"])
def test_unexpected_position_protection_pauses_before_other_mutations(
    tmp_path, phase, protection
):
    class ExternalProtectionExchange(Exchange):
        manual_tp = Decimal("0")

        def position(self):
            return super().position() | {"takeProfit": str(self.manual_tp)}

    async def run():
        database = tmp_path / "state.db"
        exchange = ExternalProtectionExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = Decimal("102")
            if phase != "initial":
                await supervisor.reconcile_once()
            if phase == "installing":
                await store.save_position_strategy(
                    (await state_for(store)).model_copy(
                        update={"installing_exits": True}
                    )
                )
            assert not (await state_for(store)).trailing_active
            if protection == "trailing":
                exchange.trail = Decimal("1")
            else:
                exchange.manual_tp = Decimal("110")
            before = len(exchange.requests)
            reopened = IntentStore(database)
            restarted = PositionSupervisor(
                store=reopened, executor=executor, mutation_lock=asyncio.Lock()
            )
            await restarted.reconcile_once()
            alerts = await reopened.get_pending_strategy_alerts()
            assert (
                len(alerts) == 1 and alerts[0].status is StrategyStatus.MANUAL_OVERRIDE
            )
            assert protection in alerts[0].pause_reason
            assert exchange.trail == (1 if protection == "trailing" else 0)
            assert exchange.manual_tp == (110 if protection == "take profit" else 0)
            assert exchange.stop == plan.stop_loss
            assert not any(
                path
                in {
                    "/v5/order/cancel",
                    "/v5/order/create",
                    "/v5/order/create-batch",
                    "/v5/position/trading-stop",
                }
                for path, _ in exchange.requests[before:]
            )
            await restarted.reconcile_once()
            assert len(await IntentStore(database).get_pending_strategy_alerts()) == 1

    asyncio.run(run())


@pytest.mark.parametrize("change", [None, "cancel", "price"])
@pytest.mark.parametrize("installing", [False, True])
def test_fixed_exit_drift_blocks_partial_stop_handoff(tmp_path, change, installing):
    class HandoffExchange(Exchange):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/v5/order/realtime" and not any(
                key in request.url.params for key in ("orderId", "orderLinkId")
            ):
                payload = response.json()
                payload["result"]["list"].extend(
                    order
                    for order in self.orders.values()
                    if order["orderStatus"] == "Untriggered"
                )
                return httpx.Response(200, json=payload)
            return response

    async def run():
        database = tmp_path / "state.db"
        exchange = HandoffExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = Decimal("102")
            await supervisor.reconcile_once()
            state = await state_for(store)
            if installing:
                await store.save_position_strategy(
                    state.model_copy(update={"installing_exits": True})
                )
            target = next(
                order for order in exchange.exits() if "-t1r" in order["orderLinkId"]
            )
            if change == "cancel":
                target["orderStatus"] = "Cancelled"
            elif change == "price":
                target["price"] = str(Decimal(target["price"]) + 1)
            exchange.orders["attached-stop"] = {
                "orderId": "attached-stop",
                "orderLinkId": "",
                "parentOrderLinkId": f"ccb-v2-{state.strategy_id.hex[:20]}-{plan.orders[0].name.lower()}",
                "symbol": state.symbol,
                "side": "Sell",
                "orderType": "Market",
                "orderStatus": "Untriggered",
                "qty": str(exchange.size),
                "leavesQty": str(exchange.size),
                "cumExecQty": "0",
                "price": "0",
                "avgPrice": "0",
                "reduceOnly": True,
                "stopOrderType": "PartialStopLoss",
                "triggerPrice": str(plan.stop_loss),
                "updatedTime": "0",
            }
            before = len(exchange.requests)
            reopened = IntentStore(database)
            restarted = PositionSupervisor(
                store=reopened, executor=executor, mutation_lock=asyncio.Lock()
            )
            try:
                await restarted.reconcile_once()
            finally:
                if change is not None:
                    assert (
                        exchange.orders["attached-stop"]["orderStatus"] == "Untriggered"
                    )
            alerts = await reopened.get_pending_strategy_alerts()
            if change is None:
                assert not alerts
                assert exchange.orders["attached-stop"]["orderStatus"] == "Cancelled"
                assert (
                    await state_for(reopened)
                ).status is not StrategyStatus.MANUAL_OVERRIDE
            else:
                assert (
                    len(alerts) == 1 and target["orderLinkId"] in alerts[0].pause_reason
                )
                assert alerts[0].status is StrategyStatus.MANUAL_OVERRIDE
                assert not any(
                    path
                    in {
                        "/v5/order/cancel",
                        "/v5/order/create",
                        "/v5/order/create-batch",
                        "/v5/position/trading-stop",
                    }
                    for path, _ in exchange.requests[before:]
                )
            assert exchange.stop == plan.stop_loss

    asyncio.run(run())


@pytest.mark.parametrize("change", ["trigger", "side"])
@pytest.mark.parametrize("expired", [False, True])
def test_owned_partial_stop_drift_pauses_before_exchange_mutations(
    tmp_path, change, expired
):
    class AttachedStopExchange(Exchange):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/v5/order/realtime" and not any(
                key in request.url.params for key in ("orderId", "orderLinkId")
            ):
                payload = response.json()
                payload["result"]["list"].extend(
                    order
                    for order in self.orders.values()
                    if order["orderStatus"] == "Untriggered"
                )
                return httpx.Response(200, json=payload)
            return response

    async def run():
        database = tmp_path / "state.db"
        exchange = AttachedStopExchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = Decimal("102")
            await supervisor.reconcile_once()
            state = await state_for(store)
            parent = (
                f"ccb-v2-{state.strategy_id.hex[:20]}-{plan.orders[0].name.lower()}"
            )
            exchange.orders["attached-stop"] = {
                "orderId": "attached-stop",
                "orderLinkId": "",
                "parentOrderLinkId": parent,
                "symbol": state.symbol,
                "side": "Buy" if change == "side" else "Sell",
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
                    plan.stop_loss + 1 if change == "trigger" else plan.stop_loss
                ),
                "updatedTime": "0",
            }
            if expired:
                policy = plan.policy.model_copy(
                    update={
                        "strategy_v2": plan.policy.strategy_v2.model_copy(
                            update={"entry_order_ttl_minutes": 1}
                        )
                    }
                )
                await store.update_execution_plan(
                    plan.model_copy(
                        update={
                            "created_at": datetime.now(UTC) - timedelta(days=1),
                            "policy": policy,
                        }
                    )
                )
            before = len(exchange.requests)
            restarted_store = IntentStore(database)
            restarted = PositionSupervisor(
                store=restarted_store, executor=executor, mutation_lock=asyncio.Lock()
            )
            await restarted.reconcile_once()
            alerts = await restarted_store.get_pending_strategy_alerts()
            assert len(alerts) == 1
            assert alerts[0].status is StrategyStatus.MANUAL_OVERRIDE
            assert "owned partial stop" in alerts[0].pause_reason
            assert "attached-stop" in alerts[0].pause_reason
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
            assert exchange.orders["attached-stop"]["orderStatus"] == "Untriggered"
            assert exchange.stop == plan.stop_loss
            await restarted.reconcile_once()
            assert len(await IntentStore(database).get_pending_strategy_alerts()) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        "cancel",
        "partial_cancel",
        "price",
        "quantity",
        "side",
        "reduce_only",
        "missing",
        "legacy",
    ],
)
@pytest.mark.parametrize("expired", [False, True])
def test_owned_exit_drift_survives_restart_and_alert_delivery_failure(
    tmp_path, change, expired
):
    async def run():
        database = tmp_path / "state.db"
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
            exchange.mark = Decimal("102")
            await supervisor.reconcile_once()
            target = next(
                order for order in exchange.exits() if "-t1r" in order["orderLinkId"]
            )
            link = target["orderLinkId"]
            if change == "legacy":
                await store.save_position_strategy(
                    (await state_for(store)).model_copy(update={"expected_exits": None})
                )
            if change == "partial_cancel":
                exchange.fill(link, Decimal(target["qty"]) / 2)
            if change in {"cancel", "partial_cancel"}:
                target["orderStatus"] = "Cancelled"
            elif change == "price":
                target["price"] = str(Decimal(target["price"]) + 1)
            elif change == "quantity":
                target["qty"] = str(Decimal(target["qty"]) + 1)
            elif change == "side":
                target["side"] = "Buy"
            elif change == "reduce_only":
                target["reduceOnly"] = False
            elif change == "missing":
                del exchange.orders[link]
            if expired:
                policy = plan.policy.model_copy(
                    update={
                        "strategy_v2": plan.policy.strategy_v2.model_copy(
                            update={"entry_order_ttl_minutes": 1}
                        )
                    }
                )
                await store.update_execution_plan(
                    plan.model_copy(
                        update={
                            "created_at": datetime.now(UTC) - timedelta(days=1),
                            "policy": policy,
                        }
                    )
                )
            before = len(exchange.requests)
            restarted_store = IntentStore(database)
            restarted = PositionSupervisor(
                store=restarted_store, executor=executor, mutation_lock=asyncio.Lock()
            )
            await restarted.reconcile_once()
            assert not await restarted_store.get_active_position_strategies()
            alerts = await restarted_store.get_pending_strategy_alerts()
            assert (
                len(alerts) == 1 and alerts[0].status is StrategyStatus.MANUAL_OVERRIDE
            )
            if change == "legacy":
                assert "snapshot unavailable" in alerts[0].pause_reason
            else:
                assert link in alerts[0].pause_reason
            assert exchange.stop == Decimal("90")
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

            bot = ApprovalBot(
                token="123456:integration-token",
                approval_chat_id=1,
                approver_user_id=1,
                store=restarted_store,
                executor=coordinator,
            )
            try:
                with patch.object(
                    bot._bot,
                    "send_message",
                    new=AsyncMock(side_effect=RuntimeError("Telegram unavailable")),
                ):
                    await bot.deliver_strategy_alerts()
                assert (
                    len(await IntentStore(database).get_pending_strategy_alerts()) == 1
                )
                sender = AsyncMock()
                with patch.object(bot._bot, "send_message", new=sender):
                    await bot.deliver_strategy_alerts()
                    await bot.deliver_strategy_alerts()
                assert sender.await_count == 1
                assert alerts[0].pause_reason in sender.call_args.kwargs["text"]
                assert not await restarted_store.get_pending_strategy_alerts()
            finally:
                await bot.close()

    asyncio.run(run())


def test_intentionally_omitted_small_exits_are_not_missing_orders(tmp_path):
    class SmallExitExchange(Exchange):
        minimum = "0.001"

        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/v5/market/instruments-info":
                payload = response.json()
                payload["result"]["list"][0]["lotSizeFilter"]["minOrderQty"] = (
                    self.minimum
                )
                return httpx.Response(200, json=payload)
            return response

    async def run():
        exchange = SmallExitExchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.minimum = "2"
            exchange.mark = Decimal("102")
            await supervisor.reconcile_once()
            await supervisor.reconcile_once()
            assert (await state_for(store)).expected_exits == {}
            assert exchange.exits() == []
            assert exchange.stop == Decimal("90")
            assert not await store.get_pending_strategy_alerts()

    asyncio.run(run())


def test_schema_8_upgrade_preserves_legacy_strategy_and_is_idempotent(tmp_path):
    database = tmp_path / "state.db"
    original_id = str(uuid4())
    legacy_schema = LATEST_SCHEMA_SQL
    for column in (
        "    expected_exits_json TEXT,\n",
        "    pause_reason TEXT,\n",
        "    pause_notified INTEGER NOT NULL DEFAULT 0,\n",
    ):
        assert column in legacy_schema
        legacy_schema = legacy_schema.replace(column, "")
    with sqlite3.connect(database) as db:
        db.executescript(legacy_schema)
        db.execute("PRAGMA user_version = 8")
        db.execute(
            "INSERT INTO position_strategies(strategy_id, symbol, side, status, created_at, updated_at) "
            "VALUES (?, 'BTCUSDT', 'LONG', 'OPEN_RISK', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (original_id,),
        )

    async def run():
        store = IntentStore(database)
        await store.initialize()
        await store.initialize()

    asyncio.run(run())
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (LATEST_SCHEMA_VERSION,)
        row = db.execute(
            "SELECT strategy_id, status, expected_exits_json, pause_reason, pause_notified "
            "FROM position_strategies"
        ).fetchone()
        assert row == (original_id, "OPEN_RISK", None, None, 0)


@pytest.mark.parametrize("partial", [False, True])
def test_real_fixed_exit_fill_does_not_trigger_drift(tmp_path, partial):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = Decimal("102")
            await supervisor.reconcile_once()
            target = next(
                order for order in exchange.exits() if "-t1r" in order["orderLinkId"]
            )
            exchange.fill(
                target["orderLinkId"], Decimal(target["qty"]) / 2 if partial else None
            )
            await supervisor.reconcile_once()
            state = await state_for(store)
            assert state.entry_frozen and state.tp1_done is (not partial)
            assert not await store.get_pending_strategy_alerts()

    asyncio.run(run())
