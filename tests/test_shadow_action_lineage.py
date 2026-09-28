"""Offline exchange-proof reconciliation of shadow lifecycle actions."""

import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

spec = importlib.util.spec_from_file_location(
    "shadow_action_lineage",
    Path(__file__).parents[1] / "scripts" / "reconcile_shadow_actions.py",
)
assert spec is not None and spec.loader is not None
lineage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lineage)

START = 1_800_000_000_000  # Synthetic, aligned to a minute.
END = START + 600_000
ACTION_ID = "11111111-1111-1111-1111-111111111111"
LINK = f"ccb-action-{UUID(ACTION_ID).hex[:24]}"


def inputs(*, side="LONG"):
    return [
        {
            "case": {
                "id": "near-example",
                "symbol": "NEARUSDT",
                "side": side,
                "channel_id": 101,
                "start_ms": START,
                "end_ms": END,
                "actions": [],
            },
            "bars": [],
        }
    ]


def snapshot(*, side="LONG"):
    return {
        "db": {
            "position_actions": [
                {
                    "action_id": ACTION_ID,
                    "channel_id": 101,
                    "bybit_order_id": "order-1",
                    "created_at": datetime.fromtimestamp(
                        (START - 60_000) / 1000, UTC
                    ).isoformat(),
                    "status": "EXECUTED",
                    "payload_json": json.dumps(
                        {
                            "symbol": "NEARUSDT",
                            "action": "REDUCE",
                            "expected_side": side,
                            "close_pct": 25,
                        }
                    ),
                }
            ]
        },
        "exchange": {
            "fills": [
                {
                    "execId": "fill-1",
                    "execType": "Trade",
                    "orderId": "order-1",
                    "orderLinkId": LINK,
                    "symbol": "NEARUSDT",
                    "side": "Sell" if side == "LONG" else "Buy",
                    "execTime": str(START + 20_000),
                    "execQty": "5",
                    "closedSize": "5",
                }
            ],
            "orders": [
                {
                    "orderId": "order-1",
                    "orderLinkId": LINK,
                    "symbol": "NEARUSDT",
                    "side": "Sell" if side == "LONG" else "Buy",
                    "orderStatus": "Filled",
                    "qty": "5",
                }
            ],
        },
    }


def events(corrected):
    return [event for item in corrected for event in item["case"]["actions"]]


def assert_unresolved(corrected, audit):
    assert events(corrected) == []
    assert audit["applied"] == []
    assert audit["unresolved"]
    case_ids = {item["case"]["id"] for item in corrected}
    assert all(
        item["case_id"] in case_ids
        and item["action_id"] == ACTION_ID
        and item["reason"]
        for item in audit["unresolved"]
    )


def test_skewed_near_action_uses_execution_time_once_despite_failed_status():
    original, evidence = inputs(), snapshot()
    evidence["db"]["position_actions"][0]["status"] = "FAILED"
    original_before, evidence_before = deepcopy(original), deepcopy(evidence)

    corrected, audit = lineage.reconcile_actions(original, evidence)

    assert original == original_before
    assert evidence == evidence_before
    assert corrected is not original
    assert corrected[0]["case"] is not original[0]["case"]
    assert len(events(corrected)) == 1
    event = events(corrected)[0]
    assert {key: event[key] for key in ("ts", "action", "close_pct", "action_id")} == {
        "ts": START + 20_000,
        "action": "REDUCE",
        "close_pct": 25,
        "action_id": ACTION_ID,
    }
    assert len(audit["applied"]) == 1
    assert audit["unresolved"] == []
    repeated, _ = lineage.reconcile_actions(corrected, evidence)
    assert events(repeated) == events(corrected)


@pytest.mark.parametrize("match,side", [("stored_id", "LONG"), ("exact_link", "SHORT")])
def test_matching_identity_deduplicates_fills_before_summing(match, side):
    evidence = snapshot(side=side)
    first = evidence["exchange"]["fills"][0]
    first.update(execQty="2", closedSize="2")
    if match == "stored_id":
        first["orderLinkId"] = ""
    else:
        evidence["db"]["position_actions"][0]["bybit_order_id"] = None
    second = {
        **first,
        "execId": "fill-2",
        "execTime": str(START + 40_000),
        "execQty": "3",
        "closedSize": "3",
    }
    evidence["exchange"]["fills"] = [second, first, deepcopy(first)]

    corrected, audit = lineage.reconcile_actions(inputs(side=side), evidence)

    assert len(events(corrected)) == 1
    assert events(corrected)[0]["ts"] == START + 20_000
    assert audit["unresolved"] == []


def test_action_is_bound_to_matching_symbol_and_source_case():
    cases = inputs()
    for case_id, symbol, channel_id in (
        ("other-source", "NEARUSDT", 202),
        ("other-symbol", "ATOMUSDT", 101),
    ):
        item = deepcopy(cases[0])
        item["case"].update(id=case_id, symbol=symbol, channel_id=channel_id)
        cases.append(item)

    corrected, audit = lineage.reconcile_actions(cases, snapshot())

    assert [len(item["case"]["actions"]) for item in corrected] == [1, 0, 0]
    assert len(audit["applied"]) == 1
    assert audit["unresolved"] == []


def test_missing_fill_is_unresolved_without_database_time_fallback():
    evidence = snapshot()
    evidence["db"]["position_actions"][0]["created_at"] = datetime.fromtimestamp(
        (START + 20_000) / 1000, UTC
    ).isoformat()
    evidence["exchange"]["fills"] = []

    corrected, audit = lineage.reconcile_actions(inputs(), evidence)

    assert_unresolved(corrected, audit)


def test_case_end_is_exclusive_for_matching_execution():
    evidence = snapshot()
    evidence["exchange"]["fills"][0]["execTime"] = str(END)

    corrected, audit = lineage.reconcile_actions(inputs(), evidence)

    assert events(corrected) == []
    assert audit["applied"] == []


@pytest.mark.parametrize(
    "kind",
    [
        "partial",
        "missing_order",
        "cross_minute",
        "cross_case",
        "after_last_replay_minute",
    ],
)
def test_incomplete_or_split_execution_is_unresolved(kind):
    cases, evidence = inputs(), snapshot()
    if kind == "partial":
        evidence["exchange"]["orders"][0].update(
            orderStatus="PartiallyFilled", qty="10"
        )
    elif kind == "missing_order":
        evidence["exchange"]["orders"] = []
    elif kind == "after_last_replay_minute":
        evidence["exchange"]["fills"][0]["execTime"] = str(END - 1000)
    else:
        first = evidence["exchange"]["fills"][0]
        first.update(execQty="2", closedSize="2")
        second = {**first, "execId": "fill-2", "execQty": "3", "closedSize": "3"}
        if kind == "cross_minute":
            first["execTime"] = str(START + 59_000)
            second["execTime"] = str(START + 61_000)
        else:
            second["execTime"] = str(START + 40_000)
            later = deepcopy(cases[0])
            cases[0]["case"]["end_ms"] = START + 30_000
            later["case"].update(id="near-later", start_ms=START + 30_000)
            cases.append(later)
        evidence["exchange"]["fills"] = [first, second]

    corrected, audit = lineage.reconcile_actions(cases, evidence)

    assert_unresolved(corrected, audit)


@pytest.mark.parametrize(
    "corruption",
    [
        "symbol",
        "side",
        "closed_size",
        "order_link",
        "conflicting_exec_id",
        "multiple_orders",
    ],
)
def test_corrupted_matching_fill_evidence_fails_closed(corruption):
    evidence = snapshot()
    fill = evidence["exchange"]["fills"][0]
    if corruption == "symbol":
        fill["symbol"] = "ATOMUSDT"
    elif corruption == "side":
        fill["side"] = "Buy"
    elif corruption == "closed_size":
        fill["closedSize"] = "0"
    elif corruption == "order_link":
        fill["orderLinkId"] = "unrelated-link"
    elif corruption == "conflicting_exec_id":
        evidence["exchange"]["fills"].append(
            {**fill, "execQty": "4", "closedSize": "4"}
        )
    else:
        evidence["exchange"]["fills"].append(
            {**fill, "execId": "fill-2", "orderId": "order-2"}
        )

    with pytest.raises(ValueError):
        lineage.reconcile_actions(inputs(), evidence)


def test_runner_refuses_existing_output_before_reading_inputs(tmp_path):
    output = tmp_path / "existing-output"
    output.mkdir()

    # Both input paths are absent: any attempt to read them would fail first.
    with pytest.raises(ValueError, match="new directory"):
        lineage.run(tmp_path / "missing-run", tmp_path / "missing-engine.py", output)

    assert list(output.iterdir()) == []


def test_runner_rejects_engine_hash_before_loading_engine(tmp_path):
    run_dir = tmp_path / "frozen-run"
    run_dir.mkdir()
    files = {
        "config.json": {"engine_sha256": lineage.ENGINE_SHA256},
        "snapshot.json": {},
        "inputs.json": [],
        "summary.json": {},
        "actual_ledger.json": [],
    }
    for name, value in files.items():
        (run_dir / name).write_text(json.dumps(value), encoding="utf-8")
    engine = tmp_path / "untrusted-engine.py"
    engine.write_text(
        "raise AssertionError('Engine must not execute before hash verification')\n",
        encoding="utf-8",
    )
    output = tmp_path / "new-output"

    with pytest.raises(ValueError, match="Frozen engine hash mismatch"):
        lineage.run(run_dir, engine, output)

    assert not output.exists()
