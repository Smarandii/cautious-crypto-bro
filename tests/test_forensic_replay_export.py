"""Forensic export coverage includes unfilled randomized assignments."""

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cautious_crypto_bro.bybit.client import DEMO_BASE_URL, BybitClient
from cautious_crypto_bro.domain import TradeExecutionError
from cautious_crypto_bro.forensic_pnl import reconcile

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import export_forensic_replay as exporter  # noqa: E402
from export_forensic_replay import (  # noqa: E402
    MARKET_CANDLE_LOOKBACK_MS,
    _historical_rows,
    _market_candle_coverage,
)


def test_market_candle_coverage_includes_skip_only_symbols_and_signal_history() -> None:
    signal_time = datetime(2026, 10, 9, 12, tzinfo=UTC)
    signal_ms = int(signal_time.timestamp() * 1000)

    symbols, start_ms = _market_candle_coverage(
        {"BTCUSDT"},
        [signal_ms + 5 * 60_000],
        [
            {
                "symbol": "skipusdt",
                "created_at": signal_time.isoformat(),
                "arm": "skip",
            }
        ],
    )

    assert symbols == ["BTCUSDT", "SKIPUSDT"]
    assert start_ms == signal_ms - MARKET_CANDLE_LOOKBACK_MS


@pytest.mark.parametrize("cursors", [("a", "a"), ("a", "b", "a"), ("a", "")])
def test_history_export_rejects_pagination_cycles(cursors) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "time": 1000})
        assert request.url.path == "/v5/position/closed-pnl"
        requests.append(request)
        index = len(requests) - 1
        assert request.url.params.get("cursor") == (
            cursors[index - 1] if index else None
        )
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [{"orderId": f"close-{index}", "closedPnl": "17"}],
                    "nextPageCursor": cursors[index],
                },
            },
        )

    client = BybitClient(DEMO_BASE_URL, "synthetic-key", "synthetic-secret")
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(respond)
    )
    try:
        if cursors[-1]:
            with pytest.raises(TradeExecutionError, match="page cursor repeated"):
                _historical_rows(
                    client, "/v5/position/closed-pnl", {}, 0, 1000, limit=100
                )
        else:
            rows = _historical_rows(
                client, "/v5/position/closed-pnl", {}, 0, 1000, limit=100
            )
            assert [row["orderId"] for row in rows] == ["close-0", "close-1"]
    finally:
        client.close()
    assert len(requests) == len(cursors)


@pytest.mark.parametrize(
    "page",
    [{}, {"list": {}}, {"list": ["invalid-row"]}, {"list": [], "nextPageCursor": 0}],
)
def test_history_export_rejects_malformed_pages(page) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "time": 1000})
        return httpx.Response(200, json={"retCode": 0, "result": page})

    client = BybitClient(DEMO_BASE_URL, "synthetic-key", "synthetic-secret")
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(TradeExecutionError, match="invalid page"):
            _historical_rows(client, "/v5/position/closed-pnl", {}, 0, 1000, limit=100)
    finally:
        client.close()


def test_transaction_history_keeps_rows_before_empty_null_terminal_page():
    requests = []

    def respond(request):
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "time": 1000})
        requests.append(request)
        assert len(requests) <= 2
        assert request.url.path == "/v5/account/transaction-log"
        assert request.url.params.get("cursor") == (
            None if len(requests) == 1 else "next"
        )
        page = (
            {
                "list": [{"id": "settlement-1", "type": "SETTLEMENT"}],
                "nextPageCursor": "next",
            }
            if len(requests) == 1
            else {"list": [], "nextPageCursor": None}
        )
        return httpx.Response(200, json={"retCode": 0, "result": page})

    client = BybitClient(DEMO_BASE_URL, "synthetic-key", "synthetic-secret")
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(respond)
    )
    try:
        rows = _historical_rows(
            client,
            "/v5/account/transaction-log",
            {"accountType": "UNIFIED"},
            0,
            1000,
            limit=50,
        )
        assert rows == [{"id": "settlement-1", "type": "SETTLEMENT"}]
        assert len(requests) == 2
    finally:
        client.close()


@pytest.mark.parametrize(
    "path,page,cursor",
    [
        ("/v5/account/transaction-log", [{"id": "unconfirmed-continuation"}], None),
        ("/v5/account/transaction-log", [], 0),
        ("/v5/account/transaction-log", [], False),
        ("/v5/account/transaction-log", [], []),
        ("/v5/account/transaction-log", [], {}),
        ("/v5/order/realtime", [], None),
        ("/v5/position/list", [], None),
        ("/v5/execution/list", [], None),
    ],
)
def test_terminal_transaction_compatibility_does_not_accept_malformed_pages(
    path, page, cursor
):
    def respond(request):
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "time": 1000})
        return httpx.Response(
            200, json={"retCode": 0, "result": {"list": page, "nextPageCursor": cursor}}
        )

    client = BybitClient(DEMO_BASE_URL, "synthetic-key", "synthetic-secret")
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(TradeExecutionError, match="invalid page cursor"):
            _historical_rows(client, path, {}, 0, 1000, limit=50)
    finally:
        client.close()


@pytest.mark.parametrize("skip_candles", [False, True])
def test_export_bundle_uses_supported_private_page_sizes(
    tmp_path, monkeypatch, skip_candles
) -> None:
    database_path = tmp_path / "state.sqlite3"
    now = datetime.now(UTC)
    timestamp = int(now.timestamp() * 1000)
    with sqlite3.connect(database_path) as database:
        database.executescript(
            """
            CREATE TABLE execution_plans (
                intent_id TEXT, payload_json TEXT, bybit_order_ids_json TEXT,
                created_at TEXT
            );
            CREATE TABLE intents (
                intent_id TEXT, status TEXT, approval_mode TEXT, payload_json TEXT
            );
            CREATE TABLE position_actions (
                payload_json TEXT, status TEXT, bybit_order_id TEXT, created_at TEXT
            );
            """
        )
        database.execute(
            "INSERT INTO execution_plans VALUES (?,?,?,?)",
            (
                "intent-1",
                json.dumps(
                    {
                        "strategy_version": 2,
                        "symbol": "BTCUSDT",
                        "side": "LONG",
                        "stop_loss": "90",
                    }
                ),
                '["entry-1"]',
                now.isoformat(),
            ),
        )
        database.execute(
            "INSERT INTO intents VALUES (?,?,?,?)",
            ("intent-1", "EXECUTED", "AUTO", "{}"),
        )

    pages = {
        "/v5/execution/list": (
            100,
            [
                {
                    "execType": "Trade",
                    "orderId": "entry-1",
                    "execTime": str(timestamp),
                    "execQty": "2",
                    "execPrice": "100",
                    "closedSize": "0",
                }
            ],
        ),
        "/v5/account/transaction-log": (
            50,
            [
                {
                    "type": "SETTLEMENT",
                    "symbol": "BTCUSDT",
                    "transactionTime": str(timestamp),
                    "funding": "-1",
                }
            ],
        ),
        "/v5/position/closed-pnl": (
            100,
            [
                {
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "updatedTime": str(timestamp),
                    "closedSize": "2",
                    "closedPnl": "17",
                }
            ],
        ),
        "/v5/order/history": (50, []),
        "/v5/order/realtime": (50, []),
        "/v5/position/list": (200, []),
    }
    seen = set()

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "time": timestamp})
        if path == "/v5/market/kline":
            assert not skip_candles
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
        maximum, rows = pages[path]
        if int(request.url.params["limit"]) > maximum:
            return httpx.Response(
                200, json={"retCode": 10001, "retMsg": "limit exceeds endpoint maximum"}
            )
        seen.add(path)
        return httpx.Response(
            200, json={"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}}
        )

    client = BybitClient(DEMO_BASE_URL, "synthetic-key", "synthetic-secret")
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr(exporter, "BybitClient", lambda **kwargs: client)
    monkeypatch.setattr(
        exporter,
        "get_settings",
        lambda: SimpleNamespace(
            database_path=database_path,
            bybit_api_key="synthetic-key",
            bybit_api_secret="synthetic-secret",
        ),
    )
    bundle = tmp_path / "export.zip"
    try:
        metadata = exporter.export_bundle(bundle, skip_market_candles=skip_candles)
    finally:
        client.close()
    assert seen == set(pages)
    assert metadata["filled_v2_cases"] == 1
    assert metadata["market_candles_included"] is not skip_candles
    positions = reconcile(bundle)
    assert len(positions) == 1 and positions[0]["complete"]
    assert positions[0]["net_pnl"] == 17
    assert positions[0]["funding"] == -1
