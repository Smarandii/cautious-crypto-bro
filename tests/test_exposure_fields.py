"""Invalid exchange quantities cannot masquerade as absent account exposure."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare, source

from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    Side,
    TradeExecutionError,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator

pytestmark = pytest.mark.integration
MISSING = object()


@pytest.mark.parametrize("position_idx", [0, 1, 2])
@pytest.mark.parametrize("size", ["0", "1"])
def test_hedge_mode_snapshots_fail_before_position_actions(position_idx, size):
    class PositionModeExchange(ExposureFieldsExchange):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == "/v5/position/list":
                data = response.json()
                for row in data["result"]["list"]:
                    row["size"] = size
                return httpx.Response(200, json=data)
            return response

    async def run():
        exchange = PositionModeExchange("position", "positionIdx", position_idx)
        action = PositionActionIntent(
            source=source("Close BTCUSDT"),
            symbol="BTCUSDT",
            action=PositionActionType.CLOSE,
            expected_side=Side.LONG,
            summary="Close position",
            confidence=1,
        )
        with executor_for(exchange) as executor:
            if position_idx == 0:
                account = await executor.account_state()
                assert len(account.positions) == int(size)
            else:
                with pytest.raises(TradeExecutionError, match="one-way position mode"):
                    await executor.account_state()
                with pytest.raises(TradeExecutionError, match="one-way position mode"):
                    await executor.execute_position_action(action)
        assert not exchange.orders
        assert not any(
            path
            in {"/v5/order/cancel", "/v5/order/create", "/v5/position/trading-stop"}
            for path, _ in exchange.requests
        )

    asyncio.run(run())


class ExposureFieldsExchange(Exchange):
    def __init__(self, kind, field, value):
        super().__init__()
        self.kind, self.field, self.value = kind, field, value

    def handle(self, request):
        positions = (
            self.kind in {"position", "portfolio"}
            and request.url.path == "/v5/position/list"
        )
        orders = (
            self.kind in {"order", "entry"}
            and request.url.path == "/v5/order/realtime"
            and (self.kind == "entry" or "settleCoin" in request.url.params)
        )
        if positions or orders:
            row = {
                "symbol": "ETHUSDT" if self.kind == "portfolio" else "BTCUSDT",
                "side": "Buy" if self.side is Side.LONG else "Sell",
                "size": "10" if self.kind == "portfolio" else "1",
                "avgPrice": "100",
                "markPrice": "100",
                "stopLoss": "90",
                "qty": "1",
                "leavesQty": "1",
                "price": "100",
                "orderId": "manual",
                "orderLinkId": "ccb-synthetic" if self.kind == "entry" else "",
                "orderType": "Limit",
                "orderStatus": "New",
                "createdTime": "1",
                "updatedTime": "1",
            }
            if self.value is MISSING:
                row.pop(self.field)
            else:
                row[self.field] = self.value
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"list": [row], "nextPageCursor": ""}},
            )
        return super().handle(request)


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("position", "size", None),
        ("position", "size", ""),
        ("position", "size", "-1"),
        ("position", "size", "NaN"),
        ("position", "size", "Infinity"),
        ("position", "symbol", ""),
        ("position", "avgPrice", "Infinity"),
        ("position", "markPrice", "NaN"),
        pytest.param("position", "size", MISSING, id="missing-position-size"),
        ("order", "leavesQty", None),
        ("order", "leavesQty", ""),
        ("order", "leavesQty", "-1"),
        ("order", "leavesQty", "Infinity"),
        ("order", "leavesQty", "not-a-number"),
        ("order", "symbol", ""),
        ("order", "qty", None),
        ("order", "qty", "0"),
        pytest.param("order", "leavesQty", MISSING, id="missing-order-remaining"),
    ],
)
def test_invalid_exposure_fields_fail_before_auto_entry(tmp_path, kind, field, value):
    async def run():
        exchange = ExposureFieldsExchange(kind, field, value)
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, _ = await prepare(
                tmp_path / "state.db",
                exchange,
                executor,
                mode=ApprovalMode.AUTO,
                relation=OpenRelation.NEW,
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.AUTO
            )
            assert outcome.status is IntentStatus.FAILED
            assert (
                "TradeExecutionError" in outcome.message and "Bybit" in outcome.message
            )
            assert (
                await store.get_intent(intent.intent_id)
            ).status is IntentStatus.FAILED
            assert await store.get_active_position_strategies() == ()
            assert not exchange.orders
            assert not any(
                path
                in {
                    "/v5/position/set-leverage",
                    "/v5/order/create",
                    "/v5/order/create-batch",
                }
                for path, _ in exchange.requests
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "side,stop", [(Side.LONG, "Infinity"), (Side.SHORT, "-1"), (Side.LONG, "90")]
)
def test_invalid_stop_cannot_bypass_portfolio_cap(tmp_path, side, stop):
    async def run():
        exchange = ExposureFieldsExchange("portfolio", "stopLoss", stop)
        exchange.side = side
        with executor_for(exchange) as executor:
            store, intent, _, _, _ = await prepare(
                tmp_path / "state.db",
                exchange,
                executor,
                mode=ApprovalMode.AUTO,
                relation=OpenRelation.NEW,
            )
            coordinator = ExecutionCoordinator(
                store=store,
                executor=executor,
                planner=ExecutionPlanner(),
                max_age_seconds=3600,
                portfolio_stop_risk_cap_usdt=Decimal("100"),
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.AUTO
            )
            assert outcome.status is IntentStatus.FAILED
            if stop == "90":
                assert "Portfolio stop-risk cap exceeded" in outcome.message
            else:
                assert "TradeExecutionError" in outcome.message
            assert not exchange.orders
            assert await store.get_active_position_strategies() == ()

    asyncio.run(run())


@pytest.mark.parametrize("kind,field", [("position", "size"), ("order", "leavesQty")])
def test_explicit_zero_exposure_still_allows_auto_entry(tmp_path, kind, field):
    async def run():
        exchange = ExposureFieldsExchange(kind, field, "0")
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, _ = await prepare(
                tmp_path / "state.db",
                exchange,
                executor,
                mode=ApprovalMode.AUTO,
                relation=OpenRelation.NEW,
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.AUTO
            )
            assert outcome.status is IntentStatus.EXECUTED
            assert (
                await store.get_intent(intent.intent_id)
            ).status is IntentStatus.EXECUTED
            assert exchange.orders

    asyncio.run(run())


@pytest.mark.parametrize("kind,field", [("position", "size"), ("entry", "leavesQty")])
def test_invalid_action_exposure_fails_before_canceling_or_closing(kind, field):
    async def run():
        exchange = ExposureFieldsExchange(kind, field, None)
        exchange.size = Decimal("1")
        action = PositionActionIntent(
            source=source("Close BTCUSDT"),
            symbol="BTCUSDT",
            action=PositionActionType.CLOSE,
            expected_side=Side.LONG,
            summary="Close position",
            confidence=1,
        )
        with executor_for(exchange) as executor:
            with pytest.raises(TradeExecutionError, match="Bybit.*(size|leavesQty)"):
                await executor.execute_position_action(action)
        assert not any(
            path in {"/v5/order/cancel", "/v5/order/create", "/v5/order/create-batch"}
            for path, _ in exchange.requests
        )

    asyncio.run(run())
