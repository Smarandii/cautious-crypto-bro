"""Export a read-only production snapshot for docs/data.json.

Run from the repository root after the worker has synced its SQLite state:
    python scripts/export_dashboard_data.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
database = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "cautious_crypto_bro.sqlite3"
output = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs" / "data.json"

with sqlite3.connect(database) as db:
    def count(table: str) -> int:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    pnl = [float(row[0]) for row in db.execute("SELECT closed_pnl FROM account_closed_pnl")]
    policy = db.execute("SELECT risk_per_trade_pct FROM execution_policy WHERE id = 1").fetchone()
    sync = db.execute("SELECT history_start_at, last_synced_at FROM account_pnl_sync WHERE id = 1").fetchone()
    statuses = dict(db.execute("SELECT status, COUNT(*) FROM intents GROUP BY status").fetchall())
    table_counts = {
        table: count(table)
        for table in ("source_messages", "intents", "execution_plans", "position_strategies", "position_actions")
    }

snapshot = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "source": str(database),
    "engineering": {
        "source_messages": table_counts["source_messages"],
        "intents": table_counts["intents"],
        "plans": table_counts["execution_plans"],
        "active_strategies": table_counts["position_strategies"],
        "position_actions": table_counts["position_actions"],
        "intent_statuses": statuses,
        "execution_success_pct": round(statuses.get("EXECUTED", 0) / sum(statuses.values()) * 100, 2) if statuses else None,
        "failure_pct": round(statuses.get("FAILED", 0) / sum(statuses.values()) * 100, 2) if statuses else None,
    },
    "funnel": {
        "signals": table_counts["source_messages"],
        "intents": table_counts["intents"],
        "plans": table_counts["execution_plans"],
        "actions": table_counts["position_actions"],
        "strategies": table_counts["position_strategies"],
    },
    "risk": {"risk_per_trade_pct": float(policy[0]) if policy else None},
    "pnl": {
        "available": bool(sync),
        "realized_pnl_usdt": round(sum(pnl), 8),
        "record_count": len(pnl),
        "positive_count": sum(value > 0 for value in pnl),
        "negative_count": sum(value < 0 for value in pnl),
        "history_start_at": sync[0] if sync else None,
        "last_synced_at": sync[1] if sync else None,
        "win_rate_pct": round((sum(value > 0 for value in pnl) / len(pnl) * 100), 2) if pnl else None,
        "avg_win_usdt": round(sum(value for value in pnl if value > 0) / sum(value > 0 for value in pnl), 8) if any(value > 0 for value in pnl) else None,
        "avg_loss_usdt": round(sum(value for value in pnl if value < 0) / sum(value < 0 for value in pnl), 8) if any(value < 0 for value in pnl) else None,
        "avg_win_to_loss_ratio": round((sum(value for value in pnl if value > 0) / sum(value > 0 for value in pnl)) / abs(sum(value for value in pnl if value < 0) / sum(value < 0 for value in pnl)), 4) if any(value > 0 for value in pnl) and any(value < 0 for value in pnl) else None,
        "profit_factor": round(sum(value for value in pnl if value > 0) / abs(sum(value for value in pnl if value < 0)), 4) if any(value < 0 for value in pnl) else None,
        "expectancy_usdt": round(sum(pnl) / len(pnl), 8) if pnl else None,
    },
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
data_js = ROOT / "docs" / "data.js"
data_js.write_text("window.DASHBOARD_DATA = " + json.dumps(snapshot) + ";\n", encoding="utf-8")
print(f"wrote {output}")
