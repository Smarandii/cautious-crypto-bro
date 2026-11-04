"""Signed ledger, public spot prices, scoped retention and public transport together."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_demo_balance_integration import event

import cautious_crypto_bro.btc_benchmark as benchmark_module
from cautious_crypto_bro.btc_benchmark import collect_benchmark, validate_benchmark
from cautious_crypto_bro.bybit.client import DEMO_BASE_URL, BybitClient
from cautious_crypto_bro.dashboard_balance import (
    decode_balance_feed,
    encode_balance_feed,
)
from cautious_crypto_bro.demo_balance import daily_wallet_history

pytestmark = pytest.mark.integration
AS_OF = datetime(2026, 10, 6, 12, tzinfo=UTC)
START = datetime(2026, 10, 3, tzinfo=UTC)


class BenchmarkHTTP:
    def __init__(self, *, prices=("100", "110", "90"), fault=None):
        self.fault = fault
        self.as_of = AS_OF
        self.requests = []
        self.rows = [
            event(
                "prior", "2026-10-03T12:00:00+00:00", "900", "100", kind="TRANSFER_IN"
            ),
            event("first", "2026-10-04T12:00:00+00:00", "1000", "100"),
        ]
        self.candles = [
            [
                str(int(datetime(2026, 10, i + 4, tzinfo=UTC).timestamp() * 1000)),
                "100",
                "200",
                "50",
                price,
                "1",
                "100",
            ]
            for i, price in enumerate(prices)
        ]

    def handle(self, request):
        assert request.method == "GET", "Benchmark must never mutate exchange state"
        self.requests.append(request)
        params = request.url.params
        if request.url.path == "/v5/market/time":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "time": int(self.as_of.timestamp() * 1000),
                    "result": {},
                },
            )
        if request.url.path == "/v5/market/kline":
            assert not request.headers.get("X-BAPI-API-KEY")
            assert params["category"] == "spot" and params["symbol"] == "BTCUSDT"
            if self.fault == "outage":
                raise httpx.ConnectError(
                    "synthetic private diagnostic", request=request
                )
            rows = [
                r
                for r in reversed(self.candles)
                if int(params["start"]) <= int(r[0]) <= int(params["end"])
            ]
            if self.fault == "gap":
                rows = [r for r in rows if r is not self.candles[1]]
            if self.fault == "duplicate" and rows:
                rows = [rows[0], rows[0]]
            if self.fault == "price" and rows:
                rows = deepcopy(rows)
                rows[0][4] = "NaN"
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "time": int(self.as_of.timestamp() * 1000),
                    "result": {
                        "category": "linear" if self.fault == "category" else "spot",
                        "symbol": "BTCUSDT",
                        "list": rows[:2],
                    },
                },
            )
        assert request.headers.get("X-BAPI-SIGN"), "Private ledger reads must be signed"
        if request.url.path == "/v5/account/wallet-balance":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "accountType": "UNIFIED",
                                "coin": [
                                    {
                                        "coin": "USDT",
                                        "walletBalance": "1100",
                                        "spotBorrow": "0",
                                        "borrowAmount": "0",
                                        "accruedInterest": "0",
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        assert request.url.path == "/v5/account/transaction-log"
        assert "category" not in params and "type" not in params
        if self.fault == "ledger":
            raise httpx.ConnectError("synthetic ledger outage", request=request)
        rows = self.rows[1:] if self.fault == "no-prior" else self.rows
        cursor = int(params.get("cursor", "0"))
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": rows[cursor : cursor + 1],
                    "nextPageCursor": str(cursor + 1) if cursor + 1 < len(rows) else "",
                },
            },
        )


@contextmanager
def clients(http):
    private = BybitClient(DEMO_BASE_URL, "SYNTHETIC_KEY", "SYNTHETIC_SECRET")
    market = BybitClient("https://api.bybit.com", "", "")
    for client, host in ((private, DEMO_BASE_URL), (market, "https://api.bybit.com")):
        client._http_client.close()
        client._http_client = httpx.Client(
            base_url=host, transport=httpx.MockTransport(http.handle)
        )
    try:
        yield private, market
    finally:
        private.close()
        market.close()


def retained(http):
    return daily_wallet_history(
        http.rows, start=START, as_of=AS_OF, wallet_balance="1100"
    )


def capture(tmp_path, http, *, key="SYNTHETIC_KEY", wallet=True):
    with clients(http) as (private, market):
        return collect_benchmark(
            private,
            market,
            tmp_path / "btc-hold-v1.json",
            api_key=key,
            database_path=tmp_path / "account.sqlite3",
            retained_wallet=retained(http) if wallet else None,
        )


@pytest.mark.parametrize(
    "prices,expected",
    [
        (("100", "100", "100"), ["1000.00000000"] * 3),
        (("100", "110", "120"), ["1000.00000000", "1100.00000000", "1200.00000000"]),
        (("100", "90", "80"), ["1000.00000000", "900.00000000", "800.00000000"]),
    ],
)
def test_independent_start_capital_fixed_holding_and_transport(
    tmp_path, prices, expected
):
    http = BenchmarkHTTP(prices=prices)
    result = capture(tmp_path, http)
    assert not result["refresh_failed"]
    h = result["history"]
    assert h["capital"] == "1000"  # Not first-day closing wallet1100.
    assert h["entry_price"] == "100"
    assert h["start"] == "2026-10-04T00:00:00+00:00"
    assert [d["value"] for d in h["days"]] == expected
    assert [d["complete"] for d in h["days"]] == [True, True, False]
    feed = {
        "schema_version": 2,
        "refresh_failed": True,
        "history": None,
        "benchmark": result,
    }
    assert decode_balance_feed(encode_balance_feed(feed)) == feed
    assert sum(r.url.path == "/v5/account/transaction-log" for r in http.requests) == 2
    saved = (tmp_path / "btc-hold-v1.json").read_text()
    assert "SYNTHETIC_KEY" not in saved and "SYNTHETIC_SECRET" not in saved
    assert "PRIVATE_ORDER_CANARY" not in saved and "PRIVATE_LINK_CANARY" not in saved


@pytest.mark.parametrize(
    "fault", ["no-prior", "ledger", "outage", "gap", "duplicate", "price", "category"]
)
def test_unverified_inputs_never_invent_start_or_prices(tmp_path, fault):
    result = capture(tmp_path, BenchmarkHTTP(fault=fault))
    assert result == {"refresh_failed": True, "history": None}
    assert not (tmp_path / "btc-hold-v1.json").exists()


def test_cache_keeps_baseline_when_wallet_window_moves_and_preserves_stale_time(
    tmp_path,
):
    http = BenchmarkHTTP()
    initial = capture(tmp_path, http)
    path = tmp_path / "btc-hold-v1.json"
    original = path.read_bytes()
    http.fault = "outage"
    stale = capture(tmp_path, http, wallet=False)
    assert stale == {"refresh_failed": True, "history": initial["history"]}
    assert path.read_bytes() == original
    http.fault = (
        "ledger"  # Later refresh must not rederive capital from a rolling window.
    )
    refreshed = capture(tmp_path, http, wallet=False)
    assert not refreshed["refresh_failed"] and refreshed["history"]["capital"] == "1000"
    other = capture(tmp_path, http, key="DIFFERENT_KEY", wallet=False)
    assert other == {"refresh_failed": True, "history": None}
    assert json.loads(path.read_text())["benchmark"] == refreshed


def test_corrupt_cache_and_public_extra_fields_are_rejected(tmp_path):
    result = capture(tmp_path, BenchmarkHTTP())
    invalid = deepcopy(result)
    invalid["history"]["days"][0]["orderId"] = "PRIVATE"
    with pytest.raises(ValueError):
        validate_benchmark(invalid)
    truncated = deepcopy(result)
    truncated["history"]["days"] = truncated["history"]["days"][1:]
    with pytest.raises(ValueError, match="price gap"):
        validate_benchmark(truncated)
    path = tmp_path / "btc-hold-v1.json"
    path.write_text("not json")
    with pytest.raises(ValueError):
        capture(tmp_path, BenchmarkHTTP())
    assert path.read_text() == "not json"


def test_pages_validator_needs_no_installed_dependencies(tmp_path):
    benchmark = capture(tmp_path, BenchmarkHTTP())
    feed = {
        "schema_version": 2,
        "refresh_failed": True,
        "history": None,
        "benchmark": benchmark,
    }
    code = "from cautious_crypto_bro.dashboard_balance import decode_balance_feed;import sys;f=decode_balance_feed(sys.stdin.read());assert f['benchmark']['history']['capital']=='1000'"
    env = os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        input=encode_balance_feed(feed),
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_cache_write_failure_retains_verified_capture_and_timestamp(
    tmp_path, monkeypatch
):
    http = BenchmarkHTTP()
    initial = capture(tmp_path, http)
    path = tmp_path / "btc-hold-v1.json"
    before = path.read_bytes()

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(benchmark_module, "_save_benchmark", fail)
    result = capture(tmp_path, http)
    assert result == {"refresh_failed": True, "history": initial["history"]}
    assert path.read_bytes() == before
