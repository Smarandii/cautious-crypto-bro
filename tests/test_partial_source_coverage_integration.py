"""A successful sibling must not hide incomplete post participation."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_remaining_concerns import make_intent

from cautious_crypto_bro.domain import PositionActionIntent, PositionActionType
from cautious_crypto_bro.source_coverage import coverage_summary
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration
REPO = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "kinds,statuses,second_mode,expected",
    [
        (("entry", "entry"), ("EXECUTED", "FAILED"), "MANUAL", "PARTIAL_EXECUTION"),
        (("entry", "entry"), ("FAILED", "EXECUTED"), "MANUAL", "PARTIAL_EXECUTION"),
        (("action", "action"), ("EXECUTED", "SKIPPED"), "MANUAL", "PARTIAL_EXECUTION"),
        (("action", "action"), ("SKIPPED", "EXECUTED"), "MANUAL", "PARTIAL_EXECUTION"),
        (("entry", "action"), ("EXECUTED", "FAILED"), "MANUAL", "PARTIAL_EXECUTION"),
        (("entry", "action"), ("FAILED", "EXECUTED"), "MANUAL", "PARTIAL_EXECUTION"),
        (("entry", "action"), ("EXECUTED", "PENDING"), "SKIPPED", "PARTIAL_EXECUTION"),
        (("entry", "action"), ("EXECUTED", "PENDING"), "MANUAL", "AWAITING_DECISION"),
        (("entry", "action"), ("EXECUTED", "PENDING"), "AUTO", "UNRESOLVED"),
        (("entry", "action"), ("EXECUTED", "UNCERTAIN"), "MANUAL", "UNRESOLVED"),
        (("entry", "action"), ("EXECUTED", "EXECUTED"), "MANUAL", "EXECUTED"),
        (("entry", "action"), ("FAILED", "FAILED"), "MANUAL", "FAILED"),
        (("entry", "action"), ("SKIPPED", "SKIPPED"), "MANUAL", "SKIPPED"),
    ],
)
def test_stored_sibling_outcomes_remain_visible(
    tmp_path, kinds, statuses, second_mode, expected
):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        source = make_intent().source.model_copy(
            update={"text": "Private synthetic two-instruction post"}
        )
        token = await store.claim_source(source, lease_seconds=300)
        decision = {
            "extraction": {"intents": [], "position_actions": []},
            "normalized": {"open_intents": [], "position_actions": []},
        }
        for index, (kind, status) in enumerate(zip(kinds, statuses, strict=True)):
            record = (
                make_intent().model_copy(
                    update={"source": source, "intent_id": uuid4()}
                )
                if kind == "entry"
                else PositionActionIntent(
                    source=source,
                    symbol="BTCUSDT",
                    action=PositionActionType.CLOSE,
                    summary="Synthetic close instruction",
                    confidence=1,
                )
            )
            table, id_column, field = (
                ("intents", "intent_id", "open_intents")
                if kind == "entry"
                else ("position_actions", "action_id", "position_actions")
            )
            record_id = getattr(record, id_column)
            decision["normalized"][field].append(record.model_dump(mode="json"))
            with sqlite3.connect(path) as db:
                db.execute(
                    f"INSERT INTO {table}({id_column},channel_id,message_id,payload_json,"
                    "status,approval_mode,created_at,updated_at) "
                    "VALUES (?,?,?,?,'PENDING',?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                    (
                        str(record_id),
                        source.channel_id,
                        source.message_id,
                        record.model_dump_json(),
                        second_mode if index else "MANUAL",
                    ),
                )
            if status == "EXECUTED":
                if kind == "entry":
                    assert await store.claim_for_execution(record_id, 1)
                    await store.mark_executed(record_id, ("synthetic-order",))
                else:
                    assert await store.claim_position_action_for_execution(record_id, 1)
                    await store.complete_position_action(record, "synthetic-action")
            elif status == "FAILED":
                method = (
                    store.mark_failed
                    if kind == "entry"
                    else store.mark_position_action_failed
                )
                await method(record_id, "Synthetic ownership rejection")
            elif status == "SKIPPED":
                method = (
                    store.mark_skipped
                    if kind == "entry"
                    else store.mark_position_action_skipped
                )
                assert await method(record_id, 1)
            elif status == "UNCERTAIN":
                await store.mark_position_action_uncertain(
                    record_id, record.symbol, None, "Synthetic lost acknowledgement"
                )
        assert await store.record_source_decision(source, token, json.dumps(decision))
        assert await store.mark_source_completed(source, token)
        before = path.read_bytes()
        result = await coverage_summary(
            path, since=datetime.now(UTC) - timedelta(days=1)
        )
        assert path.read_bytes() == before
        assert result["dispositions"] == {expected: 1}
        assert sum(result["linked_record_statuses"].values()) == 2
        assert result["missing_original_decisions"] == 0
        serialized = json.dumps(result)
        assert source.text not in serialized and "BTCUSDT" not in serialized
        if kinds == ("entry", "action") and statuses == ("EXECUTED", "FAILED"):
            process = subprocess.run(
                [
                    sys.executable,
                    "scripts/report_source_coverage.py",
                    "--database",
                    str(path),
                ],
                cwd=REPO,
                env=os.environ | {"PYTHONPATH": str(REPO / "src")},
                capture_output=True,
                text=True,
                check=True,
            )
            assert json.loads(process.stdout) == result
            assert not process.stderr and path.read_bytes() == before

    asyncio.run(run())
