"""Read-only, fixed-capital BTC spot benchmark; never a trading instruction."""

from __future__ import annotations

import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .bybit.client import DEMO_BASE_URL, BybitClient
from .dashboard_benchmark import BENCHMARK_START, _positive, _value, validate_benchmark
from .demo_balance import _wallet, daily_wallet_history

BENCHMARK_CACHE_FILENAME = "btc-hold-v1.json"
DAY_MS = 86_400_000


def _candle_timestamp(row: object) -> int:
    if not isinstance(row, list) or len(row) != 7:
        raise ValueError("Invalid BTC candle")
    if not isinstance(row[0], str) or not row[0].isdecimal():
        raise ValueError("Invalid BTC candle timestamp")
    for price in row[1:5]:
        _positive(price)
    if _positive(row[2]) < max(
        _positive(p) for p in (row[1], row[3], row[4])
    ) or _positive(row[3]) > min(_positive(p) for p in (row[1], row[2], row[4])):
        raise ValueError("BTC candle high/low contradict its prices")
    return int(row[0])


def _candles(client: BybitClient, start: datetime, end: datetime) -> dict[int, list]:
    first, last = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    result: dict[int, list] = {}
    cursor = last
    while cursor >= first:
        response = client.public_get(
            "/v5/market/kline",
            {
                "category": "spot",
                "symbol": "BTCUSDT",
                "interval": "D",
                "start": first,
                "end": cursor,
                "limit": 1000,
            },
        )
        data = response.get("result", {})
        if data.get("category") != "spot" or data.get("symbol") != "BTCUSDT":
            raise ValueError("Unexpected BTC price instrument")
        rows = data.get("list")
        if not isinstance(rows, list) or not rows:
            raise ValueError("BTC price history is incomplete")
        times = []
        for row in rows:
            timestamp = _candle_timestamp(row)
            if (
                timestamp % DAY_MS
                or not first <= timestamp <= cursor
                or timestamp in result
            ):
                raise ValueError("Contradictory BTC candle coverage")
            result[timestamp] = row
            times.append(timestamp)
        if times != sorted(times, reverse=True):
            raise ValueError("BTC candles are not in documented reverse order")
        cursor = min(times) - 1
    expected = set(range(first, last - last % DAY_MS + 1, DAY_MS))
    if set(result) != expected:
        raise ValueError("BTC daily price history has gaps")
    return result


def _initial_capital(client: BybitClient, retained: dict | None) -> str:
    # Grug, Chesterton's Fence (faithful paraphrase): preserve the existing
    # observed-balance boundary; a first-day close is not its opening capital.
    if retained is None:
        raise ValueError("Starting wallet evidence unavailable")
    first = next((d for d in retained["days"] if d["date"] == "2026-10-04"), None)
    if first is None or not first["complete"] or first["balance"] is None:
        raise ValueError("Starting wallet close is unverified")
    before = _wallet(
        client.private_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    )
    start, end = (
        BENCHMARK_START - timedelta(days=1),
        BENCHMARK_START + timedelta(days=1),
    )
    rows = client.paginate_private_list(
        "/v5/account/transaction-log",
        {
            "accountType": "UNIFIED",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000) - 1,
            "limit": 50,
        },
    )
    history = daily_wallet_history(
        rows,
        start=start,
        as_of=end - timedelta(milliseconds=1),
        wallet_balance=first["balance"],
    )
    if (
        _wallet(
            client.private_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        )
        != before
    ):
        raise ValueError("Wallet changed during baseline verification")
    capital = history["days"][0]["balance"]
    _positive(capital)
    return capital


def _save_benchmark(path: Path, scope: str, result: dict) -> None:
    temporary = path.with_suffix(".next")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(
            {"schema_version": 1, "scope": scope, "benchmark": result},
            output,
            allow_nan=False,
        )
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _retained_benchmark(path: Path, scope: str) -> dict | None:
    saved = None
    if path.exists():
        if path.stat().st_size > 100_000:
            raise ValueError("BTC benchmark retention exceeds budget")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"schema_version", "scope", "benchmark"}
            or type(envelope["schema_version"]) is not int
            or envelope["schema_version"] != 1
        ):
            raise ValueError("Invalid BTC benchmark retention")
        if envelope["scope"] == scope:
            previous = validate_benchmark(envelope["benchmark"])
            if previous["refresh_failed"] or previous["history"] is None:
                raise ValueError("Retained BTC baseline is not a successful capture")
            saved = previous["history"]
    return saved


def collect_benchmark(
    private: BybitClient,
    market: BybitClient,
    path: Path,
    *,
    api_key: str,
    database_path: Path,
    retained_wallet: dict | None,
) -> dict:
    """Retain an account-bound baseline and independent last-success timestamp."""
    scope = hmac.digest(
        api_key.encode(),
        (DEMO_BASE_URL + "\0" + str(database_path.resolve())).encode(),
        "sha256",
    ).hex()
    saved = _retained_benchmark(path, scope)
    try:
        time = market.public_get("/v5/market/time")["time"]
        if type(time) is not int:
            raise ValueError("BTC capture time unavailable")
        as_of = datetime.fromtimestamp(time / 1000, UTC)
        start = max(
            BENCHMARK_START,
            (as_of - timedelta(days=30)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
        )
        prices = _candles(market, start, as_of)
        if saved is None:
            capital = _initial_capital(private, retained_wallet)
            initial = _candles(
                market,
                BENCHMARK_START,
                BENCHMARK_START + timedelta(days=1) - timedelta(milliseconds=1),
            )
            entry = initial[int(BENCHMARK_START.timestamp() * 1000)][1]
        else:
            capital, entry = saved["capital"], saved["entry_price"]
            if as_of < datetime.fromisoformat(saved["as_of"]):
                raise ValueError("BTC capture moved backwards")
        history = {
            "start": BENCHMARK_START.isoformat(),
            "source": "BYBIT_SPOT_BTCUSDT",
            "fee_basis": "GROSS_NO_SUBSEQUENT_FLOWS",
            "capital": capital,
            "entry_price": entry,
            "as_of": as_of.isoformat(),
            "days": [
                {
                    "date": datetime.fromtimestamp(t / 1000, UTC).date().isoformat(),
                    "price": row[4],
                    "value": _value(capital, entry, row[4]),
                    "complete": datetime.fromtimestamp(t / 1000, UTC).date()
                    < as_of.date(),
                }
                for t, row in sorted(prices.items())
            ],
        }
        result = validate_benchmark({"refresh_failed": False, "history": history})
    except Exception:
        return {"refresh_failed": True, "history": saved}
    try:
        _save_benchmark(path, scope, result)
    except OSError:
        return {"refresh_failed": True, "history": saved}
    return result
