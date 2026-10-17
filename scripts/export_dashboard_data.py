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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_forensic_pnl import (  # noqa: E402
    DEMO_EXIT_PROFILE_REVIEW_CASES,
    DEMO_LONG_EXIT_AB_CONTROL_PROFILE,
    DEMO_LONG_EXIT_AB_TREATMENT_PROFILE,
    DEMO_PAYOFF_EXIT_PROFILE,
    LIVE_LONG_RISK_EXIT_PROFILE,
    LIVE_LONG_RISK_STARTED_AT,
    PROSPECTIVE_SHADOW_REVIEW_CASES,
    bootstrap_performance_intervals,
    jsonl,
    read_open_entry_orders_snapshot,
    read_open_positions_snapshot,
    reconcile,
)
from analyze_signal_followthrough import fill_followthrough_summary  # noqa: E402

from cautious_crypto_bro.bybit.normalize import (  # noqa: E402
    closed_pnl_record_from_item,
)
from cautious_crypto_bro.dashboard_metrics import (  # noqa: E402
    summarize_intent_outcomes,
    summarize_open_positions,
    summarize_portfolio_stop_risk,
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


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def forensic_closed_pnl_updates(
    bundle: Path | None,
    *,
    last_synced_at: str | None,
) -> tuple[list, str | None]:
    """Return archive Closed-PnL rows newer than the covered cache watermark."""
    if bundle is None or last_synced_at is None:
        return [], None

    with ZipFile(bundle) as archive:
        names = set(archive.namelist())
        if not names:
            return [], None
        root = archive.namelist()[0].split("/", 1)[0]
        metadata_path = f"{root}/metadata.json"
        lineage_path = f"{root}/analysis/trade_lineage.jsonl"
        closed_pnl_path = f"{root}/bybit/closed_pnl.jsonl"
        if not {metadata_path, lineage_path, closed_pnl_path} <= names:
            return [], None

        metadata = json.loads(archive.read(metadata_path))
        watermark = _utc_datetime(last_synced_at)
        coverage_start = metadata.get("closed_pnl_coverage_start")
        if coverage_start is None:
            lineage = jsonl(archive, root, "analysis/trade_lineage.jsonl")
            plan_times = [
                intent["created_at"]
                for group in lineage
                for intent in group.get("intents", [])
                if intent.get("created_at")
            ]
            if not plan_times:
                return [], None
            coverage_start = (
                min(_utc_datetime(value) for value in plan_times) - timedelta(days=1)
            ).isoformat()

        coverage_end = metadata.get("closed_pnl_as_of") or metadata.get("created_at")
        if not coverage_end:
            return [], None
        start_at = _utc_datetime(coverage_start)
        end_at = _utc_datetime(coverage_end)
        if not start_at <= watermark <= end_at:
            return [], None

        return (
            [
                record
                for item in jsonl(archive, root, "bybit/closed_pnl.jsonl")
                if (record := closed_pnl_record_from_item(item)).updated_at > watermark
            ],
            end_at.isoformat(),
        )


def _account_pnl_history(
    database: sqlite3.Connection,
    forensic_bundle: Path | None,
    last_synced_at: str | None,
) -> tuple[list[float], str | None, int]:
    records = {
        str(record_id): (float(closed_pnl), _utc_datetime(closed_at))
        for record_id, closed_pnl, closed_at in database.execute(
            "SELECT record_id, closed_pnl, closed_at FROM account_closed_pnl"
        )
    }
    updates, archive_as_of = forensic_closed_pnl_updates(
        forensic_bundle,
        last_synced_at=last_synced_at,
    )
    supplemental_count = 0
    for update in updates:
        existing = records.get(update.record_id)
        if existing is None or update.updated_at > existing[1]:
            records[update.record_id] = (
                float(update.closed_pnl),
                update.updated_at,
            )
            supplemental_count += 1

    return (
        [value for value, _updated_at in records.values()],
        archive_as_of if supplemental_count else None,
        supplemental_count,
    )


def build_snapshot(
    database: sqlite3.Connection,
    *,
    database_source: str,
    forensic_bundle: Path | None,
    snapshot_at: datetime,
    benchmark_provider=btc_buy_hold,
    demo_long_risk_multiplier: float | None = None,
    demo_long_exit_control_fraction: float | None = None,
    demo_long_participation_skip_fraction: float | None = None,
    demo_exit_profile: str | None = None,
    demo_portfolio_stop_risk_cap_usdt: float | None = None,
) -> dict[str, object]:
    policy = database.execute(
        "SELECT risk_per_trade_pct FROM execution_policy WHERE id = 1"
    ).fetchone()
    if demo_long_risk_multiplier is None:
        demo_long_risk_multiplier = float(
            os.environ.get("DEMO_LONG_RISK_MULTIPLIER", "1")
        )
    if demo_exit_profile is None:
        demo_exit_profile = os.environ.get("DEMO_EXIT_PROFILE", "baseline")
    if demo_long_exit_control_fraction is None:
        demo_long_exit_control_fraction = float(
            os.environ.get("DEMO_LONG_EXIT_CONTROL_FRACTION", "0")
        )
    if demo_long_participation_skip_fraction is None:
        demo_long_participation_skip_fraction = float(
            os.environ.get("DEMO_LONG_PARTICIPATION_SKIP_FRACTION", "0")
        )
    sync = database.execute(
        "SELECT history_start_at, last_synced_at FROM account_pnl_sync WHERE id = 1"
    ).fetchone()
    account_records, account_archive_as_of, supplemental_account_records = (
        _account_pnl_history(
            database,
            forensic_bundle,
            sync[1] if sync else None,
        )
    )
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
    open_entry_orders, open_entry_orders_as_of, unmatched_entry_order_count = (
        read_open_entry_orders_snapshot(forensic_bundle)
        if forensic_bundle is not None
        else (None, None, None)
    )
    open_entry_order_details = build_open_entry_order_details(
        database, open_entry_orders, snapshot_at
    )
    account_open_positions = summarize_open_positions(open_positions)
    account_open_positions["as_of"] = open_positions_as_of
    account_open_positions["snapshot_available"] = open_positions_as_of is not None
    portfolio_stop_risk = summarize_portfolio_stop_risk(
        open_positions,
        open_entry_orders,
        cap_usdt=demo_portfolio_stop_risk_cap_usdt,
        unmatched_entry_order_count=unmatched_entry_order_count,
    )
    portfolio_stop_risk["snapshot_available"] = (
        open_positions_as_of is not None
        and open_entry_orders is not None
        and open_entry_orders_as_of is not None
        and unmatched_entry_order_count is not None
    )
    portfolio_stop_risk["as_of"] = open_entry_orders_as_of or open_positions_as_of
    account_open_positions["portfolio_stop_risk"] = portfolio_stop_risk
    account_open_positions["open_entry_order_details"] = open_entry_order_details
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
    participation_experiment = _demo_long_participation_experiment(
        database,
        reconciled_positions,
        skip_fraction=demo_long_participation_skip_fraction,
        forensic_available=forensic_bundle is not None,
    )
    exit_experiment = _demo_exit_experiment(
        database,
        reconciled_positions,
        profile=demo_exit_profile,
        forensic_available=forensic_bundle is not None,
    )
    if (
        demo_exit_profile == "payoff_early_tight_trail_long_015"
        and demo_long_exit_control_fraction > 0
    ):
        exit_experiment["randomized_long_ab"] = {
            "active": True,
            "control_fraction": demo_long_exit_control_fraction,
            "assignment": "stable SHA-256 bucket of intent ID",
            "control": _demo_exit_experiment(
                database,
                reconciled_positions,
                profile=DEMO_LONG_EXIT_AB_CONTROL_PROFILE,
                forensic_available=forensic_bundle is not None,
            ),
            "treatment": _demo_exit_experiment(
                database,
                reconciled_positions,
                profile=DEMO_LONG_EXIT_AB_TREATMENT_PROFILE,
                forensic_available=forensic_bundle is not None,
            ),
        }

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
            "demo_long_participation_experiment": participation_experiment,
            "demo_exit_experiment": exit_experiment,
        },
        "benchmark": benchmark_provider(
            sync[0] if sync else None,
            snapshot_at,
        ),
        "strategy_pnl": strategy_pnl,
        "fill_followthrough": (
            fill_followthrough_summary(forensic_bundle)
            if forensic_bundle is not None
            else None
        ),
        "account_open_positions": account_open_positions,
        "account_pnl_records": {
            **account_pnl_record_metrics(account_records),
            "history_start_at": sync[0] if sync else None,
            "last_synced_at": sync[1] if sync else None,
            "forensic_archive_as_of": account_archive_as_of,
            "forensic_archive_supplement_count": supplemental_account_records,
        },
    }
    return snapshot


def build_open_entry_order_details(
    database: sqlite3.Connection,
    orders: list[dict] | None,
    snapshot_at: datetime,
) -> list[dict] | None:
    """Attach saved TTL and age to tracked live entry orders, without guessing TTLs."""
    if orders is None:
        return None
    try:
        rows = database.execute(
            "SELECT bybit_order_ids_json, created_at, payload_json FROM execution_plans"
        )
    except sqlite3.OperationalError:
        return []

    plan_by_order_id: dict[str, tuple[datetime, int | None]] = {}
    for order_ids_json, created_at, payload_json in rows:
        try:
            plan = json.loads(payload_json)
            order_ids = json.loads(order_ids_json or "[]")
            ttl = (plan.get("policy", {}).get("strategy_v2") or {}).get(
                "entry_order_ttl_minutes"
            )
            ttl = int(ttl) if ttl is not None and int(ttl) > 0 else None
            created = datetime.fromisoformat(str(created_at)).astimezone(UTC)
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
            continue
        for order_id in order_ids:
            plan_by_order_id[str(order_id)] = (created, ttl)

    details = []
    for order in orders:
        order_id = str(order.get("orderId") or "")
        matched = plan_by_order_id.get(order_id)
        created = None
        ttl = None
        if matched:
            created, ttl = matched
        else:
            created_ms = order.get("createdTime")
            try:
                created = datetime.fromtimestamp(int(created_ms) / 1000, UTC)
            except (TypeError, ValueError, OSError):
                pass
        age_minutes = (
            max(0, int((snapshot_at.astimezone(UTC) - created).total_seconds() // 60))
            if created is not None
            else None
        )
        ttl_status = (
            "no_ttl"
            if ttl is None
            else "past_ttl"
            if age_minutes is not None and age_minutes > ttl
            else "within_ttl"
        )
        details.append(
            {
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "qty": order.get("leavesQty", order.get("qty")),
                "status": order.get("orderStatus"),
                "age_minutes": age_minutes,
                "ttl_minutes": ttl,
                "ttl_status": ttl_status,
            }
        )
    return details


def _demo_exit_experiment(
    database: sqlite3.Connection,
    positions: list[dict],
    *,
    profile: str,
    forensic_available: bool,
) -> dict[str, object]:
    active = profile != "baseline"
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
        if strategy.get("exit_profile") == profile:
            planned_count += 1
            executed_count += row[1] == "EXECUTED"

    filled = [
        position for position in positions if position.get("exit_profile") == profile
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


def _demo_long_participation_experiment(
    database: sqlite3.Connection,
    positions: list[dict],
    *,
    skip_fraction: float,
    forensic_available: bool,
) -> dict[str, object]:
    assignments: list[dict[str, object]] = []
    for row in database.execute(
        """
        SELECT plans.intent_id, plans.payload_json, plans.bybit_order_ids_json,
               intents.status, intents.approval_mode
        FROM execution_plans AS plans
        JOIN intents USING (intent_id)
        """
    ):
        try:
            plan = json.loads(row[1])
            strategy = plan["policy"]["strategy_v2"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        arm = strategy.get("long_participation_arm")
        if plan.get("side") == "LONG" and arm in {"take", "skip"}:
            assignments.append(
                {
                    "intent_id": row[0],
                    "arm": arm,
                    "order_ids": json.loads(row[2] or "[]"),
                    "status": row[3],
                    "approval_mode": row[4],
                }
            )

    take = [item for item in assignments if item["arm"] == "take"]
    skip = [item for item in assignments if item["arm"] == "skip"]
    take_ids = {item["intent_id"] for item in take}
    take_positions = [
        position
        for position in positions
        if position.get("intent_id") in take_ids
        and position.get("side") == "LONG"
        and position.get("long_participation_arm") == "take"
    ]
    complete_take = [position for position in take_positions if position["complete"]]
    failed_take = sum(
        item["status"] == "FAILED" and not item["order_ids"] for item in take
    )
    unresolved_take = max(0, len(take) - len(complete_take) - failed_take)
    valid_skips = [
        item
        for item in skip
        if item["status"] == "SKIPPED"
        and item["approval_mode"] == "SKIPPED"
        and not item["order_ids"]
    ]
    valid_takes = all(item["approval_mode"] == "AUTO" for item in take)
    status = (
        "inactive"
        if skip_fraction <= 0
        else "awaiting_forensic_data"
        if not forensic_available
        else "ready_for_review"
        if len(take) >= 20
        and len(skip) >= 20
        and unresolved_take == 0
        and len(valid_skips) == len(skip)
        and valid_takes
        else "collecting"
    )
    return {
        "active": skip_fraction > 0,
        "skip_fraction": skip_fraction,
        "assignment": "stable SHA-256 bucket of intent ID, independent salt",
        "planned_count": len(assignments),
        "take_assigned_count": len(take),
        "skip_assigned_count": len(skip),
        "skip_records_valid_count": len(valid_skips),
        "take_filled_position_count": len(take_positions)
        if forensic_available
        else None,
        "take_completed_position_count": len(complete_take)
        if forensic_available
        else None,
        "take_unresolved_count": unresolved_take if forensic_available else None,
        "review_target_per_arm": 20,
        "status": status,
        "take_completed_performance": summarize_reconciled_positions(complete_take),
        "take_completed_performance_bootstrap_95ci": (
            bootstrap_performance_intervals(complete_take) if complete_take else None
        ),
        "skip_portfolio_pnl_usdt": 0.0,
        "comparison_note": (
            "Skip arm is zero exposure by design. Do not compare arm expectancy "
            "until take assignments are resolved and the per-arm review target is met."
        ),
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
                and plan.get("policy", {}).get("strategy_v2", {}).get("exit_profile")
                == LIVE_LONG_RISK_EXIT_PROFILE
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
        and position.get("exit_profile") == LIVE_LONG_RISK_EXIT_PROFILE
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
        "exit_profile": LIVE_LONG_RISK_EXIT_PROFILE,
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
    parser.add_argument(
        "--demo-long-risk-multiplier",
        type=float,
        default=float(os.environ.get("DEMO_LONG_RISK_MULTIPLIER", "1")),
        help="Current Demo LONG risk multiplier (defaults to the environment)",
    )
    parser.add_argument(
        "--demo-long-exit-control-fraction",
        type=float,
        default=float(os.environ.get("DEMO_LONG_EXIT_CONTROL_FRACTION", "0")),
        help="Randomized LONG exit control share (defaults to the environment)",
    )
    parser.add_argument(
        "--demo-long-participation-skip-fraction",
        type=float,
        default=float(os.environ.get("DEMO_LONG_PARTICIPATION_SKIP_FRACTION", "0")),
        help="Randomized LONG skip share (defaults to the environment)",
    )
    parser.add_argument(
        "--demo-exit-profile",
        choices=(
            "baseline",
            "payoff_challenger",
            DEMO_PAYOFF_EXIT_PROFILE,
            "payoff_early_tight_trail",
            "payoff_early_tight_trail_long_015",
            DEMO_LONG_EXIT_AB_TREATMENT_PROFILE,
            DEMO_LONG_EXIT_AB_CONTROL_PROFILE,
        ),
        default=os.environ.get("DEMO_EXIT_PROFILE", "baseline"),
        help="Current Demo exit profile (defaults to the environment)",
    )
    portfolio_cap = os.environ.get("DEMO_PORTFOLIO_STOP_RISK_CAP_USDT")
    parser.add_argument(
        "--demo-portfolio-stop-risk-cap-usdt",
        type=float,
        default=float(portfolio_cap) if portfolio_cap else None,
        help="Combined Demo stop-risk cap (defaults to the environment)",
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
            demo_long_risk_multiplier=args.demo_long_risk_multiplier,
            demo_long_exit_control_fraction=args.demo_long_exit_control_fraction,
            demo_long_participation_skip_fraction=(
                args.demo_long_participation_skip_fraction
            ),
            demo_exit_profile=args.demo_exit_profile,
            demo_portfolio_stop_risk_cap_usdt=args.demo_portfolio_stop_risk_cap_usdt,
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
