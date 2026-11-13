"""Offline proof of two account-local zone lifecycles under one budget."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, state_for

from cautious_crypto_bro.bybit import BybitDemoExecutor
from cautious_crypto_bro.config import IsolatedDemoWorkerSettings, Settings
from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    IntentStatus,
    StoreError,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.isolated_demo_risk import IsolatedDemoPortfolio
from cautious_crypto_bro.isolated_demo_runtime import run_isolated_demo_workers
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class DemoAccount(Exchange):
    def __init__(self, uid):
        super().__init__()
        self.uid = uid
        self.hide_entries = False
        self.unavailable = False

    def handle(self, request):
        if request.url.path == "/v5/user/query-api":
            assert request.method == "GET"
            assert request.headers["X-BAPI-SIGN"]
            return httpx.Response(
                200, json={"retCode": 0, "result": {"userID": self.uid}}
            )
        if request.url.path == "/v5/position/list" and self.unavailable:
            return httpx.Response(200, json={"retCode": 10003, "retMsg": "unavailable"})
        if request.url.path == "/v5/order/realtime" and self.hide_entries:
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
        return super().handle(request)


@pytest.mark.parametrize("scenario", ["restart", "wrong_uid", "unavailable", "alias"])
def test_configured_demo_workers_reconcile_before_supervision(
    tmp_path, monkeypatch, scenario
):
    async def run():
        first_path = tmp_path / "first.db"
        second_path = tmp_path / "second.db"
        if scenario == "restart":
            await IntentStore(first_path).initialize_isolated_demo(101)
        first_account = DemoAccount(999 if scenario == "wrong_uid" else 101)
        first_account.unavailable = scenario == "unavailable"
        supervised = []
        first_ready = asyncio.Event()
        first_retry = asyncio.Event()
        second_ready = asyncio.Event()

        async def supervisor_run(self):
            supervised.append(self._executor)
            if self._executor is second:
                second_ready.set()
            else:
                first_ready.set()
            await asyncio.Future()

        async def wait_for_retry(seconds):
            assert seconds == 30
            first_retry.set()
            await asyncio.Future()

        with (
            executor_for(DemoAccount(303)) as primary,
            executor_for(first_account) as first,
            executor_for(DemoAccount(202)) as second,
        ):
            executors = iter((first, second))
            monkeypatch.setattr(
                "cautious_crypto_bro.isolated_demo_runtime.BybitDemoExecutor",
                lambda **kwargs: next(executors),
            )
            monkeypatch.setattr(PositionSupervisor, "run", supervisor_run)
            monkeypatch.setattr(
                "cautious_crypto_bro.isolated_demo_runtime.asyncio.sleep",
                wait_for_retry,
            )
            settings = cast(
                Settings,
                SimpleNamespace(
                    isolated_demo_workers=tuple(
                        IsolatedDemoWorkerSettings(
                            uid=uid,
                            database_path=path,
                            api_key="test",
                            api_secret="test",
                        )
                        for uid, path in (
                            (101, first_path),
                            (202, first_path if scenario == "alias" else second_path),
                        )
                    ),
                    database_path=tmp_path / "primary.db",
                    demo_portfolio_stop_risk_cap_usdt=Decimal("140"),
                    demo_entry_order_ttl_minutes=0,
                ),
            )
            if scenario == "alias":
                await run_isolated_demo_workers(settings, primary)
                assert not first_path.exists()
                assert not supervised
                return
            task = asyncio.create_task(run_isolated_demo_workers(settings, primary))
            try:
                await asyncio.wait_for(second_ready.wait(), timeout=10)
                if scenario == "unavailable":
                    await asyncio.wait_for(first_retry.wait(), timeout=10)
                if scenario == "restart":
                    await asyncio.wait_for(first_ready.wait(), timeout=10)
                    assert first in supervised
                else:
                    assert first not in supervised
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert first_path.exists() == (scenario != "wrong_uid")

    asyncio.run(run())


@pytest.mark.parametrize(
    "scenario", ["capacity", "hidden", "unprotected", "unavailable", "success"]
)
def test_isolated_zones_share_capacity_and_keep_independent_protection(
    tmp_path, scenario
):
    async def run():
        first_account = DemoAccount(101)
        sibling_account = DemoAccount(202)
        paths = (tmp_path / "first.db", tmp_path / "sibling.db")
        with (
            executor_for(first_account) as first,
            executor_for(sibling_account) as sibling,
        ):
            await BybitDemoExecutor.verify_account_isolation(
                ((101, first), (202, sibling))
            )
            first_zone = await prepare(
                paths[0],
                first_account,
                first,
                entry=Entry(type=EntryType.RANGE, range_low=94, range_high=96),
                verified_demo_uid=101,
            )
            sibling_zone = await prepare(
                paths[1],
                sibling_account,
                sibling,
                entry=Entry(type=EntryType.RANGE, range_low=97, range_high=98),
                verified_demo_uid=202,
            )
            zones = (first_zone, sibling_zone)
            assert all(
                zone[2].policy.risk_budget_usdt == Decimal("68") for zone in zones
            )
            assert (
                first_zone[2].entry_orders[0].price
                != sibling_zone[2].entry_orders[0].price
            )

            accounts = ((first, first_zone[0]), (sibling, sibling_zone[0]))
            portfolio = IsolatedDemoPortfolio(
                accounts,
                Decimal("100" if scenario in {"capacity", "hidden"} else "140"),
            )
            coordinators = [
                ExecutionCoordinator(
                    store=store,
                    executor=executor,
                    planner=ExecutionPlanner(),
                    max_age_seconds=3600,
                    isolated_demo_portfolio=portfolio,
                )
                for executor, store in accounts
            ]

            async def submit(index):
                return await coordinators[index].execute_intent(
                    zones[index][1].intent_id, approval_mode=ApprovalMode.MANUAL
                )

            if scenario == "capacity":
                accepted, outcome = await asyncio.gather(submit(0), submit(1))
            else:
                accepted = await submit(0)
                if scenario == "unprotected":
                    first_account.fill(next(iter(first_account.orders)))
                first_account.hide_entries = scenario == "hidden"
                first_account.unavailable = scenario == "unavailable"
                outcome = await submit(1)
            assert accepted.status is IntentStatus.EXECUTED
            if scenario != "success":
                reason = {
                    "capacity": "cap exceeded",
                    "hidden": "cap exceeded",
                    "unprotected": "no exchange stop",
                    "unavailable": "unavailable",
                }[scenario]
                assert outcome.status is IntentStatus.FAILED
                assert reason in outcome.message
                assert not sibling_account.orders
                assert not await sibling_zone[0].get_active_position_strategies()
                return
            assert outcome.status is IntentStatus.EXECUTED

            supervisors = [
                PositionSupervisor(
                    store=store,
                    executor=executor,
                    mutation_lock=portfolio.mutation_lock,
                )
                for executor, store in accounts
            ]

            for account, zone, supervisor in zip(
                (first_account, sibling_account), zones, supervisors, strict=True
            ):
                entries = list(account.orders)
                account.fill(entries[0])
                account.mark = account.avg
                # Exchange applies the entry order's native stop on fill.
                account.stop = zone[2].stop_loss
                await supervisor.reconcile_once()
                account.fill(entries[1])
                await supervisor.reconcile_once()
                account.mark = Decimal("120")
                account.market_fills_exits = True
                await supervisor.reconcile_once()
                assert account.size > 0 and account.stop > 0 and account.trail > 0

            for path, executor, account in zip(
                paths, (first, sibling), (first_account, sibling_account), strict=True
            ):
                protection = (account.stop, account.trail)
                reopened = IntentStore(path)
                await reopened.initialize_isolated_demo(account.uid)
                await PositionSupervisor(
                    store=reopened,
                    executor=executor,
                    mutation_lock=portfolio.mutation_lock,
                ).reconcile_once()
                state = await state_for(reopened)
                assert state.trailing_active
                assert protection == (account.stop, account.trail)

            sibling_protection = (
                sibling_account.size,
                sibling_account.stop,
                sibling_account.trail,
            )
            sibling_requests = len(sibling_account.requests)
            first_account.size = Decimal("0")
            await supervisors[0].reconcile_once()
            assert not await first_zone[0].get_active_position_strategies()
            assert sibling_protection == (
                sibling_account.size,
                sibling_account.stop,
                sibling_account.trail,
            )
            assert len(sibling_account.requests) == sibling_requests

    asyncio.run(run())


def test_isolated_store_binding_rejects_wrong_account_and_ordinary_startup(tmp_path):
    async def run():
        path = tmp_path / "isolated.db"
        await IntentStore(path).initialize_isolated_demo(101)
        await IntentStore(path).initialize_isolated_demo(101)
        with pytest.raises(StoreError, match="identity does not match"):
            await IntentStore(path).initialize_isolated_demo(202)
        with pytest.raises(RuntimeError, match="Unsupported database schema version"):
            await IntentStore(path).initialize()
        legacy = IntentStore(tmp_path / "legacy.db")
        await legacy.initialize()
        with pytest.raises(StoreError, match="fresh or bound"):
            await legacy.initialize_isolated_demo(101)
        await legacy.initialize()

    asyncio.run(run())


def test_isolated_store_first_binding_serializes_competing_accounts(tmp_path):
    async def run():
        path = tmp_path / "contended.db"
        outcomes = await asyncio.gather(
            IntentStore(path).initialize_isolated_demo(101),
            IntentStore(path).initialize_isolated_demo(202),
            return_exceptions=True,
        )
        assert outcomes.count(None) == 1
        denied = next(result for result in outcomes if result is not None)
        assert isinstance(denied, StoreError)
        winner = 101 if outcomes[0] is None else 202
        await IntentStore(path).initialize_isolated_demo(winner)

    asyncio.run(run())
