"""Export an account snapshot and optional reconciled V2 position metrics.

Run from the repository root after syncing the Docker SQLite state. Supply a
forensic bundle to publish whole-position trading metrics; closed-PnL rows are
kept separately because partial exits are not independent trades.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_forensic_pnl import (  # noqa: E402
    DEMO_EXIT_PROFILE_REVIEW_CASES,
    DEMO_PAYOFF_EXIT_PROFILE,
    LIVE_LONG_RISK_STARTED_AT,
    PROSPECTIVE_SHADOW_REVIEW_CASES,
    bootstrap_performance_intervals,
    read_open_positions_snapshot,
    reconcile,
)

from cautious_crypto_bro.dashboard_metrics import (  # noqa: E402
    summarize_intent_outcomes,
    summarize_open_positions,
    summarize_position_sides,
    summarize_reconciled_positions,
)


def btc_buy_hold(history_start_at: str | None, snapshot_at: datetime) -> dict | None:
    if not history_start_at:
        return None
    start = datetime.fromisoformat(history_start_at).astimezone(UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(snapshot_at.timestamp() * 1000)
    rows: list[list[str]] = []
    cursor = start_ms
    hour_ms = 60 * 60 * 1000
    while cursor <= end_ms:
        query = urlencode(
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "interval": "60",
                "start": cursor,
                "end": min(cursor + hour_ms * 999, end_ms),
                "limit": 1000,
            }
        )
        with urlopen(
            f"https://api-demo.bybit.com/v5/market/kline?{query}", timeout=10
        ) as response:
            payload = json.load(response)
        batch = payload["result"]["list"]
        if not batch:
            break
        rows.extend(batch)
        last_ms = max(int(row[0]) for row in batch)
        if last_ms < cursor:
            break
        cursor = last_ms + hour_ms
    rows = sorted({row[0]: row for row in rows}.values(), key=lambda row: int(row[0]))
    if not rows:
        return None
    start_price = float(rows[0][1])
    end_price = float(rows[-1][4])
    return {
        "symbol": "BTCUSDT",
        "interval": "60m",
        "start_at": datetime.fromtimestamp(int(rows[0][0]) / 1000, UTC).isoformat(),
        "end_at": datetime.fromtimestamp(int(rows[-1][0]) / 1000, UTC).isoformat(),
        "start_price_usdt": round(start_price, 2),
        "end_price_usdt": round(end_price, 2),
        "return_pct": round((end_price / start_price - 1) * 100, 4),
        "hypothetical_1000_usdt_pnl": round(1000 * (end_price / start_price - 1), 2),
    }


def account_pnl_record_metrics(records: list[float]) -> dict[str, object]:
    winners = [value for value in records if value > 0]
    losers = [value for value in records if value < 0]
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    return {
        "available": bool(records),
        "record_count": len(records),
        "positive_count": len(winners),
        "negative_count": len(losers),
        "realized_pnl_usdt": round(sum(records), 8),
        "win_rate_pct": round(len(winners) / len(records) * 100, 2)
        if records
        else None,
        "avg_win_usdt": round(gross_wins / len(winners), 8) if winners else None,
        "avg_loss_usdt": round(sum(losers) / len(losers), 8) if losers else None,
        "avg_win_to_loss_ratio": round(
            (gross_wins / len(winners)) / (gross_losses / len(losers)), 4
        )
        if winners and losers
        else None,
        "profit_factor": round(gross_wins / gross_losses, 4) if gross_losses else None,
        "expectancy_usdt": round(sum(records) / len(records), 8) if records else None,
    }


def build_snapshot(
    database: sqlite3.Connection,
    *,
    database_source: str,
    forensic_bundle: Path | None,
    snapshot_at: datetime,
    benchmark_provider=btc_buy_hold,
    demo_long_risk_multiplier: float | None = None,
    demo_exit_profile: str | None = None,
) -> dict[str, object]:
    account_records = [
        float(row[0])
        for row in database.execute("SELECT closed_pnl FROM account_closed_pnl")
    ]
    policy = database.execute(
        "SELECT risk_per_trade_pct FROM execution_policy WHERE id = 1"
    ).fetchone()
    if demo_long_risk_multiplier is None:
        demo_long_risk_multiplier = float(
            os.environ.get("DEMO_LONG_RISK_MULTIPLIER", "1")
        )
    if demo_exit_profile is None:
        demo_exit_profile = os.environ.get("DEMO_EXIT_PROFILE", "baseline")
    sync = database.execute(
        "SELECT history_start_at, last_synced_at FROM account_pnl_sync WHERE id = 1"
    ).fetchone()
    statuses = dict(
        database.execute("SELECT status, COUNT(*) FROM intents GROUP BY status")
    )
    failure_reasons = [
        str(row[0] or "")
        for row in database.execute("SELECT error FROM intents WHERE status = 'FAILED'")
    ]
    table_counts = {
        table: int(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "source_messages",
            "intents",
            "execution_plans",
            "position_strategies",
            "position_actions",
        )
    }

    reconciled_positions = []
    complete_positions = []
    incomplete_position_count = None
    if forensic_bundle is not None:
        reconciled_positions = reconcile(forensic_bundle)
        complete_positions = [
            position for position in reconciled_positions if position["complete"]
        ]
        incomplete_position_count = len(reconciled_positions) - len(complete_positions)

    strategy_pnl = summarize_reconciled_positions(complete_positions)
    strategy_pnl["incomplete_position_count"] = incomplete_position_count
    strategy_pnl["by_side"] = summarize_position_sides(complete_positions)
    strategy_pnl["performance_bootstrap_95ci"] = (
        bootstrap_performance_intervals(complete_positions)
        if complete_positions
        else None
    )
    open_positions, open_positions_as_of = (
        read_open_positions_snapshot(forensic_bundle)
        if forensic_bundle is not None
        else ([], None)
    )
    account_open_positions = summarize_open_positions(open_positions)
    account_open_positions["as_of"] = open_positions_as_of
    account_open_positions["snapshot_available"] = open_positions_as_of is not None
    if open_positions_as_of is not None and not open_positions:
        account_open_positions["unrealized_pnl_usdt"] = 0.0
        account_open_positions["estimated_stop_risk_usdt"] = 0.0

    risk_experiment = _demo_long_risk_experiment(
        database,
        reconciled_positions,
        base_risk_pct=float(policy[0]) if policy else None,
        multiplier=demo_long_risk_multiplier,
        forensic_available=forensic_bundle is not None,
    )
    exit_experiment = _demo_exit_experiment(
        database,
        reconciled_positions,
        profile=demo_exit_profile,
        forensic_available=forensic_bundle is not None,
    )

    total_intents = sum(statuses.values())
    snapshot = {
        "generated_at": snapshot_at.isoformat(),
        "source": database_source,
        "engineering": {
            "source_messages": table_counts["source_messages"],
            "intents": table_counts["intents"],
            "plans": table_counts["execution_plans"],
            "active_strategies": table_counts["position_strategies"],
            "position_actions": table_counts["position_actions"],
            "intent_statuses": statuses,
            "intent_outcomes": summarize_intent_outcomes(statuses, failure_reasons),
            "execution_success_pct": round(
                statuses.get("EXECUTED", 0) / total_intents * 100, 2
            )
            if total_intents
            else None,
            "failure_pct": round(statuses.get("FAILED", 0) / total_intents * 100, 2)
            if total_intents
            else None,
        },
        "funnel": {
            "signals": table_counts["source_messages"],
            "intents": table_counts["intents"],
            "plans": table_counts["execution_plans"],
            "actions": table_counts["position_actions"],
            "strategies": table_counts["position_strategies"],
        },
        "risk": {
            "risk_per_trade_pct": float(policy[0]) if policy else None,
            "demo_long_experiment": risk_experiment,
            "demo_exit_experiment": exit_experiment,
        },
        "benchmark": benchmark_provider(
            sync[0] if sync else None,
            snapshot_at,
        ),
        "strategy_pnl": strategy_pnl,
        "account_open_positions": account_open_positions,
        "account_pnl_records": {
            **account_pnl_record_metrics(account_records),
            "history_start_at": sync[0] if sync else None,
            "last_synced_at": sync[1] if sync else None,
        },
    }
    return snapshot


def _demo_exit_experiment(
    database: sqlite3.Connection,
    positions: list[dict],
    *,
    profile: str,
    forensic_available: bool,
) -> dict[str, object]:
    active = profile == DEMO_PAYOFF_EXIT_PROFILE
    planned_count = 0
    executed_count = 0
    for row in database.execute(
        """
        SELECT plans.payload_json, intents.status
        FROM execution_plans AS plans
        LEFT JOIN intents USING (intent_id)
        """
    ):
        try:
            plan = json.loads(row[0])
            strategy = plan["policy"]["strategy_v2"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if strategy.get("exit_profile") == DEMO_PAYOFF_EXIT_PROFILE:
            planned_count += 1
            executed_count += row[1] == "EXECUTED"

    filled = [
        position
        for position in positions
        if position.get("exit_profile") == DEMO_PAYOFF_EXIT_PROFILE
    ]
    complete = [position for position in filled if position.get("complete") is True]
    status = (
        "inactive"
        if not active
        else "awaiting_forensic_data"
        if not forensic_available
        else "ready_for_review"
        if len(complete) >= DEMO_EXIT_PROFILE_REVIEW_CASES
        else "collecting"
    )
    return {
        "active": active,
        "profile": profile,
        "planned_count": planned_count,
        "executed_plan_count": executed_count,
        "filled_position_count": len(filled) if forensic_available else None,
        "completed_position_count": len(complete) if forensic_available else None,
        "review_target_completed_positions": DEMO_EXIT_PROFILE_REVIEW_CASES,
        "status": status,
        "performance": summarize_reconciled_positions(complete),
        "performance_bootstrap_95ci": bootstrap_performance_intervals(complete)
        if complete
        else None,
    }


def _demo_long_risk_experiment(
    database: sqlite3.Connection,
    positions: list[dict],
    *,
    base_risk_pct: float | None,
    multiplier: float,
    forensic_available: bool,
) -> dict[str, object]:
    active = multiplier < 1 and base_risk_pct is not None
    target_risk_pct = base_risk_pct * multiplier if active else None
    start = datetime.fromisoformat(LIVE_LONG_RISK_STARTED_AT).astimezone(UTC)
    start_ms = int(start.timestamp() * 1000)
    planned_count = 0
    executed_count = 0
    if active and target_risk_pct is not None:
        for row in database.execute(
            """
            SELECT plans.payload_json, plans.created_at, intents.status
            FROM execution_plans AS plans
            LEFT JOIN intents USING (intent_id)
            """
        ):
            try:
                plan = json.loads(row[0])
                created_at = datetime.fromisoformat(row[1]).astimezone(UTC)
                risk_pct = float(plan["policy"]["risk_per_trade_pct"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                plan.get("side") == "LONG"
                and created_at > start
                and abs(risk_pct - target_risk_pct) <= 1e-9
            ):
                planned_count += 1
                executed_count += row[2] == "EXECUTED"

    experiment_positions = [
        position
        for position in positions
        if active
        and target_risk_pct is not None
        and position.get("side") == "LONG"
        and _position_start_ms(position) is not None
        and _position_start_ms(position) > start_ms
        and position.get("risk_per_trade_pct") is not None
        and abs(float(position["risk_per_trade_pct"]) - target_risk_pct) <= 1e-9
    ]
    complete_experiment_positions = [
        position
        for position in experiment_positions
        if position.get("complete") is True
    ]
    status = (
        "inactive"
        if not active
        else "awaiting_forensic_data"
        if not forensic_available
        else "ready_for_review"
        if len(complete_experiment_positions) >= PROSPECTIVE_SHADOW_REVIEW_CASES
        else "collecting"
    )
    return {
        "active": active,
        "multiplier": multiplier,
        "base_risk_pct": base_risk_pct,
        "effective_long_risk_pct": target_risk_pct,
        "started_at": LIVE_LONG_RISK_STARTED_AT,
        "planned_count": planned_count,
        "executed_plan_count": executed_count,
        "filled_position_count": len(experiment_positions)
        if forensic_available
        else None,
        "completed_position_count": len(complete_experiment_positions)
        if forensic_available
        else None,
        "review_target_completed_longs": PROSPECTIVE_SHADOW_REVIEW_CASES,
        "status": status,
        "performance": summarize_reconciled_positions(complete_experiment_positions),
    }


def _position_start_ms(position: dict) -> int | None:
    value = position.get("start")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).astimezone(UTC).timestamp() * 1000)
        except ValueError:
            return None
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        default=Path(
            os.environ.get(
                "DATABASE_PATH", ROOT / "data" / "cautious_crypto_bro.sqlite3"
            )
        ),
        help="SQLite snapshot (defaults to DATABASE_PATH or the local data database)",
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=ROOT / "docs" / "data.json",
        help="Output JSON path; a sibling .js file is written for GitHub Pages",
    )
    parser.add_argument(
        "--forensic-bundle",
        type=Path,
        help="Bybit Demo forensic archive used to reconcile complete V2 positions",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_uri = f"{args.database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as database:
        snapshot = build_snapshot(
            database,
            database_source="read-only SQLite snapshot",
            forensic_bundle=args.forensic_bundle,
            snapshot_at=datetime.now(UTC),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    json_content = json.dumps(snapshot, indent=2) + "\n"
    args.output.write_text(json_content, encoding="utf-8")
    javascript_path = args.output.with_suffix(".js")
    javascript_path.write_text(
        "window.DASHBOARD_DATA = " + json.dumps(snapshot) + ";\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output}")
    print(f"wrote {javascript_path}")


if __name__ == "__main__":
    main()
