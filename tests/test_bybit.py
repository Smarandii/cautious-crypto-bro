from datetime import (
    datetime,
    timezone,
)
from decimal import Decimal
from unittest.mock import patch

import httpx

from cautious_crypto_bro.bybit import (
    BybitDemoExecutor,
    TradeExecutionError,
)
from cautious_crypto_bro.domain import (
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    PlannedOrder,
    Side,
)


def policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        trading_capital_usdt=(
            Decimal("6800")
        ),
        risk_per_trade_pct=(
            Decimal("1")
        ),
        range_order_count=3,
    )


def range_plan() -> ExecutionPlan:
    return ExecutionPlan(
        intent_id=(
            "11111111-1111-1111-"
            "1111-111111111111"
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        orders=(
            PlannedOrder(
                order_type=(
                    ExecutionOrderType.LIMIT
                ),
                quantity=Decimal("0.1"),
                price=Decimal("100"),
                reference_price=(
                    Decimal("100")
                ),
            ),
            PlannedOrder(
                order_type=(
                    ExecutionOrderType.LIMIT
                ),
                quantity=Decimal("0.1"),
                price=Decimal("105"),
                reference_price=(
                    Decimal("105")
                ),
            ),
            PlannedOrder(
                order_type=(
                    ExecutionOrderType.LIMIT
                ),
                quantity=Decimal("0.1"),
                price=Decimal("110"),
                reference_price=(
                    Decimal("110")
                ),
            ),
        ),
        stop_loss=Decimal("90"),
        take_profit=Decimal("130"),
        policy=policy(),
        planned_max_loss_usdt=(
            Decimal("4.5")
        ),
        created_at=datetime.now(
            timezone.utc
        ),
    )


def test_clock_sync_compensates_for_local_drift() -> None:
    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        assert (
            request.url.path
            == "/v5/market/time"
        )

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
        base_url=(
            "https://api-demo.bybit.com"
        ),
        transport=(
            httpx.MockTransport(
                handler
            )
        ),
    )

    try:
        with patch(
            (
                "cautious_crypto_bro"
                ".bybit._wall_clock_ms"
            ),
            side_effect=[
                100_000,
                100_100,
            ],
        ):
            executor._sync_clock(
                force=True
            )

        assert (
            executor._clock_offset_ms
            == 19_000
        )

    finally:
        executor.close()


def test_auth_timestamp_applies_clock_offset() -> None:
    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    try:
        executor._clock_offset_ms = (
            19_000
        )

        with (
            patch.object(
                executor,
                "_sync_clock",
            ),
            patch(
                (
                    "cautious_crypto_bro"
                    ".bybit._wall_clock_ms"
                ),
                return_value=100_000,
            ),
        ):
            assert (
                executor._auth_timestamp()
                == "119000"
            )

    finally:
        executor.close()


def test_range_plan_uses_batch_partial_tpsl() -> None:
    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        assert (
            request.url.path
            == "/v5/order/create-batch"
        )

        body = (
            request.read()
            .decode()
        )

        assert (
            body.count(
                '"tpslMode":"Partial"'
            )
            == 3
        )

        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {
                            "orderId": "a"
                        },
                        {
                            "orderId": "b"
                        },
                        {
                            "orderId": "c"
                        },
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
        base_url=(
            "https://api-demo.bybit.com"
        ),
        transport=(
            httpx.MockTransport(
                handler
            )
        ),
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
        ):
            order_ids = (
                executor._execute_sync(
                    range_plan()
                )
            )

        assert order_ids == (
            "a",
            "b",
            "c",
        )

    finally:
        executor.close()


def test_market_execution_rejects_risk_above_budget() -> None:
    plan = ExecutionPlan(
        intent_id=(
            "22222222-2222-2222-"
            "2222-222222222222"
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        orders=(
            PlannedOrder(
                order_type=(
                    ExecutionOrderType.MARKET
                ),
                quantity=Decimal("1"),
                reference_price=(
                    Decimal("100")
                ),
            ),
        ),
        stop_loss=Decimal("50"),
        take_profit=Decimal("200"),
        policy=policy(),
        planned_max_loss_usdt=(
            Decimal("50")
        ),
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
                raise AssertionError(
                    "Expected market risk "
                    "validation to fail"
                )

    finally:
        executor.close()
