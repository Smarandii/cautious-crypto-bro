"""A closed primary does not finish a strategy with pending ordinary entries."""

import asyncio
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime

import pytest
from test_partial_entry_forensics_integration import executed_attempt, export_attempt

from cautious_crypto_bro.dashboard_metrics import public_trade_history
from cautious_crypto_bro.domain import EntryType, Side
from cautious_crypto_bro.forensic_pnl import reconcile

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("entry_type", list(EntryType))
@pytest.mark.parametrize("settled", [True, False])
def test_legacy_ordinary_primary_close_requires_settled_scale_ins(
    tmp_path, monkeypatch, side, entry_type, settled
):
    database = tmp_path / "legacy.sqlite3"
    exchange, plan = legacy_attempt(database, side, entry_type, settled)
    bundle = tmp_path / "legacy.zip"
    _, _, pnl, funding = export_attempt(database, exchange, bundle, monkeypatch)
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr("forensic/market_1m/BTCUSDT.jsonl", "")
    positions = reconcile(bundle)
    assert len(positions) == 1
    position = positions[0]
    assert position["closed_fraction"] == pytest.approx(1)
    assert position["net_pnl"] == pytest.approx(float(pnl["closedPnl"]))
    assert position["fees"] == pytest.approx(
        float(pnl["openFee"]) + float(pnl["closeFee"])
    )
    assert position["funding"] == pytest.approx(float(funding))
    assert position["complete"] is settled
    assert public_trade_history(positions, available=True)["total_completed"] == int(
        settled
    )
    from export_dashboard_data import build_snapshot

    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        snapshot = build_snapshot(
            db,
            database_source="integration",
            forensic_bundle=bundle,
            snapshot_at=datetime.now(UTC),
            benchmark_provider=lambda *_: None,
        )
    assert snapshot["strategy_pnl"]["position_count"] == int(settled)
    assert position["intent_id"] == str(plan.intent_id)


def legacy_attempt(database, side, entry_type, settled, **kwargs):
    exchange, plan, _, _ = asyncio.run(
        executed_attempt(
            database,
            side,
            "",
            True,
            entry_type=entry_type,
            ceiling="1000000",
            settle_entries=settled,
            fill_primary=entry_type is not EntryType.MARKET,
            **kwargs,
        )
    )
    # Model the deployed pre-v2.2.40 format, using actual successful request IDs.
    # Only optional ownership metadata is absent; request geometry is unchanged.
    with closing(sqlite3.connect(database)) as db:
        payload = json.loads(
            db.execute("SELECT payload_json FROM execution_plans").fetchone()[0]
        )
        del payload["entry_slices"]
        db.execute("UPDATE execution_plans SET payload_json=?", (json.dumps(payload),))
        db.execute("DELETE FROM zone_orders")
        db.commit()
    return exchange, plan


@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("fill_mode", ["full", "partial"])
@pytest.mark.parametrize("fill_scale_ins", [True, False])
def test_legacy_partial_primary_retains_economics_without_guessing_submitted_qty(
    tmp_path, monkeypatch, side, fill_mode, fill_scale_ins
):
    database = tmp_path / "partial.sqlite3"
    exchange, _ = legacy_attempt(
        database,
        side,
        EntryType.MARKET,
        True,
        fill_mode=fill_mode,
        fill_scale_ins=fill_scale_ins,
    )
    bundle = tmp_path / "partial.zip"
    _, _, pnl, _ = export_attempt(database, exchange, bundle, monkeypatch)
    position = reconcile(bundle)[0]
    assert position["net_pnl"] == pytest.approx(float(pnl["closedPnl"]))
    assert position["complete"] is (fill_mode == "full")
    assert position["fill_pattern"] == ("E1+E2+E3" if fill_scale_ins else "E1")


@pytest.mark.parametrize(
    "gap",
    [
        "legacy",
        "missing_flag",
        "missing_history",
        "missing_snapshot",
        "missing_time",
        "naive_time",
        "stale_snapshot",
        "history_before_close",
        "future_history",
        "conflicting_history",
        "pending_snapshot",
        "missing_fee",
    ],
)
def test_reconciler_requires_captured_settlement_proof(tmp_path, monkeypatch, gap):
    database = tmp_path / "evidence.sqlite3"
    exchange, _ = legacy_attempt(database, Side.LONG, EntryType.MARKET, True)
    bundle = tmp_path / "evidence.zip"
    export_attempt(database, exchange, bundle, monkeypatch)
    original = reconcile(bundle)[0]
    assert original["complete"] is True
    with zipfile.ZipFile(bundle) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    root = "forensic/"
    lineage_name = root + "analysis/trade_lineage.jsonl"
    metadata_name = root + "metadata.json"
    history_name = root + "bybit/entry_order_history.jsonl"
    pending_name = root + "bybit/open_entry_orders.jsonl"
    metadata = json.loads(files[metadata_name])
    lineage = json.loads(files[lineage_name])
    item = lineage["intents"][0]
    history = [json.loads(line) for line in files[history_name].splitlines()]
    if gap == "legacy":
        del item["entry_evidence"]
    elif gap == "missing_flag":
        del item["entry_evidence"]["entry_orders_settled"]
    elif gap == "missing_history":
        del files[history_name]
    elif gap == "missing_snapshot":
        del files[pending_name]
    elif gap == "missing_time":
        del metadata["open_entry_orders_as_of"]
    elif gap == "naive_time":
        metadata["open_entry_orders_as_of"] = "2026-01-01T00:00:00"
    elif gap == "stale_snapshot":
        metadata["open_entry_orders_as_of"] = "2026-01-01T00:00:00+00:00"
    elif gap == "history_before_close":
        metadata["entry_order_history_as_of"] = "2026-01-01T00:00:00+00:00"
    elif gap == "future_history":
        history[0]["updatedTime"] = "9999999999999"
    elif gap == "conflicting_history":
        history.append(history[0] | {"orderStatus": "New"})
    elif gap == "pending_snapshot":
        files[pending_name] = json.dumps(history[1] | {"orderStatus": "New"}).encode()
    elif gap == "missing_fee":
        name = root + "bybit/executions.jsonl"
        rows = [json.loads(line) for line in files[name].splitlines()]
        del rows[0]["execFee"]
        files[name] = "\n".join(json.dumps(row) for row in rows).encode()
    if history_name in files:
        files[history_name] = "\n".join(json.dumps(row) for row in history).encode()
    files[metadata_name] = json.dumps(metadata).encode()
    files[lineage_name] = json.dumps(lineage).encode()
    with zipfile.ZipFile(bundle, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    position = reconcile(bundle)[0]
    assert position["complete"] is False
    assert position["net_pnl"] == original["net_pnl"]
    assert position["fees"] == original["fees"]
    assert position["funding"] == original["funding"]
    assert position["entry_settlement_status"] == (
        "unknown" if gap == "legacy" else "unresolved"
    )
    assert public_trade_history([position], available=True)["total_completed"] == 0
