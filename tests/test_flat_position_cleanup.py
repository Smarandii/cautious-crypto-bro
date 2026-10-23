"""Restarted supervision through real SQLite and the exchange HTTP adapter."""

import asyncio
from decimal import Decimal

import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, state_for

from cautious_crypto_bro.domain import ApprovalMode, StrategyStatus
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("closing", [False, True])
@pytest.mark.parametrize("fill_during_cancel", [False, True])
def test_flat_position_cancels_only_its_entries_and_confirms_closure(
    tmp_path, closing, fill_during_cancel
):
    class ClosingExchange(Exchange):
        race_link = None

        def handle(self, request):
            if request.url.path == "/v5/order/cancel" and self.race_link is not None:
                link, self.race_link = self.race_link, None
                self.fill(link)
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = ClosingExchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            exchange.mark = Decimal("102")
            await supervisor.reconcile_once()
            state = await state_for(store)
            assert state.last_position_qty > 0
            entries = exchange.open_entries()
            assert entries
            owned_ids = {order["orderId"] for order in entries}
            foreign = dict(
                entries[0], orderId="foreign-entry", orderLinkId="foreign-entry"
            )
            exchange.orders["foreign-entry"] = foreign
            attached_stop = dict(
                entries[0],
                orderId="attached-stop",
                orderType="Market",
                side="Sell",
                stopOrderType="PartialStopLoss",
                triggerPrice="90",
                reduceOnly=False,
                parentOrderLinkId=entries[0]["orderLinkId"],
            )
            exchange.orders["attached-stop"] = attached_stop
            protective_ids = {order["orderId"] for order in exchange.exits()}
            exchange.size = Decimal("0")
            if closing:
                await store.save_position_strategy(
                    state.model_copy(update={"status": StrategyStatus.CLOSING})
                )
            if fill_during_cancel:
                exchange.race_link = entries[0]["orderLinkId"]
            baseline = len(exchange.requests)
            restarted_store = IntentStore(database)
            restarted = PositionSupervisor(
                store=restarted_store, executor=executor, mutation_lock=asyncio.Lock()
            )
            await restarted.reconcile_once()
            mutations = [
                (path, body)
                for path, body in exchange.requests[baseline:]
                if path
                in {
                    "/v5/order/cancel",
                    "/v5/order/create",
                    "/v5/order/create-batch",
                    "/v5/position/trading-stop",
                }
            ]
            assert {
                body["orderId"]
                for path, body in mutations
                if path == "/v5/order/cancel"
            } == owned_ids
            assert all(path == "/v5/order/cancel" for path, _ in mutations)
            assert foreign["orderStatus"] == "New"
            assert attached_stop["orderStatus"] == "New"
            assert all(
                order["orderStatus"] == "New"
                for order in exchange.exits()
                if order["orderId"] in protective_ids
            )
            records = await IntentStore(database).get_active_position_strategies()
            if fill_during_cancel:
                assert exchange.size > 0 and len(records) == 1
                assert records[0][0].status is not StrategyStatus.CLOSED
            else:
                assert exchange.size == 0 and not records
                before = len(exchange.requests)
                await restarted.reconcile_once()
                assert not any(
                    path == "/v5/order/cancel" for path, _ in exchange.requests[before:]
                )

    asyncio.run(run())
