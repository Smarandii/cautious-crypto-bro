"""Opt-in live Demo checks. Use run_bybit_demo_e2e.py to isolate the app.

Real planner, coordinator, SQLite, exchange and fresh-process recovery.
No Telegram sends, LLM requests or production SQLite writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from uuid import uuid4

from cautious_crypto_bro.bybit import (
    DEMO_BASE_URL,
    BybitDemoExecutor,
    TradeExecutionError,
)
from cautious_crypto_bro.config import get_settings
from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    ExecutionPolicy,
    IntentExtraction,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
    SourceMessage,
    StrategyStatus,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.openrouter import _signals_from_extraction
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

D = Decimal
SYMBOL = "DOGEUSDT"


class ProbeOwnership:
    """Keep exact submissions and reconcile exposure from a verified flat start."""

    def __init__(self, exchange, journal_path):
        self.exchange = exchange
        self.started_ms = int(datetime.now(UTC).timestamp() * 1000)
        self.links = set()
        self.order_ids = set()
        self.close_submitted = False
        self.observations = {}
        self.original_post = exchange._client.private_post
        self.journal_path = journal_path
        self.journal_lock = threading.RLock()

    def save(self):
        with self.journal_lock:
            data = {
                "version": 1,
                "symbol": SYMBOL,
                "started_ms": self.started_ms,
                "submitted_links": sorted(self.links),
                "order_ids": sorted(self.order_ids),
                "cleanup_close_submitted": self.close_submitted,
                "observations": self.observations,
            }
            temporary = self.journal_path.with_suffix("." + uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(data, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.journal_path)
            finally:
                temporary.unlink(missing_ok=True)

    def submit(self, path, params):
        if path == "/v5/order/create" and params.get("symbol") == SYMBOL:
            link = params.get("orderLinkId")
            require(bool(link), "Probe submission requires an exact order link")
            # Retain the submitted identity even when its acknowledgment is lost.
            with self.journal_lock:
                self.links.add(link)
                self.save()
            response = self.original_post(path, params)
            order_id = response.get("result", {}).get("orderId")
            if order_id:
                with self.journal_lock:
                    self.order_ids.add(order_id)
                    self.save()
            return response
        return self.original_post(path, params)

    def owns_order(self, order):
        return (
            order.order_id in self.order_ids
            or order.order_link_id in self.links
            or getattr(order, "parent_order_link_id", None) in self.links
        )

    async def verify(self, account, side):
        orders = [o for o in account.open_orders if o.symbol == SYMBOL]
        require(all(self.owns_order(o) for o in orders), "Unknown probe-symbol order")
        with self.journal_lock:
            self.order_ids.update(o.order_id for o in orders)
            self.save()
        executions = await asyncio.to_thread(
            self.exchange._client.paginate_private_list,
            "/v5/execution/list",
            {
                "category": "linear",
                "symbol": SYMBOL,
                "startTime": self.started_ms,
                "limit": 100,
            },
        )
        signed_quantity = D(0)
        seen = set()
        for execution in executions:
            if execution.get("execType") == "Funding":
                continue
            execution_id = execution.get("execId")
            require(
                bool(execution_id) and execution_id not in seen,
                "Ambiguous execution identity",
            )
            seen.add(execution_id)
            require(execution.get("symbol") == SYMBOL, "Unexpected execution symbol")
            require(
                execution.get("orderId") in self.order_ids
                or execution.get("orderLinkId") in self.links,
                "Foreign or unattributed probe-symbol execution",
            )
            require(execution.get("side") in {"Buy", "Sell"}, "Unknown execution side")
            quantity = D(execution["execQty"])
            require(quantity > 0, "Invalid execution quantity")
            signed_quantity += quantity if execution["side"] == "Buy" else -quantity
        live = position(account)
        actual = (
            D(0) if live is None else live.size * (1 if live.side is Side.LONG else -1)
        )
        require(signed_quantity == actual, "Run executions do not explain net exposure")
        require(live is None or live.side is side, "Unexpected probe position side")
        return abs(signed_quantity)

    @classmethod
    async def start(cls, exchange, *, journal_path=None):
        if journal_path is None:
            directory = os.environ.get("CCB_PROBE_EVIDENCE_DIR")
            require(bool(directory), "Persistent probe evidence directory required")
            journal_path = Path(directory) / ("ownership-" + uuid4().hex + ".json")
        require(journal_path.parent.is_dir(), "Probe evidence directory is unavailable")
        owner = cls(exchange, journal_path)
        require(
            symbol_flat(await exchange.account_state()),
            "Ownership journal requires a flat start",
        )
        owner.save()
        exchange._client.private_post = owner.submit
        return owner

    def restore(self):
        self.exchange._client.private_post = self.original_post

    def record_observation(self, name, payload):
        with self.journal_lock:
            self.observations[name] = payload
            self.save()


def emit(check, **values):
    print(json.dumps({"check": check, **values}, default=str), flush=True)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def rounded(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def source(message_id, text="Synthetic isolated Demo E2E"):
    now = datetime.now(UTC)
    return SourceMessage(
        channel_id=0,
        channel_title="Demo E2E",
        message_id=message_id,
        published_at=now,
        received_at=now,
        text=text,
    )


def executor():
    settings = get_settings()
    require(DEMO_BASE_URL == "https://api-demo.bybit.com", "Demo endpoint required")
    return BybitDemoExecutor(
        api_key=settings.bybit_api_key, api_secret=settings.bybit_api_secret
    )


async def wait_account(e, predicate, description, seconds=15):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        account = await e.account_state()
        if predicate(account):
            return account
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out: {description}")
        await asyncio.sleep(0.3)


def position(account):
    return next((p for p in account.positions if p.symbol == SYMBOL), None)


def exits(account):
    return [
        o
        for o in account.open_orders
        if o.symbol == SYMBOL and o.reduce_only and "-t" in o.order_link_id
    ]


def entries(account):
    return [o for o in account.open_orders if o.symbol == SYMBOL and not o.reduce_only]


def symbol_flat(account):
    return position(account) is None and not any(
        o.symbol == SYMBOL for o in account.open_orders
    )


async def strategy(store):
    rows = await store.get_active_position_strategies()
    require(len(rows) == 1, f"Expected one managed strategy, got {len(rows)}")
    return rows[0][0]


async def reconcile_in_fresh_process(database):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--resume-db",
        str(database),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    require(
        process.returncode == 0,
        f"Fresh process failed: {output.decode()[-600:]} {error.decode()[-600:]}",
    )
    require('"fresh_process": "passed"' in output.decode(), "Missing recovery receipt")


async def persist_action(store, side, kind, message_id, pct=None):
    if kind is PositionActionType.REDUCE:
        caption = "Закрываем часть DOGE, 25% позиции."
        extraction = IntentExtraction.model_validate(
            {
                "actionable": True,
                "reason": "E2E",
                "position_actions": [
                    {
                        "symbol": SYMBOL,
                        "action": "REDUCE",
                        "close_pct": pct,
                        "expected_side": side.value,
                        "evidence_text": caption,
                        "summary": "E2E reduction",
                        "confidence": 1,
                    }
                ],
            }
        )
        actions = _signals_from_extraction(
            source(message_id, caption), extraction
        ).position_actions
        require(
            len(actions) == 1 and actions[0].close_pct == pct,
            "Reduction percentage changed",
        )
        action = actions[0]
    else:
        action = PositionActionIntent(
            source=source(message_id),
            symbol=SYMBOL,
            action=kind,
            expected_side=side,
            summary="E2E close",
            confidence=1,
        )
    claim = await store.claim_source(action.source, lease_seconds=300)
    require(
        await store.create_signal_batch_and_complete_source((), (action,), claim),
        "Action persistence failed",
    )
    return action


async def cleanup(e, side, prefix, *, ownership=None):
    for attempt in range(3):
        try:
            account = await e.account_state()
            if ownership is None:
                require(
                    symbol_flat(account), "Run ownership evidence required for cleanup"
                )
                emit("cleanup", symbol=SYMBOL, side=side.value, flat=True)
                return
            owned_quantity = await ownership.verify(account, side)
            if position(account):
                require(
                    not ownership.close_submitted,
                    "Cleanup close outcome unresolved; no replay",
                )
                ownership.close_submitted = True
                ownership.save()
                # Freeze the attributable quantity; a later account read must not
                # expand cleanup to somebody else's new same-side exposure.
                await asyncio.to_thread(
                    e._client.private_post,
                    "/v5/order/create",
                    {
                        "category": "linear",
                        "symbol": SYMBOL,
                        "side": "Sell" if side is Side.LONG else "Buy",
                        "orderType": "Market",
                        "qty": str(owned_quantity),
                        "reduceOnly": True,
                        "positionIdx": 0,
                        "orderLinkId": prefix + "cleanup",
                    },
                )
            await wait_account(e, lambda a: position(a) is None, "cleanup close")
            account = await e.account_state()
            await ownership.verify(account, side)
            for order in account.open_orders:
                if order.symbol == SYMBOL:
                    await ownership.verify(await e.account_state(), side)
                    await e.cancel_order(SYMBOL, order.order_id)
            await wait_account(e, symbol_flat, "cleanup flat")
            emit("cleanup", symbol=SYMBOL, side=side.value, flat=True)
            return
        except Exception as exc:
            emit("cleanup_retry", attempt=attempt + 1, error=type(exc).__name__)
    raise RuntimeError("Cleanup unverified; keep normal app paused")


async def run_side(e, side, directory):
    before = await e.account_state()
    require(
        symbol_flat(before),
        "Test symbol must be completely flat",
    )
    database = directory / f"{side.value}.sqlite3"
    store = IntentStore(database)
    await store.initialize()
    context = await e.market_context(SYMBOL)
    sign = D(1) if side is Side.LONG else D(-1)
    stop = rounded(context.market_price * (1 - sign * D("0.05")), context.tick_size)
    cap = rounded(context.market_price * (1 + sign * D("0.03")), context.tick_size)
    intent = TradingIntent(
        source=source(1),
        symbol=SYMBOL,
        side=side,
        entry=Entry(type=EntryType.MARKET),
        stop_loss=float(stop),
        take_profit=float(cap),
        summary="Isolated Demo E2E",
        confidence=1,
    )
    plan = ExecutionPlanner().plan(
        intent,
        ExecutionPolicy(trading_capital_usdt=D("1000"), risk_per_trade_pct=D("0.45")),
        context,
    )
    # Reserve 10% of the $5 test budget for quote movement between planning
    # and submission. Production risk validation remains fully enabled.
    plan = plan.model_copy(
        update={
            "policy": plan.policy.model_copy(update={"risk_per_trade_pct": D("0.5")})
        }
    )
    initial_notional = plan.orders[0].quantity * context.market_price
    total_notional = sum(o.quantity * o.reference_price for o in plan.orders)
    require(
        initial_notional <= 75 and total_notional <= 200, "E2E notional bounds exceeded"
    )
    require(plan.planned_max_loss_usdt <= 5, "E2E risk bound exceeded")
    claim = await store.claim_source(intent.source, lease_seconds=300)
    require(
        await store.create_signal_batch_and_complete_source(
            ((intent, plan),), (), claim
        ),
        "Intent persistence failed",
    )
    require(
        await store.claim_source(intent.source, lease_seconds=300) is None,
        "Source deduplication failed",
    )
    lock = asyncio.Lock()
    coordinator = ExecutionCoordinator(
        store=store,
        executor=e,
        planner=ExecutionPlanner(),
        max_age_seconds=1800,
        execution_lock=lock,
    )
    supervisor = PositionSupervisor(store=store, executor=e, mutation_lock=lock)
    prefix = f"ccb-v2-{intent.intent_id.hex[:20]}-"
    ownership = await ProbeOwnership.start(e)
    emit(
        "mutation_started",
        side=side.value,
        initial_notional=initial_notional,
        maximum_planned_loss=plan.planned_max_loss_usdt,
        total_entry_notional=total_notional,
    )
    try:
        result = await coordinator.execute_intent(
            intent.intent_id, approval_mode=ApprovalMode.MANUAL
        )
        require(
            result.status is IntentStatus.EXECUTED, f"OPEN failed: {result.message}"
        )
        account = await wait_account(
            e,
            lambda a: position(a) is not None and len(entries(a)) == 2,
            "E1 and E2/E3",
        )
        live = position(account)
        require(live.side is side, "Wrong position side")
        require(
            live.stop_loss == stop
            or any(o.symbol == SYMBOL and o.kind == "SL" for o in account.open_orders),
            "E1 lacks protection",
        )
        emit(
            "entry_and_immediate_protection",
            side=side.value,
            result="passed",
            quantity=live.size,
        )

        if side is Side.SHORT:
            original = e.place_reduce_only_exit

            async def crash_after_accept(**kwargs):
                await original(**kwargs)
                raise RuntimeError("Injected process interruption after accepted exit")

            e.place_reduce_only_exit = crash_after_accept
            try:
                try:
                    await supervisor.reconcile_once()
                except RuntimeError as exc:
                    require(str(exc).startswith("Injected process"), str(exc))
                else:
                    raise AssertionError("Crash hook was not exercised")
            finally:
                e.place_reduce_only_exit = original
            require(
                (await strategy(store)).installing_exits,
                "Missing installation checkpoint",
            )
            accepted = {o.order_id for o in exits(await e.account_state())}
            require(
                len(accepted) == 1, "Expected one accepted exit before interruption"
            )
            await reconcile_in_fresh_process(database)
            require(
                accepted <= {o.order_id for o in exits(await e.account_state())},
                "Recovery replaced accepted exit",
            )
            emit(
                "interrupted_install_recovery",
                side=side.value,
                result="passed",
                fault="application exception after real acceptance",
            )
        else:
            await supervisor.reconcile_once()

        account = await wait_account(
            e,
            lambda a: len(exits(a)) == 3 and position(a).stop_loss == stop,
            "three exits and full stop",
        )
        prices = [o.price for o in exits(account)]
        require(len(set(prices)) == 3, "Exit prices collapsed")
        require(
            all(p <= cap if side is Side.LONG else p >= cap for p in prices),
            "Trader cap exceeded",
        )
        require(
            not any(
                o.symbol == SYMBOL and o.stop_order_type == "PartialStopLoss"
                for o in account.open_orders
            ),
            "Partial stop handoff incomplete",
        )
        ids = {o.order_id for o in exits(account)}
        await reconcile_in_fresh_process(database)
        require(
            ids == {o.order_id for o in exits(await e.account_state())},
            "Unchanged restart rebuilt exits",
        )
        emit(
            "cap_handoff_and_restart",
            side=side.value,
            result="passed",
            cap=cap,
            prices=prices,
        )

        saved = await strategy(store)
        tight = rounded(stop + sign * abs(live.avg_price - stop) / 5, context.tick_size)
        for mode, target in (("tightened", tight), ("removed", D(0))):
            await asyncio.to_thread(
                e._client.private_post,
                "/v5/position/trading-stop",
                {
                    "category": "linear",
                    "symbol": SYMBOL,
                    "positionIdx": 0,
                    "tpslMode": "Full",
                    "stopLoss": str(target),
                },
            )
            try:
                await wait_account(
                    e,
                    lambda a, target=target: position(a).stop_loss == (target or None),
                    "manual stop change",
                )
                await supervisor.reconcile_once()
                require(
                    not await store.get_active_position_strategies(),
                    "Stop change failed to quarantine",
                )
                require(
                    position(await e.account_state()).stop_loss == (target or None),
                    "Manual stop overwritten",
                )
                emit("manual_stop_" + mode, side=side.value, result="passed")
            finally:
                await e.set_position_protection(SYMBOL, stop)
                await wait_account(
                    e, lambda a: position(a).stop_loss == stop, "restore test stop"
                )
                await store.save_position_strategy(saved)

        account = await e.account_state()
        tp1 = next(o for o in exits(account) if "-t1r" in o.order_link_id)
        context = await e.market_context(SYMBOL)
        crossing = rounded(
            context.market_price * (1 - sign * D("0.005")), context.tick_size
        )
        # Test-only geometry makes this reduce-only exit fill on demand. Update
        # its disposable snapshot explicitly; production treats amendments as drift.
        fill_state = await strategy(store)
        require(fill_state.expected_exits is not None, "Missing fixed-exit snapshot")
        expected_exits = dict(fill_state.expected_exits)
        expected_exits[tp1.order_link_id] = (tp1.quantity, crossing)
        await store.save_position_strategy(
            fill_state.model_copy(update={"expected_exits": expected_exits})
        )
        await asyncio.to_thread(
            e._client.private_post,
            "/v5/order/amend",
            {
                "category": "linear",
                "symbol": SYMBOL,
                "orderId": tp1.order_id,
                "price": str(crossing),
            },
        )
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            filled = await e.strategy_order(SYMBOL, tp1.order_link_id)
            if filled is not None and filled.status == "Filled":
                break
            require(asyncio.get_running_loop().time() < deadline, "TP did not fill")
            await asyncio.sleep(0.3)
        require(filled.executed_quantity > 0, "Filled order lacks executed quantity")
        await supervisor.reconcile_once()
        require(
            (await strategy(store)).entry_frozen and (await strategy(store)).tp1_done,
            "TP fill failed to freeze entries",
        )
        await wait_account(
            e, lambda a: not entries(a) and len(exits(a)) == 2, "TP freeze/cancel"
        )
        await reconcile_in_fresh_process(database)
        require((await strategy(store)).tp1_done, "Restart lost TP completion")
        emit(
            "real_tp_fill_and_freeze",
            side=side.value,
            result="passed",
            trigger="test-only target/snapshot adjusted to marketable price",
        )

        action = await persist_action(store, side, PositionActionType.REDUCE, 2, 25)
        outcome = await coordinator.execute_position_action(
            action.action_id, approval_mode=ApprovalMode.MANUAL
        )
        require(
            outcome.status is IntentStatus.EXECUTED and outcome.result is not None,
            f"REDUCE failed: {outcome.message}",
        )
        expected = (
            outcome.result.position_size_before - outcome.result.submitted_quantity
        )
        await wait_account(
            e,
            lambda a: position(a) is not None and position(a).size == expected,
            "exact 25% reduction",
        )
        await supervisor.reconcile_once()
        account = await e.account_state()
        require(
            len(exits(account)) == 2 and not entries(account),
            "Reduction exit rebuild wrong",
        )
        require(
            all("-t1r" not in o.order_link_id for o in exits(account)),
            "Completed TP1 recreated",
        )
        require(
            all(
                o.price <= cap if side is Side.LONG else o.price >= cap
                for o in exits(account)
            ),
            "Rebuilt cap exceeded",
        )
        emit(
            "russian_25pct_reduce_and_rebuild",
            side=side.value,
            result="passed",
            remainder=expected,
        )

        if side is Side.SHORT:
            action = await persist_action(store, side, PositionActionType.REDUCE, 3, 25)
            original_read = e.account_state

            async def unavailable():
                raise TradeExecutionError("Injected confirmation transport failure")

            e.account_state = unavailable
            try:
                outcome = await coordinator.execute_position_action(
                    action.action_id, approval_mode=ApprovalMode.MANUAL
                )
            finally:
                e.account_state = original_read
            require(
                outcome.status is IntentStatus.UNCERTAIN and outcome.result is not None,
                "Accepted uncertain reduction lost result",
            )
            require(
                (await strategy(store)).status is StrategyStatus.UNCERTAIN,
                "Strategy not quarantined",
            )
            expected = (
                outcome.result.position_size_before - outcome.result.submitted_quantity
            )
            await wait_account(
                e,
                lambda a: position(a).size == expected,
                "accepted uncertain reduction",
            )
            await reconcile_in_fresh_process(database)
            require(
                (await strategy(store)).status is StrategyStatus.UNCERTAIN
                and not exits(await e.account_state()),
                "Quarantine mutated exchange",
            )
            emit(
                "accepted_reduce_transport_failure",
                side=side.value,
                result="passed",
                fault="injected client read failure; real exchange reduction",
            )

        action = await persist_action(store, side, PositionActionType.CLOSE, 4)
        outcome = await coordinator.execute_position_action(
            action.action_id, approval_mode=ApprovalMode.MANUAL
        )
        require(
            outcome.status is IntentStatus.EXECUTED, f"CLOSE failed: {outcome.message}"
        )
        await supervisor.reconcile_once()
        await wait_account(e, symbol_flat, "CLOSE flat")
        emit("coordinator_close", side=side.value, result="passed")
    finally:
        try:
            await cleanup(e, side, prefix, ownership=ownership)
        finally:
            ownership.restore()


async def main(args):
    if not args.resume_db:
        require(
            args.execute_demo,
            "Explicit --execute-demo required; use orchestration script",
        )
    e = executor()
    try:
        if args.resume_db:
            path = args.resume_db.resolve()
            require(
                path.parent.name.startswith("ccb-e2e-"),
                "Resume only disposable E2E databases",
            )
            await PositionSupervisor(
                store=IntentStore(path), executor=e, mutation_lock=asyncio.Lock()
            ).reconcile_once()
            emit("resume", fresh_process="passed")
            return
        require(
            args.execute_demo,
            "Explicit --execute-demo required; use orchestration script",
        )
        account = await e.account_state()
        require(
            symbol_flat(account),
            "Test symbol must be flat before E2E",
        )
        emit(
            "isolation",
            test_symbol=SYMBOL,
            untouched_symbols=sorted({p.symbol for p in account.positions}),
        )
        with tempfile.TemporaryDirectory(prefix="ccb-e2e-") as directory:
            zone_mode = getattr(args, "zone_protection", False) or getattr(
                args, "zone_pair_cancellation", False
            )
            for side in (Side.LONG, Side.SHORT):
                if zone_mode:
                    await observe_zone_stops(
                        e,
                        side,
                        paired_cancellation=getattr(
                            args, "zone_pair_cancellation", False
                        ),
                    )
                else:
                    await run_side(e, side, Path(directory))
        emit(
            "suite",
            result="passed",
            coverage=(
                "native independent-exit stop observation; no activation proof"
                if zone_mode
                else "real exchange plus labeled application fault injection"
            ),
            not_live_tested=[
                "partial-fill timing",
                "immediate fill during installation",
                "Telegram delivery",
                "LLM accuracy",
                "paired-stop amendment/cancellation races",
                "independent-zone offline/restart lifecycle",
            ],
        )
    finally:
        e.close()


def zone_children(account, parent, kind):
    return [
        order
        for order in account.open_orders
        if order.symbol == SYMBOL
        and order.kind == kind
        and order.parent_order_link_id == parent
    ]


def zone_pair_snapshot(account, parents, quantities, stops, targets):
    snapshot = []
    for parent, quantity, stop, target in zip(
        parents, quantities, stops, targets, strict=True
    ):
        pair = {}
        for kind, trigger in (("SL", stop), ("TP", target)):
            orders = zone_children(account, parent, kind)
            require(len(orders) == 1, "Ambiguous or missing parent-linked pair child")
            order = orders[0]
            require(
                order.remaining_quantity == quantity and order.trigger_price == trigger,
                "Pair child does not match its original allocation",
            )
            pair[kind] = order
        snapshot.append(pair)
    identities = [order.order_id for pair in snapshot for order in pair.values()]
    require(
        len(set(identities)) == len(identities), "Allocations share a child identity"
    )
    return snapshot


def require_original_pair(account, parent, pair):
    for kind, original in pair.items():
        orders = zone_children(account, parent, kind)
        require(
            len(orders) == 1
            and orders[0].order_id == original.order_id
            and orders[0].remaining_quantity == original.remaining_quantity
            and orders[0].trigger_price == original.trigger_price,
            "Remaining zone protection changed",
        )


async def terminal_child(e, order_id):
    deadline = asyncio.get_running_loop().time() + 15
    while True:
        response = await asyncio.to_thread(
            e._client.private_get,
            "/v5/order/history",
            {"category": "linear", "symbol": SYMBOL, "orderId": order_id},
        )
        rows = [
            row
            for row in response.get("result", {}).get("list", [])
            if row.get("orderId") == order_id
        ]
        require(len(rows) <= 1, "Ambiguous child history")
        if rows and rows[0].get("orderStatus") in {
            "Cancelled",
            "Deactivated",
            "Filled",
        }:
            return rows[0]["orderStatus"]
        require(
            asyncio.get_running_loop().time() < deadline,
            "Child terminal state unverified",
        )
        await asyncio.sleep(0.3)


def pair_evidence(account, parents):
    live = position(account)
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "net_quantity": str(live.size if live else 0),
        "children": [
            {
                "order_id": o.order_id,
                "parent_link": o.parent_order_link_id,
                "kind": o.kind,
                "remaining_quantity": str(o.remaining_quantity),
                "trigger_price": str(o.trigger_price),
            }
            for o in account.open_orders
            if o.symbol == SYMBOL and o.parent_order_link_id in parents
        ],
    }


async def observe_pair_cancellation(
    e, side, parents, snapshot, remaining_quantity, ownership
):
    account = await e.account_state()
    await ownership.verify(account, side)
    require(
        position(account) is not None and position(account).size == remaining_quantity,
        "B remainder changed before paired cancellation",
    )
    require_original_pair(account, parents[1], snapshot[1])
    original_tp = snapshot[0]["TP"]
    a_tps = zone_children(account, parents[0], "TP")
    require(
        len(a_tps) == 1 and a_tps[0].order_id == original_tp.order_id,
        "A TP no longer available for the planned cancellation",
    )
    acknowledged = True
    ownership.record_observation("before_pair_cancel", pair_evidence(account, parents))
    try:
        await e.cancel_order(SYMBOL, original_tp.order_id)
    except (TradeExecutionError, TimeoutError):
        acknowledged = False
    # A missing acknowledgment never causes a second cancellation or new order.
    status = await terminal_child(e, original_tp.order_id)
    require(status in {"Cancelled", "Deactivated"}, "A TP filled during cancellation")
    account = await e.account_state()
    await ownership.verify(account, side)
    require(
        position(account) is not None and position(account).size == remaining_quantity,
        "B remainder changed after paired cancellation",
    )
    require_original_pair(account, parents[1], snapshot[1])
    original_sl = snapshot[0]["SL"]
    a_stops = zone_children(account, parents[0], "SL")
    require(
        not a_stops
        or (len(a_stops) == 1 and a_stops[0].order_id == original_sl.order_id),
        "A peer stop identity changed",
    )
    peer_status = (
        "Untriggered" if a_stops else await terminal_child(e, original_sl.order_id)
    )
    require(peer_status != "Filled", "A stale stop consumed remaining exposure")
    final_account = await e.account_state()
    await ownership.verify(final_account, side)
    require(
        position(final_account) is not None
        and position(final_account).size == remaining_quantity,
        "B remainder changed during terminal confirmation",
    )
    require_original_pair(final_account, parents[1], snapshot[1])
    final_stops = zone_children(final_account, parents[0], "SL")
    require(
        not final_stops
        or (
            len(final_stops) == 1
            and final_stops[0].order_id == original_sl.order_id
            and final_stops[0].remaining_quantity == original_sl.remaining_quantity
            and final_stops[0].trigger_price == original_sl.trigger_price
        ),
        "A peer stop changed during terminal confirmation",
    )
    if a_stops and not final_stops:
        peer_status = await terminal_child(e, original_sl.order_id)
        require(peer_status != "Filled", "A stale stop consumed remaining exposure")
    require(not final_stops or bool(a_stops), "A terminal peer stop reappeared")
    a_stops = final_stops
    ownership.record_observation(
        "after_pair_cancel", pair_evidence(final_account, parents)
    )
    ownership.record_observation(
        "pair_terminal_history",
        {
            "a_tp_order_id": original_tp.order_id,
            "a_tp_status": status,
            "a_sl_order_id": original_sl.order_id,
            "a_sl_status": peer_status,
            "cancellation_acknowledged": acknowledged,
        },
    )
    emit(
        "paired_zone_cancellation_observation",
        side=side.value,
        cancellation_acknowledged=acknowledged,
        a_tp_terminal=status,
        a_peer_sl_status=peer_status,
        a_peer_retired=not a_stops,
        a_peer_quantity_units=sum(o.remaining_quantity for o in a_stops)
        / original_sl.remaining_quantity,
        b_original_pair_preserved=True,
        activation_proven=False,
    )


def zone_entry_params(side, parent, quantity, stop, target=None):
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": "Buy" if side is Side.LONG else "Sell",
        "orderType": "Market",
        "qty": str(quantity),
        "timeInForce": "IOC",
        "positionIdx": 0,
        "orderLinkId": parent,
        "stopLoss": str(stop),
        "tpslMode": "Partial",
        "slOrderType": "Market",
    }
    if target is not None:
        params.update(takeProfit=str(target), tpOrderType="Market")
    return params


def zone_pairs_ready(account, parents):
    return all(
        len(zone_children(account, parent, kind)) == 1
        for parent in parents
        for kind in ("TP", "SL")
    )


async def observe_zone_stops(e, side, *, paired_cancellation=False):
    """Observe native behavior; a successful observation is not activation proof."""
    require(symbol_flat(await e.account_state()), "Zone probe symbol must be flat")
    context = await e.market_context(SYMBOL)
    # Reserve quote movement above the minimum, with bounded aggregate exposure.
    unit = max(context.min_qty, context.min_notional * D("1.2") / context.market_price)
    unit = rounded(unit, context.qty_step) + context.qty_step
    require(unit * 3 * context.market_price <= 50, "Zone probe notional bound exceeded")
    sign = D(1) if side is Side.LONG else D(-1)
    prefix = f"ccb-zone-{uuid4().hex[:16]}-"
    parents = [prefix + "a", prefix + "b"]
    quantities = [unit, unit * 2]
    stops = [
        rounded(context.market_price * (1 - sign * distance), context.tick_size)
        for distance in (D("0.05"), D("0.06"))
    ]
    targets = [
        rounded(context.market_price * (1 + sign * distance), context.tick_size)
        for distance in (D("0.08"), D("0.09"))
    ]

    def children(account, parent):
        return [
            o
            for o in account.open_orders
            if o.symbol == SYMBOL
            and o.kind == "SL"
            and o.parent_order_link_id == parent
        ]

    ownership = await ProbeOwnership.start(e)
    emit("mutation_started", side=side.value, scenario="independent_zone_stop")
    try:
        for index, parent in enumerate(parents):
            response = await asyncio.to_thread(
                e._client.private_post,
                "/v5/order/create",
                zone_entry_params(
                    side,
                    parent,
                    quantities[index],
                    stops[index],
                    targets[index] if paired_cancellation else None,
                ),
            )
            require(
                bool(response.get("result", {}).get("orderId")),
                "Missing entry acknowledgement",
            )
            expected = sum(quantities[: index + 1])
            account = await wait_account(
                e,
                lambda a, expected=expected, parent=parent: (
                    position(a) is not None
                    and position(a).size == expected
                    and bool(children(a, parent))
                ),
                "zone entry and parent-linked stop",
            )
            stop_orders = children(account, parent)
            require(len(stop_orders) == 1, "Ambiguous native stop identity")
            require(
                stop_orders[0].remaining_quantity == quantities[index]
                and stop_orders[0].trigger_price == stops[index],
                "Native stop does not cover its entry allocation",
            )
        before = await wait_account(
            e,
            lambda a: not paired_cancellation or zone_pairs_ready(a, parents),
            "parent-linked protection pairs",
        )
        pairs = (
            zone_pair_snapshot(before, parents, quantities, stops, targets)
            if paired_cancellation
            else None
        )
        ownership.record_observation("zone_entries", pair_evidence(before, parents))
        before_ids = [children(before, parent)[0].order_id for parent in parents]
        require(before_ids[0] != before_ids[1], "Zones share one stop identity")
        response = await asyncio.to_thread(
            e._client.private_post,
            "/v5/order/create",
            {
                "category": "linear",
                "symbol": SYMBOL,
                "side": "Sell" if side is Side.LONG else "Buy",
                "orderType": "Market",
                "qty": str(unit),
                "reduceOnly": True,
                "positionIdx": 0,
                "orderLinkId": prefix + "exit-a",
            },
        )
        exit_id = response.get("result", {}).get("orderId")
        require(bool(exit_id), "Missing reduction acknowledgement")
        # Confirm the particular exit, not just a coincidentally matching net size.
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            history = await asyncio.to_thread(
                e._client.private_get,
                "/v5/order/history",
                {"category": "linear", "symbol": SYMBOL, "orderId": exit_id},
            )
            rows = history.get("result", {}).get("list", [])
            if any(
                r.get("orderId") == exit_id
                and r.get("orderStatus") == "Filled"
                and D(r.get("cumExecQty", "0")) == unit
                for r in rows
            ):
                break
            require(
                asyncio.get_running_loop().time() < deadline,
                "Reduction fill unverified",
            )
            await asyncio.sleep(0.3)
        account = await wait_account(
            e,
            lambda a: position(a) is not None and position(a).size == unit * 2,
            "B remainder after confirmed A exit",
        )
        # Allow asynchronous child adjustment, then take a second fresh snapshot.
        await asyncio.sleep(2)
        account = await e.account_state()
        a_stops, b_stops = [children(account, parent) for parent in parents]
        emit(
            "independent_zone_stop_observation",
            side=side.value,
            a_stop_count=len(a_stops),
            a_stop_quantity_units=sum(o.remaining_quantity for o in a_stops) / unit,
            b_stop_count=len(b_stops),
            b_stop_quantity_units=sum(o.remaining_quantity for o in b_stops) / unit,
            b_original_stop_preserved=any(
                o.order_id == before_ids[1] and o.trigger_price == stops[1]
                for o in b_stops
            ),
            net_quantity_units=position(account).size / unit
            if position(account)
            else 0,
            activation_proven=False,
        )
        if pairs is not None:
            await observe_pair_cancellation(
                e, side, parents, pairs, quantities[1], ownership
            )
    finally:
        try:
            await cleanup(e, side, prefix, ownership=ownership)
        finally:
            ownership.restore()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-demo", action="store_true")
    parser.add_argument("--zone-protection", action="store_true")
    parser.add_argument("--zone-pair-cancellation", action="store_true")
    parser.add_argument("--resume-db", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main(args))
    except Exception as exc:
        emit("suite_error", error=type(exc).__name__, message=str(exc)[:1200])
        raise SystemExit(1) from None
