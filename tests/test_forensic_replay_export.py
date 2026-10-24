"""Forensic export coverage includes unfilled randomized assignments."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from cautious_crypto_bro.bybit.client import DEMO_BASE_URL, BybitClient

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
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
            with pytest.raises(ValueError, match="repeated pagination cursor"):
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
