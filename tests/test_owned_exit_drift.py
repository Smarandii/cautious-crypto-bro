"""Owned-exit supervision through real SQLite, HTTP adapter, and alert delivery."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, state_for

from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.domain import ApprovalMode, IntentStatus, StrategyStatus
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import (
    LATEST_SCHEMA_SQL,
    LATEST_SCHEMA_VERSION,
    IntentStore,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("change", [None, "cancel", "price"])
def test_fixed_exit_drift_blocks_partial_stop_handoff(tmp_path, change):
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
            await restarted.reconcile_once()
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
                assert exchange.orders["attached-stop"]["orderStatus"] == "Untriggered"
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
