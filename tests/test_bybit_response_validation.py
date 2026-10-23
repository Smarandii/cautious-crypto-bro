"""Exercise response decoding and signing through the real HTTP adapter."""

import time

import httpx
import pytest

from cautious_crypto_bro.bybit import BybitClient
from cautious_crypto_bro.domain import TradeExecutionError

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("method", ["public", "private_get", "private_post"])
def test_malformed_responses_are_safe_exchange_failures_without_request_replay(method):
    client = BybitClient(
        base_url="https://api-demo.bybit.com",
        api_key="synthetic-key",
        api_secret="synthetic-secret",
    )
    client._http_client.close()
    requests = []
    body = ""
    target = "/v5/market/tickers" if method == "public" else "/v5/order/create"

    def handle(request):
        if request.url.path == "/v5/market/time":
            return httpx.Response(
                200, json={"retCode": 0, "time": int(time.time() * 1000), "result": {}}
            )
        requests.append(request)
        assert request.url.path == target
        return httpx.Response(200, text=body)

    client._http_client = httpx.Client(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handle)
    )

    def call():
        if method == "public":
            return client.public_get(target)
        if method == "private_get":
            return client.private_get(target, {})
        return client.private_post(target, {})

    try:
        for payload in (
            "<html>SYNTHETIC_PRIVATE_GATEWAY</html>",
            "[]",
            "null",
            '{"result":{},"retMsg":"SYNTHETIC_PRIVATE_GATEWAY"}',
            '{"retCode":null,"retMsg":"SYNTHETIC_PRIVATE_GATEWAY"}',
            '{"retCode":"bad","retMsg":"SYNTHETIC_PRIVATE_GATEWAY"}',
            '{"retCode":false,"retMsg":"SYNTHETIC_PRIVATE_GATEWAY"}',
            '{"retCode":0,"result":[]}',
            '{"retCode":0,"result":null}',
        ):
            body = payload
            before = len(requests)
            with pytest.raises(TradeExecutionError) as error:
                call()
            assert len(requests) == before + 1
            assert "SYNTHETIC_PRIVATE_GATEWAY" not in str(error.value)
        body = '{"retCode":0,"result":{"confirmed":true}}'
        assert call()["result"] == {"confirmed": True}
        body = '{"retCode":"0","result":{}}'
        assert call()["retCode"] == "0"
    finally:
        client.close()
