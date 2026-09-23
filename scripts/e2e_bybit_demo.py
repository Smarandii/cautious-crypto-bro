"""Opt-in live Demo checks. Use run_bybit_demo_e2e.py to isolate the app.

Real planner, coordinator, SQLite, exchange and fresh-process recovery.
No Telegram sends, LLM requests or production SQLite writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

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


async def cleanup(e, side, prefix):
    for attempt in range(3):
        try:
            account = await e.account_state()
            require(
                all(
                    (
                        o.order_link_id.startswith(prefix)
                        or o.order_link_id.startswith("ccb-action-")
                        or o.is_protective
                    )
                    for o in account.open_orders
                    if o.symbol == SYMBOL
                ),
                "Foreign orders detected; refusing broad cleanup",
            )
            require(
                all(p.side is side for p in account.positions if p.symbol == SYMBOL),
                "Foreign position detected; manual cleanup required",
            )
            if position(account):
                action = PositionActionIntent(
                    source=source(999),
                    symbol=SYMBOL,
                    action=PositionActionType.CLOSE,
                    expected_side=side,
                    summary="E2E cleanup",
                    confidence=1,
                )
                await e.execute_position_action(action)
            await wait_account(e, lambda a: position(a) is None, "cleanup close")
            account = await e.account_state()
            for order in account.open_orders:
                if order.symbol == SYMBOL:
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
        store=store, executor=e, max_age_seconds=1800, execution_lock=lock
    )
    supervisor = PositionSupervisor(store=store, executor=e, mutation_lock=lock)
    prefix = f"ccb-v2-{intent.intent_id.hex[:20]}-"
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
                e._private_post,
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
        await asyncio.to_thread(
            e._private_post,
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
            trigger="test amended TP1 to marketable price",
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
        await cleanup(e, side, prefix)


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
            for side in (Side.LONG, Side.SHORT):
                await run_side(e, side, Path(directory))
        emit(
            "suite",
            result="passed",
            coverage="real exchange plus labeled application fault injection",
            not_live_tested=[
                "partial-fill timing",
                "immediate fill during installation",
                "Telegram delivery",
                "LLM accuracy",
            ],
        )
    finally:
        e.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-demo", action="store_true")
    parser.add_argument("--resume-db", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main(args))
    except Exception as exc:
        emit("suite_error", error=type(exc).__name__, message=str(exc)[:1200])
        raise SystemExit(1) from None
