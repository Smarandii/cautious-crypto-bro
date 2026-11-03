"""Private terminal observations survive history expiry without weakening proof."""

import asyncio
import json
import os
import subprocess
import sys
import zipfile

import pytest
from test_ordinary_entry_settlement_integration import legacy_attempt
from test_partial_entry_forensics_integration import executed_attempt, export_attempt

from cautious_crypto_bro import terminal_entry_evidence as retention
from cautious_crypto_bro.dashboard_metrics import public_trade_history
from cautious_crypto_bro.domain import EntryType, Side
from cautious_crypto_bro.forensic_pnl import reconcile
from cautious_crypto_bro.state_ownership import state_ownership

pytestmark = pytest.mark.integration


def omit_unfilled_scale_ins(pages):
    pages["/v5/order/history"] = [
        row
        for row in pages["/v5/order/history"]
        if not row["orderLinkId"].endswith(("-e2", "-e3"))
    ]


def test_missing_historical_terminal_facts_are_never_backfilled_from_pnl(
    tmp_path, monkeypatch
):
    database = tmp_path / "state.sqlite3"
    exchange, _ = legacy_attempt(database, Side.LONG, EntryType.MARKET, True)
    evidence = tmp_path / "private.json"
    output = tmp_path / "unknown.zip"
    export_attempt(
        database,
        exchange,
        output,
        monkeypatch,
        omit_unfilled_scale_ins,
        terminal_evidence_path=evidence,
    )
    position = reconcile(output)[0]
    assert position["closed_fraction"] == pytest.approx(1)
    assert position["entry_identity_complete"] is True
    assert position["complete"] is False
    assert len(json.loads(evidence.read_text())["observations"]) == 1


@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("fill_scale_ins", [False, True])
def test_quantity_split_requests_keep_exact_fills_and_economics_after_expiry(
    tmp_path, monkeypatch, side, fill_scale_ins
):
    database = tmp_path / "state.sqlite3"
    exchange, _, _, _ = asyncio.run(
        executed_attempt(database, side, "", True, fill_scale_ins=fill_scale_ins)
    )
    evidence = tmp_path / "private.json"
    first = tmp_path / "first.zip"
    export_attempt(
        database, exchange, first, monkeypatch, terminal_evidence_path=evidence
    )
    original = reconcile(first)[0]
    assert original["complete"] is True
    observations = json.loads(evidence.read_text())["observations"]
    assert len(observations) > 3

    def omit_entries(pages):
        pages["/v5/order/history"] = [
            r for r in pages["/v5/order/history"] if r["reduceOnly"]
        ]

    later = tmp_path / "later.zip"
    export_attempt(
        database,
        exchange,
        later,
        monkeypatch,
        omit_entries,
        terminal_evidence_path=evidence,
    )
    restored = reconcile(later)[0]
    assert restored["complete"] is True
    for key in ("net_pnl", "fees", "funding", "entry_qty", "closed_qty"):
        assert restored[key] == original[key]


def test_parallel_evidence_owner_cannot_change_cache_or_produce_archive(
    tmp_path, monkeypatch
):
    database = tmp_path / "state.sqlite3"
    exchange, _ = legacy_attempt(database, Side.LONG, EntryType.MARKET, True)
    evidence = tmp_path / "private.json"
    export_attempt(
        database,
        exchange,
        tmp_path / "first.zip",
        monkeypatch,
        terminal_evidence_path=evidence,
    )
    original = evidence.read_bytes()
    output = tmp_path / "competing.zip"
    with state_ownership(evidence):
        with pytest.raises(RuntimeError, match="already owned"):
            export_attempt(
                database, exchange, output, monkeypatch, terminal_evidence_path=evidence
            )
    assert evidence.read_bytes() == original and not output.exists()


@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("entry_type", list(EntryType))
def test_terminal_scale_ins_survive_two_exports_and_exporter_restart(
    tmp_path, monkeypatch, side, entry_type
):
    database = tmp_path / "state.sqlite3"
    exchange, _ = legacy_attempt(database, side, entry_type, True)
    evidence = tmp_path / "private-evidence.json"
    first = tmp_path / "first.zip"
    export_attempt(
        database, exchange, first, monkeypatch, terminal_evidence_path=evidence
    )
    assert reconcile(first)[0]["complete"] is True
    second = tmp_path / "expired.zip"
    export_attempt(
        database,
        exchange,
        second,
        monkeypatch,
        omit_unfilled_scale_ins,
        terminal_evidence_path=evidence,
    )
    assert reconcile(second)[0]["complete"] is True
    assert (
        public_trade_history(reconcile(second), available=True)["total_completed"] == 1
    )
    text = evidence.read_text(encoding="utf-8")
    assert "synthetic-key" not in text and "synthetic-secret" not in text
    assert "complete" not in text and "net_pnl" not in text
    if os.name != "nt":
        assert evidence.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(second) as archive:
        provenance = archive.read("forensic/analysis/retained_terminal_entries.jsonl")
        assert len(provenance.splitlines()) == 2
        assert (
            json.loads(archive.read("forensic/metadata.json"))[
                "retained_terminal_entry_rows"
            ]
            == 2
        )
    # Research exports retain their read-only default and never borrow this cache.
    uncached = tmp_path / "uncached.zip"
    export_attempt(database, exchange, uncached, monkeypatch, omit_unfilled_scale_ins)
    assert reconcile(uncached)[0]["complete"] is False


@pytest.mark.parametrize(
    "gap",
    [
        "pending",
        "fresh_pending",
        "conflict",
        "new_fill",
        "fee",
        "identity",
        "quantity",
        "future",
    ],
)
def test_fresh_contradiction_revokes_retained_proof_even_after_it_disappears(
    tmp_path, monkeypatch, gap
):
    database = tmp_path / "state.sqlite3"
    exchange, _ = legacy_attempt(database, Side.LONG, EntryType.MARKET, True)
    evidence = tmp_path / "private.json"
    export_attempt(
        database,
        exchange,
        tmp_path / "initial.zip",
        monkeypatch,
        terminal_evidence_path=evidence,
    )

    def contradict(pages):
        order = next(
            o.copy()
            for o in pages["/v5/order/history"]
            if o["orderLinkId"].endswith("-e2")
        )
        omit_unfilled_scale_ins(pages)
        if gap == "pending":
            pages["/v5/order/realtime"].append(order | {"orderStatus": "New"})
        elif gap == "fresh_pending":
            pages["/v5/order/history"].append(order | {"orderStatus": "New"})
        elif gap == "conflict":
            pages["/v5/order/history"].extend([order, order | {"orderStatus": "New"}])
        elif gap == "new_fill":
            original = pages["/v5/execution/list"][0]
            pages["/v5/execution/list"].append(
                original
                | {
                    "execId": "later-scale-in-fill",
                    "orderId": order["orderId"],
                    "orderLinkId": order["orderLinkId"],
                    "execQty": "1",
                }
            )
        elif gap == "fee":
            del pages["/v5/execution/list"][0]["execFee"]
        elif gap == "identity":
            pages["/v5/order/history"].append(order | {"symbol": "ETHUSDT"})
        elif gap == "quantity":
            pages["/v5/order/history"].append(order | {"qty": "999999"})
        elif gap == "future":
            pages["/v5/order/history"].append(order | {"updatedTime": "9999999999999"})

    contradicted = tmp_path / "contradicted.zip"
    export_attempt(
        database,
        exchange,
        contradicted,
        monkeypatch,
        contradict,
        terminal_evidence_path=evidence,
    )
    position = reconcile(contradicted)[0]
    assert position["complete"] is False
    assert position["entry_orders_settled"] is False

    def omit_entries(pages):
        pages["/v5/order/history"] = [
            r for r in pages["/v5/order/history"] if r["reduceOnly"]
        ]

    later = tmp_path / "later.zip"
    export_attempt(
        database,
        exchange,
        later,
        monkeypatch,
        omit_entries,
        terminal_evidence_path=evidence,
    )
    assert reconcile(later)[0]["complete"] is False


@pytest.mark.parametrize("damage", ["json", "scope", "version", "future", "shape"])
def test_corruption_and_scope_mismatch_fail_before_archive_or_publication(
    tmp_path, monkeypatch, damage
):
    database = tmp_path / "state.sqlite3"
    exchange, _ = legacy_attempt(database, Side.LONG, EntryType.MARKET, True)
    evidence = tmp_path / "private.json"
    export_attempt(
        database,
        exchange,
        tmp_path / "initial.zip",
        monkeypatch,
        terminal_evidence_path=evidence,
    )
    data = json.loads(evidence.read_text())
    if damage == "scope":
        data["scope"] = "another-account-or-database"
    elif damage == "version":
        data["version"] = 99
    elif damage == "future":
        next(iter(data["observations"].values()))["captured_at_ms"] = 9999999999999
    elif damage == "shape":
        data["observations"] = []
    evidence.write_text("{" if damage == "json" else json.dumps(data), encoding="utf-8")
    output = tmp_path / "refused.zip"
    with pytest.raises(
        retention.TerminalEvidenceUnavailable, match="private terminal-entry evidence"
    ):
        export_attempt(
            database,
            exchange,
            output,
            monkeypatch,
            omit_unfilled_scale_ins,
            terminal_evidence_path=evidence,
        )
    assert not output.exists()


def test_failed_atomic_revocation_cannot_reuse_the_previous_good_file(
    tmp_path, monkeypatch
):
    database = tmp_path / "state.sqlite3"
    exchange, _ = legacy_attempt(database, Side.LONG, EntryType.MARKET, True)
    evidence = tmp_path / "private.json"
    export_attempt(
        database,
        exchange,
        tmp_path / "initial.zip",
        monkeypatch,
        terminal_evidence_path=evidence,
    )
    original = evidence.read_bytes()

    def unavailable(*args):
        raise OSError("Synthetic atomic replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(retention.os, "replace", unavailable)
        with pytest.raises(OSError):
            export_attempt(
                database,
                exchange,
                tmp_path / "failed.zip",
                monkeypatch,
                terminal_evidence_path=evidence,
            )
    assert evidence.read_bytes() == original
    assert evidence.with_name(evidence.name + ".pending").exists()
    with pytest.raises(RuntimeError, match="requires review"):
        export_attempt(
            database,
            exchange,
            tmp_path / "restart.zip",
            monkeypatch,
            omit_unfilled_scale_ins,
            terminal_evidence_path=evidence,
        )
    assert not (tmp_path / "failed.zip").exists()
    assert not (tmp_path / "restart.zip").exists()


def test_killed_exporter_leaves_durable_interruption_guard(tmp_path, monkeypatch):
    database = tmp_path / "state.sqlite3"
    exchange, _ = legacy_attempt(database, Side.LONG, EntryType.MARKET, True)
    evidence = tmp_path / "private.json"
    export_attempt(
        database,
        exchange,
        tmp_path / "initial.zip",
        monkeypatch,
        terminal_evidence_path=evidence,
    )
    original = evidence.read_bytes()
    data = json.loads(original)
    requests = [
        (r["expected"], r["symbol"], r["side"]) for r in data["observations"].values()
    ]
    arguments = tmp_path / "child-input.json"
    arguments.write_text(
        json.dumps(
            {
                "scope": data["scope"],
                "requests": requests,
                "history": [],
                "current": [],
                "fills": {},
                "as_of_ms": 2000000000000,
            }
        ),
        encoding="utf-8",
    )
    code = """import json,os,sys
from pathlib import Path
from cautious_crypto_bro.terminal_entry_evidence import retain_terminal_entries
os.replace=lambda *args: os._exit(73)
retain_terminal_entries(Path(sys.argv[1]), **json.loads(Path(sys.argv[2]).read_text()))
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(evidence), str(arguments)],
        capture_output=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert child.returncode == 73
    assert evidence.read_bytes() == original
    with pytest.raises(RuntimeError, match="requires review"):
        export_attempt(
            database,
            exchange,
            tmp_path / "restart.zip",
            monkeypatch,
            omit_unfilled_scale_ins,
            terminal_evidence_path=evidence,
        )
    assert not (tmp_path / "restart.zip").exists()
