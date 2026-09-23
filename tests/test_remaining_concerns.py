"""Regression checks using real SQLite and mocked exchange/Telegram transports."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from cautious_crypto_bro.bybit import BybitDemoExecutor
from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    ExecutionPolicy,
    IncomingPost,
    IntentExtraction,
    IntentStatus,
    Side,
    SignalExtraction,
    SourceMessage,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner, InstrumentContext
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.openrouter import _signals_from_extraction
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.service import SignalService
from cautious_crypto_bro.signal_context import SignalContextProvider
from cautious_crypto_bro.storage import IntentStore

D = Decimal


class Exchange:
    """Small stateful exchange boundary; unspecified protection fields persist."""

    def __init__(self, side=Side.LONG):
        self.side = side
        self.size = D("0")
        self.avg = D("100")
        self.mark = D("100")
        self.fill_price = D("100")
        self.stop = D("0")
        self.trail = D("0")
        self.orders = {}
        self.requests = []
        self.market_fills_exits = False
        self.confirmation_timeout = False
        self.action_accepted = False

    def position(self):
        return {
            "symbol": "BTCUSDT",
            "side": "Buy" if self.side is Side.LONG else "Sell",
            "size": str(self.size),
            "avgPrice": str(self.avg),
            "markPrice": str(self.mark),
            "unrealisedPnl": "0",
            "positionStatus": "Normal",
            "stopLoss": str(self.stop),
            "breakEvenPrice": str(self.avg),
            "trailingStop": str(self.trail),
        }

    def fill(self, link, quantity=None, price=None):
        order = self.orders[link]
        quantity = D(order["leavesQty"]) if quantity is None else D(quantity)
        price = D(order["price"]) if price is None else D(price)
        assert quantity <= D(order["leavesQty"])
        if order["reduceOnly"]:
            self.size -= quantity
        else:
            self.avg = (self.size * self.avg + quantity * price) / (
                self.size + quantity
            )
            self.size += quantity
        order["leavesQty"] = str(D(order["leavesQty"]) - quantity)
        order["cumExecQty"] = str(D(order["cumExecQty"]) + quantity)
        order["avgPrice"] = str(price)
        order["orderStatus"] = (
            "Filled" if D(order["leavesQty"]) == 0 else "PartiallyFilled"
        )

    def create(self, body):
        link = body["orderLinkId"]
        if link in self.orders:
            return {
                "retCode": 110072,
                "retMsg": "OrderLinkedID is duplicate",
                "result": {},
            }
        quantity = D(body["qty"]) or self.size
        order = {
            "symbol": body["symbol"],
            "side": body["side"],
            "qty": str(quantity),
            "leavesQty": str(quantity),
            "price": body.get("price", str(self.fill_price)),
            "avgPrice": "0",
            "cumExecQty": "0",
            "orderId": f"id-{len(self.orders) + 1}",
            "orderLinkId": link,
            "orderType": body["orderType"],
            "orderStatus": "New",
            "reduceOnly": body.get("reduceOnly", False),
            "createdTime": "1",
            "updatedTime": "1",
        }
        self.orders[link] = order
        if body["orderType"] == "Market":
            self.fill(link, price=self.fill_price)
            if not order["reduceOnly"]:
                self.stop = D(body.get("stopLoss", "0"))
            else:
                self.action_accepted = True
        elif order["reduceOnly"] and self.market_fills_exits:
            marketable = (
                D(order["price"]) <= self.mark
                if self.side is Side.LONG
                else D(order["price"]) >= self.mark
            )
            if marketable:
                self.fill(link)
        return {
            "retCode": 0,
            "result": {"orderId": order["orderId"], "orderLinkId": link},
        }

    def handle(self, request):
        body = (
            json.loads(request.content) if request.content else dict(request.url.params)
        )
        path = request.url.path
        self.requests.append((path, body))
        result = {}
        if path == "/v5/market/time":
            result = {"timeSecond": str(int(datetime.now(UTC).timestamp()))}
        elif path == "/v5/market/tickers":
            result = {"list": [{"lastPrice": str(self.mark)}]}
        elif path == "/v5/market/instruments-info":
            result = {
                "list": [
                    {
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.001",
                            "minNotionalValue": "5",
                        },
                        "priceFilter": {"tickSize": "0.1"},
                    }
                ]
            }
        elif path == "/v5/account/wallet-balance":
            result = {"list": [{"totalWalletBalance": "6800"}]}
        elif path == "/v5/position/list":
            if self.confirmation_timeout and self.action_accepted:
                raise httpx.ReadTimeout(
                    "Injected after accepted order", request=request
                )
            result = {
                "list": [self.position()] if self.size else [],
                "nextPageCursor": "",
            }
        elif path in ("/v5/order/realtime", "/v5/order/history"):
            orders = list(self.orders.values())
            if "orderId" in body:
                orders = [o for o in orders if o["orderId"] == body["orderId"]]
            elif "orderLinkId" in body:
                orders = [o for o in orders if o["orderLinkId"] == body["orderLinkId"]]
            elif path.endswith("realtime"):
                orders = [
                    o for o in orders if o["orderStatus"] in ("New", "PartiallyFilled")
                ]
            result = {"list": orders, "nextPageCursor": ""}
        elif path == "/v5/order/create":
            return httpx.Response(200, json=self.create(body))
        elif path == "/v5/order/create-batch":
            responses = [self.create(item) for item in body["request"]]
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"list": [r["result"] for r in responses]},
                    "retExtInfo": {
                        "list": [
                            {"code": r["retCode"], "msg": r.get("retMsg", "")}
                            for r in responses
                        ]
                    },
                },
            )
        elif path == "/v5/order/cancel":
            order = next(
                o
                for o in self.orders.values()
                if o["orderId"] == body.get("orderId")
                or o["orderLinkId"] == body.get("orderLinkId")
            )
            order["orderStatus"] = "Cancelled"
            result = {"orderId": order["orderId"]}
        elif path == "/v5/position/trading-stop":
            if "stopLoss" in body:
                self.stop = D(body["stopLoss"])
            if "trailingStop" in body:
                self.trail = D(body["trailingStop"])
        else:
            raise AssertionError(f"Unexpected network request: {path}")
        return httpx.Response(200, json={"retCode": 0, "result": result})

    def open_entries(self):
        return [
            o
            for o in self.orders.values()
            if not o["reduceOnly"] and o["orderStatus"] in ("New", "PartiallyFilled")
        ]

    def exits(self):
        return [
            o
            for o in self.orders.values()
            if o["reduceOnly"] and o["orderStatus"] in ("New", "PartiallyFilled")
        ]


@contextmanager
def executor_for(exchange):
    executor = BybitDemoExecutor(api_key="synthetic-key", api_secret="synthetic-secret")
    executor._client.close()
    executor._client = httpx.Client(
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(exchange.handle),
    )
    try:
        yield executor
    finally:
        executor.close()


def source(text="Synthetic audit", message_id=1):
    now = datetime.now(UTC)
    return SourceMessage(
        channel_id=0,
        channel_title="Audit",
        message_id=message_id,
        published_at=now,
        received_at=now,
        text=text,
    )


def make_intent(side=Side.LONG, tp=None, mode=ApprovalMode.MANUAL):
    return TradingIntent(
        source=source(),
        symbol="BTCUSDT",
        side=side,
        entry=Entry(type=EntryType.MARKET),
        stop_loss=90 if side is Side.LONG else 110,
        take_profit=tp,
        summary="Audit",
        confidence=1,
        approval_mode=mode,
    )


async def prepare(
    path, exchange, executor, side=Side.LONG, tp=None, mode=ApprovalMode.MANUAL
):
    store = IntentStore(path)
    await store.initialize()
    intent = make_intent(side, tp, mode)
    planner = ExecutionPlanner()
    plan = planner.plan(
        intent,
        ExecutionPolicy(trading_capital_usdt=D("6800")),
        InstrumentContext(D("100"), D("0.1"), D("0.001"), D("0.001"), D("5")),
    )
    claim = await store.claim_source(intent.source, lease_seconds=300)
    await store.create_signal_batch_and_complete_source(((intent, plan),), (), claim)
    lock = asyncio.Lock()
    coordinator = ExecutionCoordinator(
        store=store, executor=executor, max_age_seconds=3600, execution_lock=lock
    )
    supervisor = PositionSupervisor(store=store, executor=executor, mutation_lock=lock)
    return store, intent, plan, coordinator, supervisor


async def state_for(store):
    return (await store.get_active_position_strategies())[0][0]


def test_immediate_fill_preserves_revision_and_protection_on_restart(
    tmp_path,
):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            assert (
                await coordinator.execute_intent(
                    intent.intent_id, approval_mode=ApprovalMode.MANUAL
                )
            ).status is IntentStatus.EXECUTED
            exchange.mark = D("106")
            exchange.market_fills_exits = True
            await supervisor.reconcile_once()
            assert exchange.stop == D("100.5") and exchange.trail == D("3")
            assert (await state_for(store)).exit_revision == 1
            exchange.mark = D("104")
            restarted = PositionSupervisor(
                store=IntentStore(tmp_path / "state.db"),
                executor=executor,
                mutation_lock=asyncio.Lock(),
            )
            await restarted.reconcile_once()
            assert exchange.stop == D("100.5")
            assert exchange.trail == D("3"), (
                "Actual adapter omits trailingStop, rather than sending zero"
            )

    asyncio.run(run())


@pytest.mark.parametrize("frozen", [False, True])
def test_02_removed_stop_before_and_after_freeze(tmp_path, frozen):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = D("106") if frozen else D("102")
            await supervisor.reconcile_once()
            exchange.stop = D("0")
            before = len(exchange.requests)
            for _ in range(3):
                await supervisor.reconcile_once()
            assert not any(
                p == "/v5/position/trading-stop" for p, _ in exchange.requests[before:]
            )
            assert not await store.get_active_position_strategies()  # MANUAL_OVERRIDE

    asyncio.run(run())


@pytest.mark.parametrize("fill_kind", ["partial", "full", "full_plus_entry"])
def test_03_exit_fill_detection_controls(tmp_path, fill_kind):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = D("102")
            await supervisor.reconcile_once()
            tp = next(o for o in exchange.exits() if "-t1" in o["orderLinkId"])
            exchange.fill(
                tp["orderLinkId"], D("0.5") if fill_kind == "partial" else None
            )
            exchange.mark = D("104")
            if fill_kind == "full_plus_entry":
                entry = next(
                    o
                    for o in exchange.open_entries()
                    if o["orderLinkId"].endswith("-e2")
                )
                exchange.fill(entry["orderLinkId"])
                exchange.mark = plan.orders[1].reference_price
            await supervisor.reconcile_once()
            state = await state_for(store)
            assert state.entry_frozen
            assert state.tp1_done is (fill_kind != "partial")
            assert not exchange.open_entries()
            if fill_kind == "partial":
                remaining = next(
                    o for o in exchange.exits() if o["orderLinkId"] == tp["orderLinkId"]
                )
                assert D(remaining["leavesQty"]) == D(tp["qty"]) - D("0.5")

    asyncio.run(run())


def test_tighter_manual_stop_is_preserved_when_entry_fills(tmp_path):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            _, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = D("102")
            await supervisor.reconcile_once()
            exchange.stop = D("95")
            entry = next(
                o for o in exchange.open_entries() if o["orderLinkId"].endswith("-e2")
            )
            exchange.fill(entry["orderLinkId"])
            exchange.mark = D(entry["price"])
            await supervisor.reconcile_once()
            assert exchange.stop == D("95")

    asyncio.run(run())


def normalize(text, kind="REDUCE", pct=25, evidence=None):
    raw = IntentExtraction.model_validate(
        {
            "actionable": True,
            "reason": "Audit",
            "position_actions": [
                {
                    "symbol": "BTCUSDT",
                    "action": kind,
                    "close_pct": pct,
                    "evidence_text": text if evidence is None else evidence,
                    "expected_side": "LONG",
                    "summary": "Audit",
                    "confidence": 1,
                }
            ],
        }
    )
    return _signals_from_extraction(source(text, 2), raw)


@pytest.mark.parametrize("side,tp", [(Side.LONG, 106), (Side.SHORT, 94)])
@pytest.mark.parametrize("accumulated", [False, True])
def test_10_trader_cap_after_actual_market_rebase(tmp_path, side, tp, accumulated):
    async def run():
        exchange = Exchange(side)
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor, side, tp
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            if accumulated:
                for entry in list(exchange.open_entries()):
                    exchange.fill(entry["orderLinkId"])
            exchange.mark = exchange.avg
            await supervisor.reconcile_once()
            prices = [D(o["price"]) for o in exchange.exits()]
            exceeds = (
                any(p > D(tp) for p in prices)
                if side is Side.LONG
                else any(p < D(tp) for p in prices)
            )
            assert not exceeds
            assert len(set(prices)) == 3

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["OPEN", "REDUCE"])
def test_manual_delivery_retries_after_restart_without_replaying_source(tmp_path, kind):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    from cautious_crypto_bro.approval_bot import ApprovalBot

    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, existing, _, coordinator, _ = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            token = await store.claim_manual_delivery(existing.intent_id)
            await store.finish_manual_delivery(
                existing.intent_id, token, delivered=True
            )
            signals = normalize("Close 25% of BTC position now.")
            if kind == "OPEN":
                new_intent = make_intent().model_copy(
                    update={"source": source("Open BTC", 2)}
                )
                signals = SignalExtraction(open_intents=(new_intent,))
            signal = (
                signals.open_intents[0]
                if kind == "OPEN"
                else signals.position_actions[0]
            )
            bot = ApprovalBot(
                token="123456:synthetic-audit-token",
                approval_chat_id=1,
                approver_user_id=1,
                store=store,
                coordinator=coordinator,
            )
            sender = AsyncMock(
                side_effect=TelegramNetworkError(
                    method=SendMessage(chat_id=1, text="audit"),
                    message="Injected network outage",
                )
            )

            def service_for(current_store):
                service = SignalService(
                    store=current_store,
                    extractor=SimpleNamespace(extract=AsyncMock(return_value=signals)),
                    planner=ExecutionPlanner(),
                    executor=executor,
                    approval_bot=bot,
                    coordinator=coordinator,
                    context_provider=SignalContextProvider(
                        store=current_store, executor=executor
                    ),
                )
                service._sync_account_pnl = AsyncMock(return_value=None)
                return service

            try:
                with patch.object(bot._bot, "send_message", sender):
                    await service_for(store).on_message(
                        IncomingPost(source=signal.source)
                    )
                    attempts = sender.await_count
                    sender.side_effect = None
                    restarted = service_for(IntentStore(tmp_path / "state.db"))
                    import aiosqlite

                    async with aiosqlite.connect(tmp_path / "state.db") as db:
                        await db.execute(
                            "UPDATE manual_deliveries SET retry_after = NULL"
                        )
                        await db.commit()
                    await restarted.recover_manual_deliveries()
                    await restarted.on_message(IncomingPost(source=signal.source))
                    assert (
                        sender.await_count == attempts + 1 == 3
                    )  # failed snapshot plus failed actionable card
                    await restarted.recover_manual_deliveries()
                    assert sender.await_count == 3
                    saved = (
                        (await store.get_intent(signal.intent_id))
                        if kind == "OPEN"
                        else (await store.get_position_action(signal.action_id))
                    )
                    assert saved.status is IntentStatus.PENDING
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("crash_at", ["after_protection", "after_fill"])
def test_interrupted_install_resumes_without_reusing_filled_order(tmp_path, crash_at):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = D("106")
            exchange.market_fills_exits = True
            original_place = executor.place_reduce_only_exit

            async def crash(**kwargs):
                if crash_at == "after_fill":
                    await original_place(**kwargs)
                raise RuntimeError("Simulated process death")

            executor.place_reduce_only_exit = crash
            with pytest.raises(RuntimeError, match="Simulated process death"):
                await supervisor.reconcile_once()
            saved = await state_for(store)
            assert saved.installing_exits and saved.exit_revision == 1
            assert saved.protected_stop_loss == exchange.stop == D("100.5")
            executor.place_reduce_only_exit = original_place
            exchange.mark = D("104")
            restarted = PositionSupervisor(
                store=IntentStore(tmp_path / "state.db"),
                executor=executor,
                mutation_lock=asyncio.Lock(),
            )
            await restarted.reconcile_once()
            await restarted.reconcile_once()
            saved = await state_for(store)
            assert not saved.installing_exits and saved.exit_revision == 1
            assert saved.entry_frozen and saved.trailing_active
            assert exchange.stop == D("100.5") and exchange.trail == D("3")
            creates = [
                b["orderLinkId"]
                for path, b in exchange.requests
                if path == "/v5/order/create" and b.get("reduceOnly")
            ]
            assert len(creates) == len(set(creates)) == 3

    asyncio.run(run())


def test_cancelled_exit_is_not_inferred_as_profit_fill(tmp_path):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = D("102")
            await supervisor.reconcile_once()
            order = exchange.exits()[0]
            order["orderStatus"] = "Cancelled"
            order["leavesQty"] = "0"
            exchange.size -= D("0.1")  # unrelated manual reduction
            await supervisor.reconcile_once()
            saved = await state_for(store)
            assert not any((saved.tp1_done, saved.tp2_done, saved.tp3_done))

    asyncio.run(run())


def test_manual_delivery_claim_survives_restart_and_has_one_owner(tmp_path):
    async def run():
        exchange = Exchange()
        with executor_for(exchange) as executor:
            store, intent, _, _, _ = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            other = IntentStore(tmp_path / "state.db")
            first, second = await asyncio.gather(
                store.claim_manual_delivery(intent.intent_id),
                other.claim_manual_delivery(intent.intent_id),
            )
            assert sum(t is not None for t in (first, second)) == 1
            assert not await other.pending_manual_deliveries()
            import aiosqlite

            async with aiosqlite.connect(tmp_path / "state.db") as db:
                await db.execute(
                    "UPDATE manual_deliveries SET retry_after = datetime('now', '-1 second')"
                )
                await db.commit()
            replacement = await other.claim_manual_delivery(intent.intent_id)
            assert replacement and replacement not in (first, second)
            await store.finish_manual_delivery(
                intent.intent_id, first or second, delivered=True
            )
            assert not await store.claim_manual_delivery(intent.intent_id)
            await other.finish_manual_delivery(
                intent.intent_id, replacement, delivered=True
            )
            assert not await other.pending_manual_deliveries()

    asyncio.run(run())


@pytest.mark.parametrize("side,cap", [(Side.LONG, 112), (Side.SHORT, 88)])
@pytest.mark.parametrize("legacy", [False, True])
def test_distant_trader_cap_survives_policy_targets_and_legacy_storage(
    tmp_path, side, cap, legacy
):
    async def run():
        exchange = Exchange(side)
        with executor_for(exchange) as executor:
            store, intent, plan, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor, side, cap
            )
            from cautious_crypto_bro.domain import TakeProfitSource

            assert plan.take_profit_source is TakeProfitSource.POLICY
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            if legacy:
                import aiosqlite

                async with aiosqlite.connect(tmp_path / "state.db") as db:
                    cursor = await db.execute(
                        "SELECT payload_json FROM execution_plans WHERE intent_id = ?",
                        (str(intent.intent_id),),
                    )
                    payload = json.loads((await cursor.fetchone())[0])
                    payload.pop("trader_take_profit")
                    await db.execute(
                        "UPDATE execution_plans SET payload_json = ? WHERE intent_id = ?",
                        (json.dumps(payload), str(intent.intent_id)),
                    )
                    await db.commit()
            exchange.mark = exchange.avg
            await supervisor.reconcile_once()
            prices = [D(o["price"]) for o in exchange.exits()]
            assert len(set(prices)) == 3
            assert all(p <= cap if side is Side.LONG else p >= cap for p in prices)

    asyncio.run(run())
