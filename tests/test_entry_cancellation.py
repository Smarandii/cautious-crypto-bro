import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, make_intent, prepare, source

from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    ExecutionPolicy,
    InstrumentContext,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    StrategyStatus,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.signals.evidence import _current_post_action_evidence
from cautious_crypto_bro.storage import IntentStore


@pytest.mark.integration
def test_entry_cancellation_confirms_flat_after_late_fill(tmp_path):
    class LateFillExchange(Exchange):
        cancellation_started = False
        filled_late = False

        def handle(self, request):
            if request.url.path == "/v5/order/cancel":
                # Cancellation acknowledgement is not final order-state proof.
                self.cancellation_started = True
                order = next(
                    o
                    for o in self.orders.values()
                    if o["orderId"] == json.loads(request.content)["orderId"]
                )
                return httpx.Response(
                    200, json={"retCode": 0, "result": {"orderId": order["orderId"]}}
                )
            if (
                request.url.path == "/v5/order/realtime"
                and self.cancellation_started
                and not self.filled_late
            ):
                self.filled_late = True
                for order in self.open_entries():
                    self.fill(order["orderLinkId"])
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        exchange = LateFillExchange()
        with executor_for(exchange) as executor:
            store = IntentStore(database)
            await store.initialize()
            intent = make_intent().model_copy(
                update={"entry": Entry(type=EntryType.LIMIT, price=100)}
            )
            planner = ExecutionPlanner()
            plan = planner.plan(
                intent,
                ExecutionPolicy(trading_capital_usdt=Decimal("6800")),
                InstrumentContext(
                    Decimal("100"),
                    Decimal(".1"),
                    Decimal(".001"),
                    Decimal(".001"),
                    Decimal("5"),
                ),
            )
            claim = await store.claim_source(intent.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source(
                ((intent, plan),), (), claim
            )
            coordinator = ExecutionCoordinator(
                store=store, executor=executor, planner=planner, max_age_seconds=3600
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED and exchange.size == 0
            action = PositionActionIntent(
                source=source("Cancel BTC orders", 20),
                symbol="BTCUSDT",
                action=PositionActionType.CANCEL_ENTRIES,
                summary="Cancel",
                confidence=1,
            )
            claim = await store.claim_source(action.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source((), (action,), claim)
            outcome = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
            assert (
                exchange.filled_late
                and exchange.size > 0
                and not exchange.open_entries()
            )
            active = await IntentStore(database).get_active_position_strategies()
            assert len(active) == 1, "Cancellation orphaned a late-filled position"
            assert active[0][0].status is not StrategyStatus.CLOSED
            assert active[0][0].entry_frozen and active[0][0].rebalance_needed
            supervisor = PositionSupervisor(
                store=IntentStore(database),
                executor=executor,
                mutation_lock=coordinator._execution_lock,
            )
            await supervisor.reconcile_once()
            assert exchange.stop == plan.stop_loss
            assert exchange.exits()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text,symbol,accepted",
    [
        (
            "INJ улетела вверх без нас. Отменяем лимитку. Лимитка по AVAX стоит.",
            "INJUSDT",
            True,
        ),
        (
            "INJ улетела вверх без нас. Отменяем лимитку. Лимитка по AVAX стоит.",
            "AVAXUSDT",
            False,
        ),
        ("Keep AVAX orders. Cancel INJ orders.", "AVAXUSDT", False),
        ("Keep AVAX orders. Cancel INJ orders.", "INJUSDT", True),
        ("Cancel PEPE orders.", "1000PEPEUSDT", True),
        ("Cancel 1000PEPE orders.", "1000PEPEUSDT", True),
        ("Cancel 1000PEPEUSDT orders.", "1000PEPEUSDT", True),
        ("Cancel 1000SHIB orders.", "1000PEPEUSDT", False),
        ("Keep PEPE orders. Cancel INJ orders.", "1000PEPEUSDT", False),
        ("Cancel PEPE orders.", "1000SHIBUSDT", False),
        ("Do not cancel INJ orders.", "INJUSDT", False),
        ("INJ не отменяем лимитку.", "INJUSDT", False),
        ("INJ держим.", "INJUSDT", False),
    ],
)
def test_cancel_evidence(text, symbol, accepted):
    assert (
        bool(
            _current_post_action_evidence(
                source(text), PositionActionType.CANCEL_ENTRIES, symbol, "cancel orders"
            )
        )
        is accepted
    )


@pytest.mark.parametrize("timeout", [False, True])
def test_cancellation_scopes_orders_and_pending_approvals(tmp_path, timeout):
    async def run():
        with executor_for(Exchange()) as executor:
            store, intent, plan, coordinator, _ = await prepare(
                tmp_path / "state.db", None, executor
            )
            others = []
            for channel, later in [(99, False), (0, True)]:
                item = intent.model_copy(
                    update={
                        "intent_id": uuid4(),
                        "source": intent.source.model_copy(
                            update={
                                "channel_id": channel,
                                "message_id": len(others) + 10,
                                "published_at": intent.source.published_at
                                + timedelta(hours=1 if later else 0),
                            }
                        ),
                    }
                )
                claim = await store.claim_source(item.source, lease_seconds=300)
                await store.create_signal_batch_and_complete_source(
                    ((item, plan.model_copy(update={"intent_id": item.intent_id})),),
                    (),
                    claim,
                )
                others.append(item)
            action = PositionActionIntent(
                source=source("Cancel BTC orders", 20),
                symbol="BTCUSDT",
                action=PositionActionType.CANCEL_ENTRIES,
                summary="Cancel",
                confidence=1,
            )
            claim = await store.claim_source(action.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source((), (action,), claim)

            def order(item, suffix="e1", **kw):
                return SimpleNamespace(
                    symbol="BTCUSDT",
                    order_id=str(uuid4()),
                    order_link_id=f"ccb-v2-{item.intent_id.hex[:20]}-{suffix}",
                    reduce_only=False,
                    is_protective=False,
                    **kw,
                )

            own = order(intent)
            protection = order(intent, "t1r1")
            protection.reduce_only = True
            orders = [own, protection, *(order(i) for i in others)]
            remaining = orders.copy()

            async def cancel(symbol, ident):
                remaining[:] = [o for o in remaining if o.order_id != ident]
                if timeout:
                    raise TimeoutError("accepted but response lost")

            fake = SimpleNamespace(
                account_state=AsyncMock(
                    side_effect=lambda: SimpleNamespace(
                        open_orders=tuple(remaining),
                        positions=(SimpleNamespace(symbol="BTCUSDT"),),
                    )
                ),
                cancel_order=AsyncMock(side_effect=cancel),
            )
            coordinator._executor = fake
            # Simulate an entry already claimed but waiting for the account lock.
            await store.claim_for_execution(
                intent.intent_id, None, expected_approval_mode=ApprovalMode.MANUAL
            )
            result = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert result.status is (
                IntentStatus.UNCERTAIN if timeout else IntentStatus.EXECUTED
            )
            fake.cancel_order.assert_awaited_once_with("BTCUSDT", own.order_id)
            assert remaining == orders[1:]
            assert (
                await store.get_intent(intent.intent_id)
            ).status is IntentStatus.SKIPPED
            for item in others:
                assert (
                    await store.get_intent(item.intent_id)
                ).status is IntentStatus.PENDING
            repeat = await coordinator.execute_position_action(
                action.action_id, approval_mode=ApprovalMode.MANUAL
            )
            assert repeat.status is result.status
            assert fake.cancel_order.await_count == 1

    asyncio.run(run())


def test_restart_preserves_other_source_strategy(tmp_path):
    async def run():
        with executor_for(Exchange()) as executor:
            store, intent, plan, _, _ = await prepare(
                tmp_path / "restart.db", None, executor
            )
            await store.ensure_position_strategy(plan)
            before = await store.get_active_position_strategies()
            action = PositionActionIntent(
                source=source("Cancel BTC orders", 20).model_copy(
                    update={"channel_id": 99}
                ),
                symbol="BTCUSDT",
                action=PositionActionType.CANCEL_ENTRIES,
                summary="Cancel",
                confidence=1,
            )
            claim = await store.claim_source(action.source, lease_seconds=300)
            await store.create_signal_batch_and_complete_source((), (action,), claim)
            await store.claim_position_action_for_execution(
                action.action_id, None, expected_approval_mode=ApprovalMode.MANUAL
            )
            await store.quarantine_interrupted_executions()
            assert (
                await store.get_position_action(action.action_id)
            ).status is IntentStatus.UNCERTAIN
            assert await store.get_active_position_strategies() == before

    asyncio.run(run())
