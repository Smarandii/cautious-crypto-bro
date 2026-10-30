"""Public rows retain whole-trade reconciliation without leaking lineage."""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pytest
from forensic_fixture import settled_entry_fixture

from cautious_crypto_bro.dashboard_metrics import public_trade_history
from cautious_crypto_bro.forensic_pnl import reconcile

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import install_dashboard_feed as installer  # noqa: E402
from export_trade_feed import build_trade_feed  # noqa: E402

from cautious_crypto_bro.dashboard_feed import (  # noqa: E402
    public_feed_from_snapshot,
    validate_public_feed,
)


@pytest.mark.integration
def test_partial_closes_publish_one_complete_trade_without_private_lineage(tmp_path):
    bundle = tmp_path / "trades.zip"
    private = "PRIVATE_SOURCE_CANARY"
    lineage = {
        "intents": [
            {
                "intent_id": private,
                "entry_order_ids": [private],
                "intent": {"symbol": "BTCUSDT", "side": "LONG", "stop_loss": "90"},
                "metadata": {"risk_per_trade_pct": 1, "source_channel_id": private},
            }
        ]
    }
    rows = {
        "analysis/trade_lineage.jsonl": [lineage],
        "bybit/executions.jsonl": [
            {
                "execType": "Trade",
                "orderId": private,
                "execTime": "1000",
                "execQty": "2",
                "execPrice": "100",
                "closedSize": "0",
            }
        ],
        "bybit/closed_pnl.jsonl": [
            {
                "symbol": "BTCUSDT",
                "side": "Sell",
                "updatedTime": str(time),
                "closedSize": "1",
                "closedPnl": str(pnl),
                "openFee": "0",
                "closeFee": "0",
            }
            for time, pnl in [(2000, 3), (3000, 5)]
        ],
        "bybit/transaction_log.jsonl": [],
    }
    rows.update(
        settled_entry_fixture(
            lineage["intents"], rows["bybit/executions.jsonl"], {private: "2"}
        )
    )
    rows["metadata.json"][0].update(
        created_at="2026-10-29T01:00:00+00:00",
        closed_pnl_as_of="2026-10-28T23:00:00+00:00",
    )
    with ZipFile(bundle, "w") as archive:
        for name, records in rows.items():
            archive.writestr(
                "forensic/" + name, "".join(json.dumps(r) + "\n" for r in records)
            )

    positions = reconcile(bundle)
    result = public_trade_history(positions, available=True)
    assert result["total_completed"] == 1
    assert result["rows"] == [
        {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quantity": 2.0,
            "net_pnl_usdt": 8.0,
            "net_r": 0.4,
            "closed_at": "1970-01-01T00:00:03+00:00",
            "risk_per_trade_pct": 1.0,
        }
    ]
    assert private not in json.dumps(result)
    with closing(sqlite3.connect(":memory:")) as database:
        database.executescript(
            "CREATE TABLE execution_policy(id INTEGER, risk_per_trade_pct REAL); INSERT INTO execution_policy VALUES(1,3);"
        )
        feed = build_trade_feed(
            database,
            bundle,
            {
                "long_risk_multiplier": 1.0,
                "long_momentum_gate_enabled": False,
                "long_participation_skip_fraction": 0.0,
                "portfolio_stop_risk_cap_usdt": 4000.0,
                "exit_profile": "payoff_early_tight_trail_long_015",
            },
        )
    assert feed["risk"]["risk_per_trade_pct"] == 3
    assert feed["trade_history"]["rows"][0]["risk_per_trade_pct"] == 1
    assert feed["trade_history"]["as_of"] == "2026-10-28T23:00:00+00:00"
    assert private not in json.dumps(feed)
    assert validate_public_feed(feed) == feed


def test_public_history_preserves_unknown_and_unavailable_values():
    position = {"complete": True, "symbol": "BTCUSDT", "side": "SHORT"}
    result = public_trade_history([position, {"complete": False}], available=True)
    assert result["total_completed"] == 1
    assert result["rows"][0]["net_r"] is None
    assert result["rows"][0]["quantity"] is None
    assert result["rows"][0]["closed_at"] is None
    assert public_trade_history([], available=False)["total_completed"] is None
    with pytest.raises(ValueError, match="positive"):
        public_trade_history([], available=True, limit=0)


@pytest.mark.parametrize("unexpected", ["source_text", "order_id", "api_secret"])
def test_publication_rejects_extra_fields_and_nonfinite_numbers(unexpected):
    history = public_trade_history([], available=True)
    history["as_of"] = None
    feed = public_feed_from_snapshot(
        {
            "generated_at": "2026-10-29T00:00:00+00:00",
            "risk": {"source_text": "PRIVATE_CANARY"},
            "trade_history": history,
        }
    )
    assert "PRIVATE_CANARY" not in json.dumps(feed)
    feed[unexpected] = "PRIVATE_CANARY"
    with pytest.raises(ValueError, match="fields"):
        validate_public_feed(feed)
    del feed[unexpected]
    feed["risk"]["risk_per_trade_pct"] = float("nan")
    with pytest.raises(ValueError, match="number"):
        validate_public_feed(feed)


def test_pages_installer_writes_only_validated_public_payload(tmp_path, monkeypatch):
    history = public_trade_history([], available=True)
    history["as_of"] = None
    feed = public_feed_from_snapshot(
        {
            "generated_at": "2026-10-29T00:00:00+00:00",
            "risk": {},
            "trade_history": history,
        }
    )
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setenv("PUBLIC_TRADE_FEED", json.dumps(feed))
    installer.main()
    target = tmp_path / "docs" / "trades.json"
    assert json.loads(target.read_text()) == feed
    feed["source_text"] = "PRIVATE_CANARY"
    monkeypatch.setenv("PUBLIC_TRADE_FEED", json.dumps(feed))
    before = target.read_bytes()
    with pytest.raises(SystemExit, match="validation failed"):
        installer.main()
    assert target.read_bytes() == before


def test_bounded_rows_are_newest_first_and_omissions_are_explicit():
    positions = [
        {
            "complete": True,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "closed_at": ms,
            "net_pnl": ms,
        }
        for ms in [1000, 3000, 2000]
    ]
    result = public_trade_history(positions, available=True, limit=2)
    assert [row["net_pnl_usdt"] for row in result["rows"]] == [3000, 2000]
    assert result["total_completed"] == 3
    assert result["omitted_count"] == 1
