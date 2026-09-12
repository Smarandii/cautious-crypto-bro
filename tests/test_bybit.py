from unittest.mock import patch

import httpx

from cautious_crypto_bro.bybit import BybitDemoExecutor


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
        notional_usdt=100,
    )

    executor._client.close()
    executor._client = httpx.Client(
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(handler),
    )

    try:
        with patch(
            "cautious_crypto_bro.bybit._wall_clock_ms",
            side_effect=[100_000, 100_100],
        ):
            executor._sync_clock(force=True)

        assert executor._clock_offset_ms == 19_000
    finally:
        executor.close()
