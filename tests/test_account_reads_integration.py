"""Account read contracts through real signing, pagination and worker ownership."""

import asyncio
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from cautious_crypto_bro.bybit import BybitDemoExecutor
from cautious_crypto_bro.domain import TradeExecutionError

pytestmark = pytest.mark.integration


@contextmanager
def broker(handler):
    executor = BybitDemoExecutor(api_key="synthetic-key", api_secret="synthetic-secret")
    executor._client.close()
    executor._client._http_client = httpx.Client(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handler)
    )
    try:
        yield executor
    finally:
        executor.close()


def response(rows, cursor=""):
    return httpx.Response(
        200, json={"retCode": 0, "result": {"list": rows, "nextPageCursor": cursor}}
    )


def clock_response():
    return httpx.Response(
        200, json={"retCode": 0, "time": int(datetime.now(UTC).timestamp() * 1000)}
    )


def pnl_row(order_id, updated, amount):
    return {
        "symbol": "BTCUSDT",
        "orderId": order_id,
        "side": "Sell",
        "closedSize": "2",
        "closedPnl": amount,
        "updatedTime": str(int(updated.timestamp() * 1000)),
    }


def test_closed_pnl_windows_paginate_deduplicate_and_sort():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    boundary = start + timedelta(days=7)
    end = start + timedelta(days=9)
    requests = []

    def handle(request):
        assert request.method == "GET"
        requests.append(request)
        if request.url.path == "/v5/market/time":
            return clock_response()
        assert request.url.path == "/v5/position/closed-pnl"
        assert request.headers["X-BAPI-SIGN"]
        params = request.url.params
        assert params["category"] == "linear" and params["limit"] == "100"
        if int(params["startTime"]) == int(start.timestamp() * 1000):
            assert int(params["endTime"]) == int(boundary.timestamp() * 1000)
            if params.get("cursor"):
                assert params["cursor"] == "next"
                return response([pnl_row("early", start + timedelta(days=1), "1")])
            return response(
                [pnl_row("updated", start + timedelta(days=6), "2")], "next"
            )
        assert int(params["startTime"]) == int(boundary.timestamp() * 1000)
        assert int(params["endTime"]) == int(end.timestamp() * 1000)
        assert not params.get("cursor")
        return response([pnl_row("updated", start + timedelta(days=8), "3")])

    with broker(handle) as executor:
        records = asyncio.run(executor.closed_pnl_history(start, end))
    assert [record.order_id for record in records] == ["early", "updated"]
    assert [record.closed_pnl for record in records] == [Decimal("1"), Decimal("3")]
    assert all(record.closed_size == Decimal("2") for record in records)
    assert records[-1].updated_at == start + timedelta(days=8)
    assert [request.url.path for request in requests] == [
        "/v5/market/time",
        "/v5/position/closed-pnl",
        "/v5/position/closed-pnl",
        "/v5/position/closed-pnl",
    ]


@pytest.mark.parametrize("invalid", ["page", "cursor", "pnl"])
def test_closed_pnl_rejects_invalid_read_evidence(invalid):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    calls = []

    def handle(request):
        assert request.method == "GET"
        if request.url.path == "/v5/market/time":
            return clock_response()
        assert request.url.path == "/v5/position/closed-pnl"
        calls.append(request)
        row = pnl_row("synthetic-order", start, None if invalid == "pnl" else "1")
        return response(
            "invalid" if invalid == "page" else [row],
            "again" if invalid == "cursor" else "",
        )

    with broker(handle) as executor, pytest.raises(TradeExecutionError):
        asyncio.run(executor.closed_pnl_history(start, start + timedelta(days=1)))
    assert len(calls) == (2 if invalid == "cursor" else 1)


def test_invalid_or_empty_pnl_range_does_not_contact_exchange():
    def unexpected(request):
        raise AssertionError(f"Unexpected read: {request.url.path}")

    start = datetime(2026, 1, 1, tzinfo=UTC)
    with broker(unexpected) as executor:
        assert asyncio.run(executor.closed_pnl_history(start, start)) == ()
        with pytest.raises(ValueError, match="timezone-aware"):
            asyncio.run(executor.closed_pnl_history(start.replace(tzinfo=None), start))


def test_account_read_cancellation_waits_for_complete_worker():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    paths = []

    def handle(request):
        assert request.method == "GET"
        paths.append(request.url.path)
        if request.url.path == "/v5/market/time":
            return clock_response()
        if request.url.path == "/v5/position/list":
            started.set()
            assert release.wait(5), "Synthetic read was not released"
            return response([])
        assert request.url.path == "/v5/order/realtime"
        finished.set()
        return response([])

    async def run():
        with broker(handle) as executor:
            task = asyncio.create_task(executor.account_state())
            try:
                assert await asyncio.to_thread(started.wait, 2)
                for _ in range(2):
                    task.cancel()
                    done, _ = await asyncio.wait({task}, timeout=0.03)
                    assert not done and not finished.is_set()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert finished.is_set()

    asyncio.run(run())
    assert paths == ["/v5/market/time", "/v5/position/list", "/v5/order/realtime"]
