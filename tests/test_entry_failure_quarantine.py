"""Entry failure and restart preserve a single durable execution disposition."""

import asyncio
import sqlite3
from contextlib import closing
from decimal import Decimal

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    OpenRelation,
    StrategyStatus,
)
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class FailedRefresh(Exchange):
    fail_refresh = True

    def handle(self, request):
        if self.fail_refresh and self.size and request.url.path == "/v5/market/tickers":
            raise httpx.ReadTimeout(
                "Synthetic post-fill refresh failure", request=request
            )
        return super().handle(request)


@pytest.mark.parametrize("mode", [ApprovalMode.MANUAL, ApprovalMode.AUTO])
@pytest.mark.parametrize("failed_table", [None, "intents", "position_strategies"])
def test_entry_failure_preserves_restart_warning_and_protection(
    tmp_path, mode, failed_table
):
    async def run():
        database = tmp_path / "state.db"
        exchange = FailedRefresh()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, _ = await prepare(
                database, exchange, executor, mode=mode, relation=OpenRelation.NEW
            )
            if failed_table:
                status = "FAILED" if failed_table == "intents" else "UNCERTAIN"
                with closing(sqlite3.connect(database)) as db:
                    db.execute(
                        f"CREATE TRIGGER reject_disposition BEFORE UPDATE ON {failed_table} "
                        f"WHEN NEW.status='{status}' "
                        "BEGIN SELECT RAISE(ABORT, 'synthetic disposition failure'); END"
                    )
                    db.commit()
                with pytest.raises(sqlite3.IntegrityError, match="disposition failure"):
                    await coordinator.execute_intent(
                        intent.intent_id, approval_mode=mode
                    )
            else:
                outcome = await coordinator.execute_intent(
                    intent.intent_id, approval_mode=mode
                )
                assert outcome.status is IntentStatus.FAILED

            assert exchange.size > 0, "Failure must follow an actual primary fill"
            reopened = IntentStore(database)
            saved_intent = await reopened.get_intent(intent.intent_id)
            records = await reopened.get_active_position_strategies()
            assert len(records) == 1
            state, _ = records[0]
            assert saved_intent.status is (
                IntentStatus.EXECUTING if failed_table else IntentStatus.FAILED
            )
            assert state.status is (
                StrategyStatus.ENTERING if failed_table else StrategyStatus.UNCERTAIN
            )
            protection = (exchange.size, exchange.stop, exchange.trail)
            before = len(exchange.requests)
            entries_before = {
                link
                for link, order in exchange.orders.items()
                if not order["reduceOnly"]
            }

            if failed_table:
                with closing(sqlite3.connect(database)) as db:
                    db.execute("DROP TRIGGER reject_disposition")
                    db.commit()
            interrupted, actions = await reopened.quarantine_interrupted_executions()
            assert actions == ()
            assert interrupted == ((intent.intent_id,) if failed_table else ())
            assert (await reopened.get_intent(intent.intent_id)).status is (
                IntentStatus.UNCERTAIN if failed_table else IntentStatus.FAILED
            )
            records = await reopened.get_active_position_strategies()
            assert records[0][0].status is (
                StrategyStatus.ENTERING if failed_table else StrategyStatus.UNCERTAIN
            )
            assert not await reopened.get_pending_auto_intent_ids()
            assert await reopened.quarantine_interrupted_executions() == ((), ())

            exchange.fail_refresh = False
            restarted = PositionSupervisor(
                store=reopened, executor=executor, mutation_lock=asyncio.Lock()
            )
            await restarted.reconcile_once()
            requests_after_protection = len(exchange.requests)
            await coordinator.execute_intent(intent.intent_id, approval_mode=mode)
            assert len(exchange.requests) == requests_after_protection
            assert {
                link
                for link, order in exchange.orders.items()
                if not order["reduceOnly"]
            } == entries_before
            if failed_table:
                # Interrupted OPENs must still protect verified fills, never reopen them.
                exits = [
                    order for order in exchange.orders.values() if order["reduceOnly"]
                ]
                assert len(exits) == 3
                assert sum(Decimal(order["qty"]) for order in exits) <= exchange.size
                assert exchange.size == protection[0] and exchange.stop >= protection[1]
                assert all(
                    body["reduceOnly"]
                    for path, body in exchange.requests[before:]
                    if path == "/v5/order/create"
                )
            else:
                assert all(
                    path
                    in {
                        "/v5/market/time",
                        "/v5/order/realtime",
                        "/v5/order/history",
                        "/v5/execution/list",
                    }
                    for path, _ in exchange.requests[before:]
                )
                assert (exchange.size, exchange.stop, exchange.trail) == protection

    asyncio.run(run())
