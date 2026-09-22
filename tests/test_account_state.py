from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal
from unittest.mock import patch

import httpx

from cautious_crypto_bro.bybit import (
    BybitDemoExecutor,
)


def test_account_state_reads_bybit_snapshot() -> None:
    now_ms = int(
        datetime(
            2026,
            9,
            13,
            12,
            0,
            tzinfo=UTC,
        ).timestamp()
        * 1000
    )

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        path = request.url.path

        if path == "/v5/position/list":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "NEARUSDT",
                                "side": "Buy",
                                "size": "819.2",
                                "avgPrice": "2.304",
                                "markPrice": "2.31",
                                "unrealisedPnl": "4.9152",
                                "positionStatus": "Normal",
                                "takeProfit": "",
                                "stopLoss": "2.221",
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        if path == "/v5/order/realtime":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "Buy",
                                "orderType": "Limit",
                                "orderStatus": "New",
                                "qty": "0.01",
                                "leavesQty": "0.01",
                                "price": "112000",
                                "avgPrice": "",
                                "orderId": "open-1",
                                "orderLinkId": "ccb-open-1",
                                "reduceOnly": False,
                                "updatedTime": str(now_ms),
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        if path == "/v5/order/history":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "ETHUSDT",
                                "side": "Sell",
                                "orderType": "Limit",
                                "orderStatus": "Cancelled",
                                "qty": "0.2",
                                "leavesQty": "0.2",
                                "price": "4700",
                                "avgPrice": "",
                                "orderId": "closed-1",
                                "orderLinkId": "ccb-old",
                                "reduceOnly": False,
                                "updatedTime": str(now_ms),
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        if path == "/v5/position/closed-pnl":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {"closedPnl": "12.4"},
                            {"closedPnl": "-2.1"},
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        raise AssertionError(f"Unexpected request {request.url}")

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
            state = executor._account_state_sync()

        assert state.unrealised_pnl == Decimal("4.9152")

        assert len(state.positions) == 1

        assert len(state.open_orders) == 1

        exposure = state.exposure_for("BTCUSDT")

        assert len(exposure.pending_entry_orders) == 1

    finally:
        executor.close()


def test_wallet_balance_reads_total_wallet_balance() -> None:
    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        assert request.url.path == "/v5/account/wallet-balance"
        assert request.url.params["accountType"] == "UNIFIED"

        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {
                            "accountType": "UNIFIED",
                            "totalWalletBalance": "7400",
                            "totalEquity": "7425",
                            "totalPerpUPL": "25",
                        }
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
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(handler),
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
        ):
            balance = executor._wallet_balance_usdt_sync()

        assert balance == Decimal("7400")

    finally:
        executor.close()
