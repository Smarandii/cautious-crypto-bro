"""Real signed/paginated client through ledger reduction, with HTTP as the seam."""

import base64
import gzip
import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import cautious_crypto_bro.demo_balance_cache as balance_cache
from cautious_crypto_bro.bybit.client import DEMO_BASE_URL, BybitClient
from cautious_crypto_bro.dashboard_balance import (
    MAX_BALANCE_BYTES,
    decode_balance_feed,
    encode_balance_feed,
    validate_balance_history,
)
from cautious_crypto_bro.demo_balance import (
    daily_wallet_history,
    fetch_daily_wallet_history,
)
from cautious_crypto_bro.domain import TradeExecutionError

pytestmark = pytest.mark.integration
AS_OF = datetime(2026, 11, 3, 12, tzinfo=UTC)
START = datetime(2026, 10, 4, tzinfo=UTC)


def event(identity, at, before, change, *, kind="TRADE", fee="0", funding="0"):
    delta = Decimal(change)
    return {
        "id": identity,
        "transactionTime": str(int(datetime.fromisoformat(at).timestamp() * 1000)),
        "currency": "USDT",
        "type": kind,
        "category": "linear" if kind in ("TRADE", "SETTLEMENT") else "",
        "symbol": "BTCUSDT" if kind in ("TRADE", "SETTLEMENT") else "",
        "side": "Buy" if kind == "TRADE" else "None",
        "qty": "1",
        "tradePrice": "100",
        "fee": fee,
        "funding": funding,
        "cashFlow": str(delta - Decimal(funding) + Decimal(fee)),
        "change": str(delta),
        "cashBalance": str(Decimal(before) + delta),
        "orderId": "PRIVATE_ORDER_CANARY",
        "orderLinkId": "PRIVATE_LINK_CANARY",
    }


def history():
    return [
        event(
            "deposit", "2026-10-05T12:00:00+00:00", "1000", "500", kind="TRANSFER_IN"
        ),
        event("entry", "2026-10-06T12:00:00+00:00", "1500", "-2", fee="2"),
        event("exit", "2026-10-06T12:00:00+00:00", "1498", "99", fee="1"),
        event(
            "funding",
            "2026-10-07T00:00:00+00:00",
            "1597",
            "-3",
            kind="SETTLEMENT",
            funding="-3",
        ),
        event(
            "withdraw", "2026-10-08T12:00:00+00:00", "1594", "-94", kind="TRANSFER_OUT"
        ),
    ]


class LedgerHTTP:
    def __init__(self, rows, *, fault=None):
        self.rows = rows
        self.fault = fault
        self.requests = []
        self.wallet_reads = 0

    def handle(self, request):
        assert request.method == "GET", "Balance collection must never mutate Bybit"
        self.requests.append(request)
        if request.url.path == "/v5/market/time":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "time": int(AS_OF.timestamp() * 1000),
                    "result": {},
                },
            )
        assert request.headers.get("X-BAPI-SIGN")
        if request.url.path == "/v5/account/wallet-balance":
            self.wallet_reads += 1
            amount = (
                "1501"
                if self.fault == "moving-wallet" and self.wallet_reads == 2
                else "1500"
            )
            coins = [
                {
                    "coin": "USDT",
                    "walletBalance": amount,
                    "borrowAmount": "1" if self.fault == "borrow" else "0",
                    "spotBorrow": "0",
                    "accruedInterest": "0",
                }
            ]
            if self.fault == "other-asset":
                coins.append(
                    {
                        "coin": "BTC",
                        "walletBalance": "1",
                        "borrowAmount": "0",
                        "spotBorrow": "0",
                        "accruedInterest": "0",
                    }
                )
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "time": int(AS_OF.timestamp() * 1000),
                    "result": {
                        "list": [
                            {
                                "accountType": "UNIFIED",
                                "totalWalletBalance": str(1500 + self.wallet_reads),
                                "coin": coins,
                            }
                        ]
                    },
                },
            )
        assert request.url.path == "/v5/account/transaction-log"
        params = request.url.params
        assert (
            "category" not in params
            and "type" not in params
            and "currency" not in params
        )
        start, end = int(params["startTime"]), int(params["endTime"])
        assert end - start < 7 * 86400000
        rows = [r for r in self.rows if start <= int(r["transactionTime"]) <= end]
        cursor = params.get("cursor")
        page = rows[2:] if cursor else rows[:2]
        next_cursor = "next" if not cursor and len(rows) > 2 else ""
        if self.fault == "repeated-cursor" and rows:
            next_cursor = "next"
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {"list": list(reversed(page)), "nextPageCursor": next_cursor},
            },
        )


def test_complete_account_pages_reconcile_daily_balances_and_private_boundary():
    exchange = LedgerHTTP(history())
    client = BybitClient(DEMO_BASE_URL, "invented-key", "invented-secret")
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(exchange.handle)
    )
    try:
        result = fetch_daily_wallet_history(client)
    finally:
        client.close()
    days = {day["date"]: day for day in result["days"]}
    assert len(days) == 31
    assert days["2026-10-04"]["balance"] == "1000"
    assert days["2026-10-05"]["change"] == "500"
    assert days["2026-10-06"]["balance"] == "1597"
    assert days["2026-10-06"]["change"] == "97"
    assert days["2026-10-07"]["balance"] == "1594"
    assert days["2026-10-08"]["balance"] == "1500"
    assert days["2026-10-09"]["complete"] and not days["2026-10-09"]["activity"]
    assert (
        days["2026-11-03"]["balance"] == "1500" and not days["2026-11-03"]["complete"]
    )
    assert "PRIVATE" not in str(result) and "orderId" not in str(result)
    assert result["includes_unrealized_pnl"] is False
    assert exchange.wallet_reads == 2
    ledger = [r for r in exchange.requests if r.url.path.endswith("transaction-log")]
    assert len(ledger) == 6 and any(r.url.params.get("cursor") for r in ledger)


@pytest.mark.parametrize(
    "fault", ["moving-wallet", "repeated-cursor", "borrow", "other-asset"]
)
def test_incomplete_or_changing_capture_is_unavailable(fault):
    exchange = LedgerHTTP(history(), fault=fault)
    client = BybitClient(DEMO_BASE_URL, "invented-key", "invented-secret")
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(exchange.handle)
    )
    try:
        with pytest.raises((ValueError, TradeExecutionError)):
            fetch_daily_wallet_history(client)
    finally:
        client.close()


@pytest.mark.parametrize(
    "fault", ["gap", "components", "non-usdt", "duplicate", "nan", "missing-id"]
)
def test_contradictory_cash_evidence_never_becomes_a_balance_curve(fault):
    rows = history()
    if fault == "gap":
        rows[2]["cashBalance"] = "1600"  # Missing cash change or unlogged reset.
    elif fault == "components":
        rows[0]["cashFlow"] = "501"
    elif fault == "non-usdt":
        rows[0]["currency"] = "USDC"
    elif fault == "duplicate":
        duplicate = deepcopy(rows[0])
        duplicate["cashBalance"] = "1800"
        rows.append(duplicate)
    elif fault == "nan":
        rows[0]["cashBalance"] = "NaN"
    else:
        del rows[0]["id"]
    with pytest.raises(ValueError):
        daily_wallet_history(rows, start=START, as_of=AS_OF, wallet_balance="1500")


def test_day_detail_is_not_capped_at_latest_hundred_and_cycles_are_valid():
    rows = []
    for index in range(150):
        rows.append(
            event(str(index), "2026-10-05T12:00:00+00:00", str(1000 + index), "1")
        )
    # An actual same-timestamp cycle must not be lost or counted twice.
    rows.extend(
        [
            event("cycle-a", "2026-10-05T12:00:00+00:00", "1150", "10"),
            event("cycle-b", "2026-10-05T12:00:00+00:00", "1160", "-10"),
        ]
    )
    result = daily_wallet_history(
        list(reversed(rows)) + [rows[0]],
        start=START,
        as_of=AS_OF,
        wallet_balance="1150",
    )
    day = next(d for d in result["days"] if d["date"] == "2026-10-05")
    assert day["balance"] == "1150" and day["change"] == "150"
    assert len(day["activity"]) == 152


def test_empty_ledger_does_not_invent_historical_balances():
    result = daily_wallet_history([], start=START, as_of=AS_OF, wallet_balance="1500")
    assert all(d["balance"] is None and not d["complete"] for d in result["days"][:-1])
    assert result["days"][-1]["balance"] == "1500"
    assert result["days"][-1]["change"] is None


def test_public_transport_roundtrips_every_activity_without_private_fields():
    result = daily_wallet_history(
        history(), start=START, as_of=AS_OF, wallet_balance="1500"
    )
    feed = {"schema_version": 1, "refresh_failed": False, "history": result}
    assert decode_balance_feed(encode_balance_feed(feed)) == feed
    assert "PRIVATE" not in json.dumps(result)
    day = result["days"][1]
    day["activity"][0]["orderId"] = "PRIVATE_ORDER_CANARY"
    with pytest.raises(ValueError, match="Unexpected public balance fields"):
        encode_balance_feed(feed)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate-day",
        "cash-change",
        "complete-today",
        "position-side",
        "unknown-field",
    ],
)
def test_public_contract_rejects_false_or_private_claims(fault):
    result = daily_wallet_history(
        history(), start=START, as_of=AS_OF, wallet_balance="1500"
    )
    if fault == "duplicate-day":
        result["days"][1] = deepcopy(result["days"][0])
    elif fault == "cash-change":
        result["days"][1]["change"] = "501"
    elif fault == "complete-today":
        result["days"][-1]["complete"] = True
    elif fault == "position-side":
        result["days"][1]["activity"][0]["side"] = "LONG"
    else:
        result["account_id"] = "PRIVATE_ACCOUNT_CANARY"
    with pytest.raises(ValueError):
        validate_balance_history(result)


def test_dispatch_decoder_bounds_expansion_before_json_parsing():
    encoded = base64.b64encode(gzip.compress(b" " * (MAX_BALANCE_BYTES + 1))).decode()
    with pytest.raises(ValueError, match="input withheld"):
        decode_balance_feed(encoded)


def test_retention_survives_refresh_failure_and_is_scoped_to_account(
    tmp_path, monkeypatch
):
    snapshot = daily_wallet_history(
        history(), start=START, as_of=AS_OF, wallet_balance="1500"
    )
    monkeypatch.setattr(
        balance_cache, "fetch_daily_wallet_history", lambda client: deepcopy(snapshot)
    )
    path = tmp_path / "demo-balance.json"
    args = {"api_key": "PRIVATE_KEY_CANARY", "database_path": tmp_path / "state.db"}
    first = balance_cache.collect_balance_feed(None, path, **args)
    original = path.read_bytes()
    assert not first["refresh_failed"] and first["history"] == snapshot
    assert "PRIVATE_KEY_CANARY" not in path.read_text()

    def unavailable(client):
        raise ValueError("PRIVATE_EXCHANGE_RESPONSE_CANARY")

    monkeypatch.setattr(balance_cache, "fetch_daily_wallet_history", unavailable)
    failed = balance_cache.collect_balance_feed(None, path, **args)
    assert failed == {"schema_version": 1, "refresh_failed": True, "history": snapshot}
    assert path.read_bytes() == original
    other = balance_cache.collect_balance_feed(
        None, path, api_key="different-account", database_path=args["database_path"]
    )
    assert other["history"] is None and other["refresh_failed"]
    assert path.read_bytes() == original


def test_retention_keeps_older_days_but_public_window_stays_explicit(
    tmp_path, monkeypatch
):
    snapshot = daily_wallet_history(
        history(), start=START, as_of=AS_OF, wallet_balance="1500"
    )
    monkeypatch.setattr(
        balance_cache, "fetch_daily_wallet_history", lambda client: deepcopy(snapshot)
    )
    path = tmp_path / "demo-balance.json"
    args = {"api_key": "invented-key", "database_path": tmp_path / "state.db"}
    balance_cache.collect_balance_feed(None, path, **args)
    # Next day's current window drops Oct 4. Retention must keep that prior close.
    snapshot = deepcopy(snapshot)
    snapshot["start"] = "2026-10-05T00:00:00+00:00"
    snapshot["as_of"] = "2026-11-04T12:00:00+00:00"
    snapshot["days"] = snapshot["days"][1:]
    snapshot["days"][-1]["complete"] = True
    snapshot["days"].append(
        {
            "date": "2026-11-04",
            "balance": "1500",
            "change": "0",
            "complete": False,
            "activity": [],
        }
    )
    fresh = balance_cache.collect_balance_feed(None, path, **args)
    assert not fresh["refresh_failed"]
    assert len(fresh["history"]["days"]) == 31
    saved = json.loads(path.read_text())["history"]
    assert len(saved["days"]) == 32 and saved["days"][0]["date"] == "2026-10-04"
    assert validate_balance_history(saved) == saved


def test_corrupt_retention_does_not_get_overwritten_or_published(tmp_path, monkeypatch):
    path = tmp_path / "demo-balance.json"
    path.write_text('{"history":"PRIVATE_CORRUPTION_CANARY"}')
    monkeypatch.setattr(
        balance_cache,
        "fetch_daily_wallet_history",
        lambda client: pytest.fail("Corrupt cache was ignored"),
    )
    with pytest.raises(ValueError, match="Invalid retained"):
        balance_cache.collect_balance_feed(
            None, path, api_key="invented", database_path=tmp_path / "state.db"
        )
    assert "PRIVATE_CORRUPTION_CANARY" in path.read_text()


def test_pages_installs_compressed_whitelisted_balance_without_truncation(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import install_dashboard_feed as installer

    history_value = daily_wallet_history(
        history(), start=START, as_of=AS_OF, wallet_balance="1500"
    )
    feed = {"schema_version": 1, "refresh_failed": False, "history": history_value}
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setenv("PUBLIC_BALANCE_FEED", encode_balance_feed(feed))
    installer.install_balance()
    target = tmp_path / "docs" / "balance.json"
    assert json.loads(target.read_text()) == feed
    original = target.read_bytes()
    # Encoding alone is not a privacy boundary: the decoded whitelist is checked.
    feed["history"]["account_id"] = "PRIVATE_ACCOUNT_CANARY"
    invalid = base64.b64encode(gzip.compress(json.dumps(feed).encode())).decode()
    monkeypatch.setenv("PUBLIC_BALANCE_FEED", invalid)
    with pytest.raises(SystemExit, match="input withheld"):
        installer.install_balance()
    assert target.read_bytes() == original
