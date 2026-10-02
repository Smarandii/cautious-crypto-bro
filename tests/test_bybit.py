import json
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from cautious_crypto_bro.bybit import BybitClient, BybitDemoExecutor
from cautious_crypto_bro.domain import (
    Entry,
    EntryPreflightError,
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    InstrumentContext,
    PlannedOrder,
    Side,
    SourceMessage,
    TradeExecutionError,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner


def policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        trading_capital_usdt=(Decimal("6800")),
        risk_per_trade_pct=(Decimal("1")),
        range_order_count=3,
    )


def range_plan() -> ExecutionPlan:
    return ExecutionPlan(
        intent_id=("11111111-1111-1111-1111-111111111111"),
        symbol="BTCUSDT",
        side=Side.LONG,
        orders=(
            PlannedOrder(
                order_type=(ExecutionOrderType.LIMIT),
                quantity=Decimal("0.1"),
                price=Decimal("100"),
                reference_price=(Decimal("100")),
            ),
            PlannedOrder(
                order_type=(ExecutionOrderType.LIMIT),
                quantity=Decimal("0.1"),
                price=Decimal("105"),
                reference_price=(Decimal("105")),
            ),
            PlannedOrder(
                order_type=(ExecutionOrderType.LIMIT),
                quantity=Decimal("0.1"),
                price=Decimal("110"),
                reference_price=(Decimal("110")),
            ),
        ),
        stop_loss=Decimal("90"),
        take_profit=Decimal("130"),
        policy=policy(),
        planned_max_loss_usdt=(Decimal("4.5")),
        created_at=datetime.now(UTC),
    )


def v2_range_plan(
    side: Side = Side.LONG, entry_type: EntryType = EntryType.RANGE
) -> ExecutionPlan:
    now = datetime.now(UTC)
    return ExecutionPlanner().plan(
        TradingIntent(
            source=SourceMessage(
                channel_id=1,
                channel_title="Test",
                message_id=1,
                published_at=now,
                received_at=now,
                text="test",
            ),
            symbol="BTCUSDT",
            side=side,
            entry=(
                Entry(type=EntryType.RANGE, range_low=100, range_high=110)
                if entry_type is EntryType.RANGE
                else Entry(
                    type=EntryType.LIMIT, price=110 if side is Side.LONG else 100
                )
            ),
            stop_loss=90 if side is Side.LONG else 120,
            take_profit=None,
            summary="test",
            confidence=1,
        ),
        policy(),
        InstrumentContext(
            market_price=Decimal("120" if side is Side.LONG else "90"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )


def v2_market_plan() -> ExecutionPlan:
    now = datetime.now(UTC)
    return ExecutionPlanner().plan(
        TradingIntent(
            source=SourceMessage(
                channel_id=1,
                channel_title="Test",
                message_id=2,
                published_at=now,
                received_at=now,
                text="test",
            ),
            symbol="BTCUSDT",
            side=Side.LONG,
            entry=Entry(type=EntryType.MARKET),
            stop_loss=90,
            take_profit=None,
            summary="test",
            confidence=1,
        ),
        policy(),
        InstrumentContext(
            market_price=Decimal("120"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )


def test_clock_sync_compensates_for_local_drift() -> None:
    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        assert request.url.path == "/v5/market/time"

        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "retMsg": "OK",
                "time": 119_050,
            },
        )

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    executor._client.close()

    executor._client = BybitClient(
        base_url=("https://api-demo.bybit.com"),
        api_key="key",
        api_secret="secret",
    )

    executor._client._http_client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=(httpx.MockTransport(handler)),
    )

    try:
        with patch(
            ("cautious_crypto_bro.bybit.auth._wall_clock_ms"),
            side_effect=[
                100_000,
                100_100,
            ],
        ):
            executor._sync_clock(force=True)

        assert executor._client._auth._clock_offset_ms == 19_000

    finally:
        executor.close()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("entry_type", [EntryType.LIMIT, EntryType.RANGE])
@pytest.mark.parametrize("resting_distance", ["0", "0.1", "50"])
@pytest.mark.parametrize("leverage", ["10", "25"])
def test_priced_entries_recheck_market_without_chasing(
    side: Side, entry_type: EntryType, resting_distance: str, leverage: str
) -> None:
    plan = v2_range_plan(side, entry_type).model_copy(
        update={"leverage": Decimal(leverage)}
    )
    market_price = plan.orders[0].reference_price + Decimal(resting_distance) * (
        1 if side is Side.LONG else -1
    )
    calls: list[str] = []

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v5/market/tickers":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"list": [{"lastPrice": str(market_price)}]},
                },
            )
        if request.url.path == "/v5/position/set-leverage":
            assert json.loads(request.content) == {
                "category": "linear",
                "symbol": plan.symbol,
                "buyLeverage": leverage,
                "sellLeverage": leverage,
            }
            return httpx.Response(200, json={"retCode": 0, "result": {}})
        assert request.url.path == "/v5/order/create-batch"

        body = request.read().decode()

        assert body.count('"tpslMode":"Partial"') == 3
        assert '"takeProfit"' not in body
        assert [Decimal(item["price"]) for item in json.loads(body)["request"]] == [
            order.price for order in plan.orders
        ]

        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {"orderId": "a"},
                        {"orderId": "b"},
                        {"orderId": "c"},
                    ]
                },
                "retExtInfo": {
                    "list": [
                        {
                            "code": 0,
                            "msg": "OK",
                        },
                        {
                            "code": 0,
                            "msg": "OK",
                        },
                        {
                            "code": 0,
                            "msg": "OK",
                        },
                    ]
                },
            },
        )

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    executor._client.close()

    executor._client = BybitClient(
        base_url="https://api-demo.bybit.com", api_key="key", api_secret="secret"
    )

    executor._client._http_client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=(httpx.MockTransport(handler)),
    )

    try:
        with patch.object(
            executor._client._auth,
            "sync_clock",
        ):
            order_ids = executor._execute_sync(plan)

        assert order_ids == (
            "a",
            "b",
            "c",
        )
        assert calls == [
            "/v5/market/tickers",
            "/v5/position/set-leverage",
            "/v5/order/create-batch",
        ]

    finally:
        executor.close()


def test_market_execution_rejects_risk_above_budget() -> None:
    executor = BybitDemoExecutor(api_key="key", api_secret="secret")

    try:
        with pytest.raises(TradeExecutionError, match="risk budget"):
            executor._validate_market_plan(
                v2_market_plan(),
                Decimal("200"),
            )
    finally:
        executor.close()


def test_historical_v1_cannot_reach_order_submission() -> None:
    executor = BybitDemoExecutor(api_key="key", api_secret="secret")
    legacy_plan = range_plan()

    try:
        with patch.object(
            executor._client._auth,
            "sync_clock",
            side_effect=AssertionError("V1 must not make exchange calls"),
        ):
            with pytest.raises(TradeExecutionError, match="read-only"):
                executor._execute_sync(legacy_plan)

        with pytest.raises(TradeExecutionError, match="read-only"):
            executor._order_params(legacy_plan, 0)

        with pytest.raises(TradeExecutionError, match="read-only"):
            executor._validate_market_plan(legacy_plan, Decimal("105"))

        with pytest.raises(TradeExecutionError, match="read-only"):
            executor._execute_market_primary_sync(legacy_plan)
    finally:
        executor.close()


def test_exposure_reads_position_and_pending_ccb_orders() -> None:
    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        if request.url.path == "/v5/position/list":
            assert request.url.params["symbol"] == "BTCUSDT"

            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "positionIdx": 0,
                                "side": "Buy",
                                "size": "0.25",
                                "avgPrice": "60000",
                            }
                        ]
                    },
                },
            )

        if request.url.path == "/v5/order/realtime":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "list": [
                            {
                                "orderId": "ccb-order",
                                "orderLinkId": ("ccb-abc-1"),
                                "side": "Sell",
                                "price": "62000",
                                "leavesQty": "0.1",
                                "reduceOnly": False,
                            },
                            {
                                "orderId": "manual",
                                "orderLinkId": "",
                                "side": "Buy",
                                "price": "59000",
                                "leavesQty": "5",
                                "reduceOnly": False,
                            },
                            {
                                "orderId": "reduce",
                                "orderLinkId": ("ccb-reduce-1"),
                                "side": "Sell",
                                "price": "65000",
                                "leavesQty": "0.2",
                                "reduceOnly": True,
                            },
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        raise AssertionError(f"Unexpected request: {request.url}")

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    executor._client.close()

    executor._client = BybitClient(
        base_url=("https://api-demo.bybit.com"),
        api_key="key",
        api_secret="secret",
    )

    executor._client._http_client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=httpx.MockTransport(handler),
    )

    try:
        with patch.object(
            executor._client._auth,
            "sync_clock",
        ):
            exposure = executor._exposure_sync("BTCUSDT")

        assert len(exposure.positions) == 1

        position = exposure.positions[0]

        assert position.side is Side.LONG
        assert position.size == Decimal("0.25")
        assert position.avg_price == Decimal("60000")

        assert len(exposure.pending_entry_orders) == 1

        pending = exposure.pending_entry_orders[0]

        assert pending.side is Side.SHORT
        assert pending.remaining_quantity == Decimal("0.1")
        assert pending.order_id == "ccb-order"

    finally:
        executor.close()


def test_account_state_keeps_active_partial_stops_with_zero_leaves_qty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/position/list":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "Buy",
                                "size": "4",
                                "avgPrice": "100",
                                "markPrice": "102",
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )
        if request.url.path == "/v5/order/realtime":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "Sell",
                                "orderType": "Market",
                                "orderStatus": "Untriggered",
                                "orderId": "partial-sl",
                                "orderLinkId": "",
                                "parentOrderLinkId": "ccb-v2-01234567890123456789-e1",
                                "qty": "4",
                                "leavesQty": "0",
                                "stopOrderType": "PartialStopLoss",
                                "triggerPrice": "90",
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    executor = BybitDemoExecutor(api_key="key", api_secret="secret")
    executor._client.close()
    executor._client._http_client = httpx.Client(
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        with patch.object(executor._client._auth, "sync_clock"):
            state = executor._account_state_sync()
        assert len(state.open_orders) == 1
        assert state.open_orders[0].remaining_quantity == Decimal("4")
        assert state.open_orders[0].stop_order_type == "PartialStopLoss"
        assert (
            state.open_orders[0].parent_order_link_id
            == "ccb-v2-01234567890123456789-e1"
        )
    finally:
        executor.close()


def test_v2_market_direct_batch_execution_is_rejected() -> None:
    plan = v2_market_plan()

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    try:
        with patch.object(
            executor._client._auth,
            "sync_clock",
        ):
            try:
                executor._execute_sync(plan)
            except TradeExecutionError as exc:
                assert "staged E1" in str(exc)
            else:
                raise AssertionError(
                    "Expected direct V2 MARKET batch execution rejection"
                )

    finally:
        executor.close()


@pytest.mark.parametrize("leverage_code", [0, 110043, "110043"])
def test_v2_market_primary_fill_precedes_scale_ins(leverage_code) -> None:
    plan = v2_market_plan().model_copy(update={"leverage": Decimal("7.5")})

    calls: list[str] = []
    batch_request = None

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal batch_request

        calls.append(request.url.path)

        if request.url.path == "/v5/market/tickers":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "lastPrice": "120",
                            }
                        ]
                    },
                },
            )

        if request.url.path == "/v5/position/set-leverage":
            assert json.loads(request.content) == {
                "category": "linear",
                "symbol": plan.symbol,
                "buyLeverage": "7.5",
                "sellLeverage": "7.5",
            }
            return httpx.Response(200, json={"retCode": leverage_code, "result": {}})

        if request.url.path == "/v5/order/create":
            body = json.loads(request.read().decode())

            assert body["orderType"] == "Market"
            assert "takeProfit" not in body
            assert body["stopLoss"] == "90"
            assert body["tpslMode"] == "Partial"

            assert body["orderLinkId"].endswith("-e1")

            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "orderId": "e1-live",
                    },
                },
            )

        if request.url.path == "/v5/order/realtime":
            assert request.url.params["orderId"] == "e1-live"

            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "orderId": ("e1-live"),
                                "orderStatus": ("Filled"),
                                "avgPrice": "121",
                                "cumExecQty": str(plan.orders[0].quantity),
                                "leavesQty": "0",
                            }
                        ]
                    },
                },
            )

        if request.url.path == "/v5/order/history":
            raise AssertionError("History fallback should not be needed")

        if request.url.path == "/v5/order/create-batch":
            batch_request = json.loads(request.read().decode())

            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {"orderId": "e2-live"},
                            {"orderId": "e3-live"},
                        ]
                    },
                    "retExtInfo": {
                        "list": [
                            {
                                "code": 0,
                                "msg": "OK",
                            },
                            {
                                "code": 0,
                                "msg": "OK",
                            },
                        ]
                    },
                },
            )

        raise AssertionError(f"Unexpected request: {request.url}")

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    executor._client.close()

    executor._client = BybitClient(
        base_url=("https://api-demo.bybit.com"),
        api_key="key",
        api_secret="secret",
    )

    executor._client._http_client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=httpx.MockTransport(handler),
    )

    try:
        with patch.object(
            executor._client._auth,
            "sync_clock",
        ):
            primary = executor._execute_market_primary_sync(plan)

            rebased = ExecutionPlanner().rebase_market_plan(
                plan,
                fill_price=(primary.average_fill_price),
                filled_quantity=(primary.filled_quantity),
                context=InstrumentContext(
                    market_price=Decimal("121"),
                    tick_size=Decimal("0.1"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    min_notional=Decimal("5"),
                ),
            )

            remaining = executor._execute_remaining_entries_sync(rebased)

        assert primary.order_id == "e1-live"

        assert primary.average_fill_price == Decimal("121")
        assert rebased.leverage == Decimal("7.5")

        assert remaining == (
            "e2-live",
            "e3-live",
        )

        assert batch_request is not None

        requests = batch_request["request"]

        assert len(requests) == 2
        assert all(item["orderType"] == "Limit" for item in requests)
        assert all("takeProfit" not in item for item in requests)
        assert all(item["stopLoss"] == "90" for item in requests)
        assert all(item["tpslMode"] == "Partial" for item in requests)

        assert [
            item["orderLinkId"].rsplit(
                "-",
                1,
            )[-1]
            for item in requests
        ] == [
            "e2",
            "e3",
        ]

        assert [item["price"] for item in requests] == [
            "110.8",
            "100.5",
        ]

        assert calls.index("/v5/order/realtime") < calls.index("/v5/order/create-batch")
        assert calls.index("/v5/position/set-leverage") < calls.index(
            "/v5/order/create"
        )
        assert calls.count("/v5/position/set-leverage") == 1

    finally:
        executor.close()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("entry_type", [EntryType.LIMIT, EntryType.RANGE])
def test_priced_entry_passed_since_planning_rejects_before_submission(
    side: Side, entry_type: EntryType
) -> None:
    plan = v2_range_plan(side, entry_type)
    market_price = plan.orders[0].reference_price + Decimal("0.1") * (
        -1 if side is Side.LONG else 1
    )
    executor = BybitDemoExecutor(api_key="key", api_secret="secret")
    try:
        with (
            patch.object(executor._client._auth, "sync_clock"),
            patch.object(executor, "_last_price", return_value=market_price),
            patch.object(executor._client, "private_post") as submit,
        ):
            with pytest.raises(
                EntryPreflightError, match=f"primary {side.value} entry"
            ):
                executor._execute_sync(plan)
            submit.assert_not_called()
    finally:
        executor.close()


def test_priced_entry_ticker_failure_rejects_before_submission() -> None:
    executor = BybitDemoExecutor(api_key="key", api_secret="secret")
    try:
        with (
            patch.object(executor._client._auth, "sync_clock"),
            patch.object(
                executor, "_last_price", side_effect=httpx.ReadTimeout("ticker")
            ),
            patch.object(executor._client, "private_post") as submit,
        ):
            with pytest.raises(EntryPreflightError, match="ticker"):
                executor._execute_sync(v2_range_plan())
            submit.assert_not_called()
    finally:
        executor.close()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "-Infinity"])
def test_priced_entry_invalid_ticker_rejects_before_submission(
    side: Side, price: str
) -> None:
    executor = BybitDemoExecutor(api_key="key", api_secret="secret")
    try:
        with (
            patch.object(executor._client._auth, "sync_clock"),
            patch.object(executor, "_last_price", return_value=Decimal(price)),
            patch.object(executor._client, "private_post") as submit,
        ):
            with pytest.raises(EntryPreflightError, match="positive and finite"):
                executor._execute_sync(v2_range_plan(side))
            submit.assert_not_called()
    finally:
        executor.close()


@pytest.mark.parametrize(
    "entry_type", [EntryType.MARKET, EntryType.LIMIT, EntryType.RANGE]
)
@pytest.mark.parametrize("failure", [110013, 110038, "timeout"])
def test_leverage_failure_blocks_entry_submission(entry_type, failure) -> None:
    plan = (
        v2_market_plan()
        if entry_type is EntryType.MARKET
        else v2_range_plan(entry_type=entry_type)
    )
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == "/v5/position/set-leverage"
        if failure == "timeout":
            raise httpx.ReadTimeout("Leverage request timed out", request=request)
        return httpx.Response(
            200, json={"retCode": failure, "retMsg": "Cannot set requested leverage"}
        )

    executor = BybitDemoExecutor(api_key="key", api_secret="secret")
    executor._client.close()
    executor._client._http_client = httpx.Client(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handler)
    )
    try:
        with (
            patch.object(executor._client._auth, "sync_clock"),
            patch.object(executor, "_last_price", return_value=Decimal("120")),
            pytest.raises(EntryPreflightError, match="[Ll]everage"),
        ):
            if entry_type is EntryType.MARKET:
                executor._execute_market_primary_sync(plan)
            else:
                executor._execute_sync(plan)
        assert calls == ["/v5/position/set-leverage"]
    finally:
        executor.close()


def test_unchanged_leverage_code_is_not_success_for_order_submission() -> None:
    client = BybitClient(
        base_url="https://api-demo.bybit.com", api_key="key", api_secret="secret"
    )
    client.close()
    client._http_client = httpx.Client(
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"retCode": 110043})
        ),
    )
    try:
        with pytest.raises(TradeExecutionError, match="110043"):
            client.private_post("/v5/order/create", {})
    finally:
        client.close()
