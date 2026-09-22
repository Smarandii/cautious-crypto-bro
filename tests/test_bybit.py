import json
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal
from unittest.mock import patch

import httpx

from cautious_crypto_bro.bybit import (
    BybitDemoExecutor,
    TradeExecutionError,
)
from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    PlannedOrder,
    Side,
    SourceMessage,
    TradingIntent,
)
from cautious_crypto_bro.execution import (
    ExecutionPlanner,
    InstrumentContext,
)


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

    executor._client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=(httpx.MockTransport(handler)),
    )

    try:
        with patch(
            ("cautious_crypto_bro.bybit._wall_clock_ms"),
            side_effect=[
                100_000,
                100_100,
            ],
        ):
            executor._sync_clock(force=True)

        assert executor._clock_offset_ms == 19_000

    finally:
        executor.close()


def test_range_plan_uses_batch_partial_tpsl() -> None:
    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        assert request.url.path == "/v5/order/create-batch"

        body = request.read().decode()

        assert body.count('"tpslMode":"Partial"') == 3

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

    executor._client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=(httpx.MockTransport(handler)),
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
        ):
            order_ids = executor._execute_sync(range_plan())

        assert order_ids == (
            "a",
            "b",
            "c",
        )

    finally:
        executor.close()


def test_market_execution_rejects_risk_above_budget() -> None:
    plan = ExecutionPlan(
        intent_id=("22222222-2222-2222-2222-222222222222"),
        symbol="BTCUSDT",
        side=Side.LONG,
        orders=(
            PlannedOrder(
                order_type=(ExecutionOrderType.MARKET),
                quantity=Decimal("1"),
                reference_price=(Decimal("100")),
            ),
        ),
        stop_loss=Decimal("50"),
        take_profit=Decimal("200"),
        policy=policy(),
        planned_max_loss_usdt=(Decimal("50")),
    )

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    try:
        with patch.object(
            executor,
            "_last_price",
            return_value=Decimal("130"),
        ):
            try:
                executor._validate_market_plan(
                    plan,
                    Decimal("130"),
                )
            except TradeExecutionError:
                pass
            else:
                raise AssertionError("Expected market risk validation to fail")

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

    executor._client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=httpx.MockTransport(handler),
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
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


def test_v2_entries_use_stop_only_payloads() -> None:
    plan = ExecutionPlanner().plan(
        TradingIntent(
            source=SourceMessage(
                channel_id=1,
                channel_title="Test",
                message_id=1,
                published_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                text="test",
            ),
            symbol="BTCUSDT",
            side=Side.LONG,
            entry=Entry(
                type=EntryType.MARKET,
            ),
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

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    try:
        requests = [
            executor._order_params(
                plan,
                index,
            )
            for index in range(len(plan.orders))
        ]

        assert [request["orderType"] for request in requests] == [
            "Market",
            "Limit",
            "Limit",
        ]

        assert all("takeProfit" not in request for request in requests)

        assert all(request["stopLoss"] == "90" for request in requests)

        assert all(request["tpslMode"] == "Partial" for request in requests)

        assert [
            request["orderLinkId"].rsplit(
                "-",
                1,
            )[-1]
            for request in requests
        ] == [
            "e1",
            "e2",
            "e3",
        ]

    finally:
        executor.close()


def test_v2_market_direct_batch_execution_is_rejected() -> None:
    plan = ExecutionPlanner().plan(
        TradingIntent(
            source=SourceMessage(
                channel_id=1,
                channel_title="Test",
                message_id=1,
                published_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                text="test",
            ),
            symbol="BTCUSDT",
            side=Side.LONG,
            entry=Entry(
                type=EntryType.MARKET,
            ),
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

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
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


def test_v2_market_primary_fill_precedes_scale_ins() -> None:
    plan = ExecutionPlanner().plan(
        TradingIntent(
            source=SourceMessage(
                channel_id=1,
                channel_title="Test",
                message_id=2,
                published_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                text="test",
            ),
            symbol="BTCUSDT",
            side=Side.LONG,
            entry=Entry(
                type=EntryType.MARKET,
            ),
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

        if request.url.path == "/v5/order/create":
            body = json.loads(request.read().decode())

            assert body["orderType"] == "Market"

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

    executor._client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=httpx.MockTransport(handler),
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
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

        assert remaining == (
            "e2-live",
            "e3-live",
        )

        assert batch_request is not None

        requests = batch_request["request"]

        assert len(requests) == 2

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

    finally:
        executor.close()
