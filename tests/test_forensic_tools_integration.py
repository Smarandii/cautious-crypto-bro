"""Research consumers work without the analyzer CLI beside them."""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from forensic_fixture import settled_entry_fixture


def test_research_consumers_reconcile_without_analyzer_script(tmp_path) -> None:
    root = Path(__file__).parents[1]
    scripts = tmp_path / "tools"
    scripts.mkdir()
    for name in (
        "export_dashboard_data.py",
        "replay_strategy_v2.py",
        "analyze_signal_followthrough.py",
        "analyze_long_participation.py",
    ):
        shutil.copyfile(root / "scripts" / name, scripts / name)

    bundle = tmp_path / "forensic.zip"
    rows = {
        "analysis/trade_lineage.jsonl": [
            {
                "intents": [
                    {
                        "intent_id": "entry",
                        "entry_order_ids": ["order"],
                        "intent": {
                            "symbol": "BTCUSDT",
                            "side": "LONG",
                            "stop_loss": "90",
                        },
                    }
                ]
            }
        ],
        "bybit/executions.jsonl": [
            {
                "execType": "Trade",
                "orderId": "order",
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
                "updatedTime": "2000",
                "closedSize": "2",
                "closedPnl": "17",
                "openFee": ".25",
                "closeFee": ".25",
            }
        ],
        "bybit/transaction_log.jsonl": [
            {
                "type": "SETTLEMENT",
                "symbol": "BTCUSDT",
                "transactionTime": "1500",
                "funding": "-1",
            }
        ],
        "database/position_actions.jsonl": [],
    }
    rows.update(
        settled_entry_fixture(
            rows["analysis/trade_lineage.jsonl"][0]["intents"],
            rows["bybit/executions.jsonl"],
            {"order": "2"},
            fee=".25",
        )
    )
    with zipfile.ZipFile(bundle, "w") as archive:
        for name, records in rows.items():
            archive.writestr(
                f"forensic/{name}",
                "".join(json.dumps(record) + "\n" for record in records),
            )

    program = """
import asyncio
import io
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
import export_dashboard_data as dashboard
import analyze_signal_followthrough as followthrough
import analyze_long_participation as participation
import replay_strategy_v2 as replay
from cautious_crypto_bro.storage import IntentStore

bundle, database = map(Path, sys.argv[1:])
positions = followthrough.forensic_pnl.reconcile(bundle)
assert positions == participation.reconcile(bundle)
assert positions[0]['net_pnl'] == 17
assert positions[0]['gross_pnl'] == 18.5
assert participation.summarize(positions)['expectancy_r'] == .85
assert replay.completed_position_starts(io.BytesIO(bundle.read_bytes())) == {1000}
asyncio.run(IntentStore(database).initialize())
with sqlite3.connect(database) as db:
    snapshot = dashboard.build_snapshot(
        db, database_source='synthetic', forensic_bundle=bundle,
        snapshot_at=datetime(2026, 10, 10, tzinfo=UTC),
        benchmark_provider=lambda *_: None,
        demo_long_risk_multiplier=.1, demo_long_exit_control_fraction=0,
        demo_long_participation_skip_fraction=0, demo_exit_profile='baseline',
    )
assert 'analyze_forensic_pnl' not in sys.modules
print(json.dumps(snapshot))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(bundle), str(tmp_path / "state.sqlite3")],
        cwd=scripts,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    assert snapshot["strategy_pnl"]["net_pnl_usdt"] == 17
