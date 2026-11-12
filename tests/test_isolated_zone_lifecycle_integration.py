"""Offline proof of two account-local zone lifecycles under one budget."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, state_for

from cautious_crypto_bro.bybit import BybitDemoExecutor
from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    IntentStatus,
    StoreError,
)
from cautious_crypto_bro.portfolio_risk import (
    PortfolioRiskUnavailable,
    open_stop_risk_usdt,
)
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class DemoAccount(Exchange):
    def __init__(self, uid):
        super().__init__()
        self.uid = uid

    def handle(self, request):
        if request.url.path == "/v5/user/query-api":
            assert request.method == "GET"
            assert request.headers["X-BAPI-SIGN"]
            return httpx.Response(
                200, json={"retCode": 0, "result": {"userID": self.uid}}
            )
        return super().handle(request)


def test_isolated_zones_share_capacity_and_keep_independent_protection(tmp_path):
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

            # The simulator owns admission until production routing carries account identity.
            admission_lock = asyncio.Lock()

            async def submit(zone, cap):
                async with admission_lock:
                    total = Decimal("0")
                    for executor, (store, *_) in zip(
                        (first, sibling), zones, strict=True
                    ):
                        total += open_stop_risk_usdt(
                            await executor.account_state(),
                            await store.get_active_position_strategies(),
                        )
                    _, intent, plan, coordinator, _ = zone
                    if total + plan.planned_max_loss_usdt > cap:
                        return None
                    return await coordinator.execute_intent(
                        intent.intent_id, approval_mode=ApprovalMode.MANUAL
                    )

            accepted, denied = await asyncio.gather(
                submit(first_zone, Decimal("100")),
                submit(sibling_zone, Decimal("100")),
            )
            assert accepted.status is IntentStatus.EXECUTED
            assert denied is None
            assert not sibling_account.orders
            assert (
                await sibling_zone[0].get_intent(sibling_zone[1].intent_id)
            ).status is IntentStatus.PENDING
            first_entries = list(first_account.orders)
            first_account.fill(first_entries[0])
            with pytest.raises(PortfolioRiskUnavailable, match="no exchange stop"):
                await submit(sibling_zone, Decimal("140"))
            assert not sibling_account.orders
            first_account.stop = first_zone[2].stop_loss
            assert (
                await submit(sibling_zone, Decimal("140"))
            ).status is IntentStatus.EXECUTED

            for account, zone in zip(
                (first_account, sibling_account), zones, strict=True
            ):
                entries = list(account.orders)
                if not account.size:
                    account.fill(entries[0])
                account.mark = account.avg
                # Exchange applies the entry order's native stop on fill.
                account.stop = zone[2].stop_loss
                await zone[4].reconcile_once()
                account.fill(entries[1])
                await zone[4].reconcile_once()
                account.mark = Decimal("120")
                account.market_fills_exits = True
                await zone[4].reconcile_once()
                assert account.size > 0 and account.stop > 0 and account.trail > 0

            for path, executor, account in zip(
                paths, (first, sibling), (first_account, sibling_account), strict=True
            ):
                protection = (account.stop, account.trail)
                reopened = IntentStore(path)
                await reopened.initialize_isolated_demo(account.uid)
                await PositionSupervisor(
                    store=reopened, executor=executor, mutation_lock=asyncio.Lock()
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
            await first_zone[4].reconcile_once()
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
