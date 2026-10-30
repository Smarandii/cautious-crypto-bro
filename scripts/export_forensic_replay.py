from __future__ import annotations

import argparse
import json
import sqlite3
import time
import zipfile
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from cautious_crypto_bro.bybit.client import DEMO_BASE_URL, BybitClient
from cautious_crypto_bro.bybit.normalize import account_order_from_item
from cautious_crypto_bro.config import get_settings
from cautious_crypto_bro.domain import ExecutionPlan, TradeExecutionError
from cautious_crypto_bro.entry_settlement import entry_orders_settled
from cautious_crypto_bro.zone_ledger import ZoneOrder

WINDOW = timedelta(days=7)
KLINE_CHUNK_MS = 1_000 * 60_000
MARKET_CANDLE_LOOKBACK_MINUTES = 240
MARKET_CANDLE_LOOKBACK_MS = MARKET_CANDLE_LOOKBACK_MINUTES * 60_000


@dataclass(frozen=True)
class ForensicAccess:
    database_path: Path
    bybit_api_key: str = field(repr=False)
    bybit_api_secret: str = field(repr=False)


@dataclass(frozen=True)
class _EntryAttempt:
    row: sqlite3.Row
    plan: dict
    entry_ids: tuple[str, ...]
    primary_ids: tuple[str, ...]
    owned_ids: tuple[str, ...]
    evidence: dict | None


def _bound_orders(database: sqlite3.Connection) -> dict[str, list[ZoneOrder]]:
    by_zone: dict[str, list[ZoneOrder]] = defaultdict(list)
    for row in database.execute(
        "SELECT zone_id,order_id,order_link_id,payload_json FROM zone_orders"
    ):
        order = ZoneOrder.model_validate_json(row["payload_json"])
        if (str(order.zone_id), order.order_id, order.order_link_id or None) != (
            row["zone_id"],
            row["order_id"],
            row["order_link_id"],
        ):
            raise ValueError("Zone order payload contradicts its durable identity")
        by_zone[str(order.zone_id)].append(order)
    return by_zone


def _entry_attempt(
    row: sqlite3.Row, bindings: list[ZoneOrder], strategy_status: str | None
) -> _EntryAttempt | None:
    plan = json.loads(row["payload_json"])
    saved_ids = tuple(json.loads(row["bybit_order_ids_json"] or "[]"))
    if plan.get("strategy_version", 1) != 2:
        return None
    if not any(order.role == "ENTRY" for order in bindings) and not (
        row["status"] == "EXECUTED" and saved_ids
    ):
        return None
    frozen = ExecutionPlan.model_validate(plan)
    if str(frozen.intent_id) != row["intent_id"]:
        raise ValueError("Stored entry plan has conflicting intent identity")
    prefix = f"ccb-v2-{frozen.intent_id.hex[:20]}-"
    expected = {prefix + order.name.lower(): order for order in frozen.entry_orders}
    legs = {s.name: s.leg_name for s in frozen.entry_slices} or {
        order.name: order.name for order in frozen.orders
    }
    by_link = dict(zip(expected, saved_ids, strict=True)) if saved_ids else {}
    for binding in bindings:
        if binding.role != "ENTRY":
            continue
        submitted = expected.get(binding.order_link_id)
        if submitted is None or submitted.quantity != binding.quantity:
            raise ValueError(
                "Bound entry is inconsistent with its frozen physical request"
            )
        previous = by_link.get(binding.order_link_id)
        if previous is not None and previous != binding.order_id:
            raise ValueError("Conflicting confirmed entry order identities")
        by_link[binding.order_link_id] = binding.order_id
    descriptions = [
        {
            "order_id": by_link[link],
            "order_link_id": link,
            "leg_name": legs[order.name],
            "quantity": str(order.quantity),
        }
        for link, order in expected.items()
        if link in by_link
    ]
    entry_ids = tuple(item["order_id"] for item in descriptions)
    if len(entry_ids) != len(set(entry_ids)):
        raise ValueError("Duplicate confirmed physical entry identity")
    primary_ids = tuple(
        item["order_id"] for item in descriptions if item["leg_name"] == "E1"
    )
    owned_ids = tuple(
        dict.fromkeys((*entry_ids, *(order.order_id for order in bindings)))
    )
    evidence = {
        "strategy_status": strategy_status,
        "entry_identity_complete": len(entry_ids) == len(expected),
        "entry_orders": descriptions,
    }
    return _EntryAttempt(row, plan, entry_ids, primary_ids, owned_ids, evidence)


def _entry_orders_settled(
    attempt: _EntryAttempt,
    history: list[dict],
    current: list[dict],
    fills: dict[str, list[dict]],
) -> bool:
    assert attempt.evidence is not None
    return entry_orders_settled(
        attempt.evidence["entry_orders"],
        attempt.plan["symbol"],
        "Buy" if attempt.plan["side"] == "LONG" else "Sell",
        history,
        current,
        fills,
    )


def _attached_closing_orders(
    attempt: _EntryAttempt, history: list[dict]
) -> dict[str, str]:
    if attempt.evidence is None:
        return {}
    parents = {item["order_link_id"]: item for item in attempt.evidence["entry_orders"]}
    closers = {}
    for row in history:
        parent = parents.get(row.get("parentOrderLinkId"))
        if parent is None:
            continue
        try:
            child = account_order_from_item(row)
        except (ValueError, TypeError, ArithmeticError, TradeExecutionError):
            continue
        qualified = (
            child.order_id
            and not child.order_link_id
            and child.stop_order_type in {"PartialTakeProfit", "PartialStopLoss"}
            and (child.reduce_only or child.close_on_trigger)
            and child.symbol == attempt.plan["symbol"]
            and child.side.value != attempt.plan["side"]
            and child.quantity.is_finite()
            and 0 < child.quantity <= Decimal(parent["quantity"])
        )
        if qualified:
            closers[child.order_id] = child.parent_order_link_id
    return closers


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _market_candle_coverage(
    filled_symbols: set[str],
    first_fill_times: list[int],
    participation_assignments: list[dict],
) -> tuple[list[str], int]:
    symbols = {symbol.upper() for symbol in filled_symbols}
    start_times = list(first_fill_times)
    for assignment in participation_assignments:
        symbol = assignment.get("symbol")
        if symbol:
            symbols.add(str(symbol).upper())
        created_at = assignment.get("created_at")
        if created_at:
            start_times.append(_timestamp_ms(str(created_at)))

    if not start_times:
        raise ValueError("Forensic market-candle export requires a timestamp")
    return sorted(symbols), min(start_times) - MARKET_CANDLE_LOOKBACK_MS


def _historical_rows(
    client: BybitClient,
    path: str,
    base_params: dict[str, object],
    start_ms: int,
    end_ms: int,
    *,
    limit: int,
) -> list[dict]:
    rows = []
    window_ms = int(WINDOW.total_seconds() * 1000)

    for window_start in range(start_ms, end_ms, window_ms):
        window_end = min(window_start + window_ms - 1, end_ms)
        rows.extend(
            client.paginate_private_list(
                path,
                {
                    **base_params,
                    "startTime": window_start,
                    "endTime": window_end,
                    "limit": limit,
                },
            )
        )

    return rows


def _write_jsonl(
    archive: zipfile.ZipFile,
    root: str,
    name: str,
    rows,
) -> None:
    with archive.open(f"{root}/{name}", "w") as output:
        for row in rows:
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            output.write((encoded + "\n").encode("utf-8"))


def _market_candles(
    client: BybitClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> dict[int, dict[str, int | str]]:
    candles = {}
    chunk_start = start_ms // 60_000 * 60_000

    while chunk_start <= end_ms:
        chunk_end = min(chunk_start + KLINE_CHUNK_MS - 1, end_ms)
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": "1",
            "start": chunk_start,
            "end": chunk_end,
            "limit": 1_000,
        }

        for attempt in range(4):
            try:
                response = client.public_get("/v5/market/kline", params)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)

        for row in response.get("result", {}).get("list", []):
            candle_start = int(row[0])
            candles[candle_start] = {
                "startTime": candle_start,
                "high": row[2],
                "low": row[3],
                "close": row[4],
            }

        chunk_start = chunk_end + 1

    return candles


def export_bundle(
    output_path: Path,
    *,
    skip_market_candles: bool = False,
    access: ForensicAccess | None = None,
) -> dict[str, int | str]:
    if output_path.exists():
        raise FileExistsError(output_path)

    settings = access or get_settings()
    database_uri = settings.database_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as database:
        database.row_factory = sqlite3.Row
        database.execute("BEGIN")
        plan_rows = list(
            database.execute(
                """
                SELECT p.intent_id, p.payload_json, p.bybit_order_ids_json,
                       p.created_at, i.status, i.approval_mode,
                       i.payload_json AS intent_payload_json
                FROM execution_plans AS p
                JOIN intents AS i ON i.intent_id = p.intent_id
                ORDER BY p.created_at
                """
            )
        )
        action_rows = list(
            database.execute(
                """
                SELECT payload_json, status, bybit_order_id
                FROM position_actions
                WHERE status = 'EXECUTED'
                ORDER BY created_at
                """
            )
        )

        # Orders placed by this production database's own strategies and actions.
        # The rehearsal suites reuse the same link conventions with their own
        # disposable stores, so link shape alone cannot separate them.
        tables = {
            row["name"]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        own_strategy_prefixes = set()
        own_action_prefixes = set()
        if "position_strategies" in tables:
            own_strategy_prefixes = {
                f"ccb-v2-{UUID(row['strategy_id']).hex[:20]}-"
                for row in database.execute(
                    "SELECT strategy_id FROM position_strategies"
                )
                if _is_uuid(row["strategy_id"])
            }
        if "position_actions" in tables:
            own_action_prefixes = {
                f"ccb-action-{UUID(action_id).hex[:24]}"
                for payload in (
                    row[0]
                    for row in database.execute(
                        "SELECT payload_json FROM position_actions"
                    )
                )
                for action_id in [
                    str((json.loads(payload) or {}).get("action_id") or "")
                ]
                if action_id and _is_uuid(action_id)
            }
        bindings = _bound_orders(database) if "zone_orders" in tables else {}
        strategy_states = (
            {
                row["strategy_id"]: row["status"]
                for row in database.execute(
                    "SELECT strategy_id,status FROM position_strategies"
                )
            }
            if "position_strategies" in tables
            else {}
        )

    plans = []
    participation_assignments = []
    for row in plan_rows:
        plan = json.loads(row["payload_json"])
        attempt = _entry_attempt(
            row,
            bindings.get(row["intent_id"], []),
            strategy_states.get(row["intent_id"]),
        )
        policy = plan.get("policy") or {}
        strategy = policy.get("strategy_v2") or {}
        arm = strategy.get("long_participation_arm")
        if arm in {"take", "skip"}:
            intent_payload = json.loads(row["intent_payload_json"])
            participation_assignments.append(
                {
                    "intent_id": row["intent_id"],
                    "created_at": row["created_at"],
                    "status": row["status"],
                    "approval_mode": row["approval_mode"],
                    "bybit_order_ids": list(attempt.entry_ids)
                    if attempt
                    else json.loads(row["bybit_order_ids_json"] or "[]"),
                    "arm": arm,
                    "long_participation_momentum": strategy.get(
                        "long_participation_momentum"
                    ),
                    "symbol": plan.get("symbol"),
                    "side": plan.get("side"),
                    "risk_per_trade_pct": policy.get("risk_per_trade_pct"),
                    "planned_max_loss_usdt": plan.get("planned_max_loss_usdt"),
                    "exit_profile": strategy.get("exit_profile", "baseline"),
                    "source_channel_id": (intent_payload.get("source") or {}).get(
                        "channel_id"
                    ),
                }
            )
        if attempt is not None:
            plans.append(attempt)

    if not plans:
        raise ValueError("No accepted V2 entry orders with durable ownership")

    client = BybitClient(
        base_url=DEMO_BASE_URL,
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
    )
    client.sync_clock()
    end_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = (
        min(_timestamp_ms(attempt.row["created_at"]) for attempt in plans) - 86_400_000
    )

    executions = _historical_rows(
        client,
        "/v5/execution/list",
        {"category": "linear"},
        start_ms,
        end_ms,
        limit=100,
    )
    entry_order_ids = {
        str(order_id) for attempt in plans for order_id in attempt.entry_ids
    }
    order_history = _historical_rows(
        client,
        "/v5/order/history",
        {"category": "linear", "settleCoin": "USDT"},
        start_ms,
        end_ms,
        limit=50,
    )
    entry_order_history = [
        order
        for order in order_history
        if str(order.get("orderId") or "") in entry_order_ids
    ]
    open_account_orders = client.paginate_private_list(
        "/v5/order/realtime",
        {"category": "linear", "settleCoin": "USDT", "limit": 50},
    )
    open_account_orders_as_of = datetime.now(UTC).isoformat()
    open_entry_orders_as_of = open_account_orders_as_of
    tracked_entry_order_ids = {str(order_id) for order_id in entry_order_ids}
    current_entry_orders = [
        order
        for order in open_account_orders
        if str(order.get("orderId") or "") in entry_order_ids
    ]
    protective_order_types = {
        "TakeProfit",
        "PartialTakeProfit",
        "StopLoss",
        "PartialStopLoss",
        "TrailingStop",
    }
    unmatched_entry_order_count = sum(
        str(order.get("orderId") or "") not in tracked_entry_order_ids
        and not (
            order.get("reduceOnly") is True
            or str(order.get("reduceOnly")).casefold() == "true"
            or order.get("closeOnTrigger") is True
            or str(order.get("closeOnTrigger")).casefold() == "true"
            or order.get("stopOrderType") in protective_order_types
        )
        for order in open_account_orders
    )
    transactions = _historical_rows(
        client,
        "/v5/account/transaction-log",
        {"accountType": "UNIFIED", "category": "linear"},
        start_ms,
        end_ms,
        limit=50,
    )
    closed_pnl = _historical_rows(
        client,
        "/v5/position/closed-pnl",
        {"category": "linear"},
        start_ms,
        end_ms,
        limit=100,
    )
    open_positions_as_of = datetime.now(UTC).isoformat()
    open_positions = [
        position
        for position in client.paginate_private_list(
            "/v5/position/list",
            {"category": "linear", "settleCoin": "USDT", "limit": 200},
        )
        if float(position.get("size") or 0) > 0
    ]

    def _probe_link(link: str) -> bool:
        if link.startswith(("ccb-zone-", "ccb-recover-")):
            return True
        if link.startswith("ccb-v2-"):
            return not any(link.startswith(prefix) for prefix in own_strategy_prefixes)
        if link.startswith("ccb-action-"):
            return not any(link.startswith(prefix) for prefix in own_action_prefixes)
        return False

    probe_order_ids = sorted(
        {
            str(execution.get("orderId"))
            for execution in executions
            if execution.get("orderId")
            and _probe_link(str(execution.get("orderLinkId") or ""))
        }
    )

    executions_by_order = defaultdict(list)
    for execution in executions:
        if execution.get("execType") == "Trade":
            executions_by_order[str(execution.get("orderId") or "")].append(execution)

    lineage = []
    symbols = set()
    first_fill_times = []
    for attempt in plans:
        plan = attempt.plan
        order_ids = attempt.entry_ids
        intent_id = attempt.row["intent_id"]
        created_at = attempt.row["created_at"]
        intent_payload = json.loads(attempt.row["intent_payload_json"])
        # V2 submits E1 first; later entry legs are modeled by the replay candidate.
        primary_fills = [
            execution
            for order_id in attempt.primary_ids
            for execution in executions_by_order.get(str(order_id), [])
            if execution.get("execType") == "Trade"
        ]
        entry_fills = [
            execution
            for order_id in order_ids
            for execution in executions_by_order.get(str(order_id), [])
        ]
        if not entry_fills:
            continue
        # Every execution of this plan's own orders (entries, installed exits
        # and coordinator closes) so Closed-PnL can be attributed by identity.
        attached = _attached_closing_orders(attempt, order_history)
        owned_ids = tuple(dict.fromkeys((*attempt.owned_ids, *attached)))
        plan_executions = [
            execution
            for order_id in owned_ids
            for execution in executions_by_order.get(str(order_id), [])
            if execution.get("execType") == "Trade"
        ]
        source = intent_payload.get("source") or {}
        entry = intent_payload.get("entry") or {}
        policy = plan.get("policy") or {}
        symbols.add(plan["symbol"].upper())
        first_fill_times.extend(int(fill["execTime"]) for fill in entry_fills)
        item = {
            "status": attempt.row["status"],
            "intent_id": intent_id,
            "created_at": created_at,
            "entry_order_ids": list(order_ids),
            "metadata": {
                "source_channel_id": source.get("channel_id"),
                "confidence": intent_payload.get("confidence"),
                "entry_type": entry.get("type") if isinstance(entry, dict) else None,
                "risk_per_trade_pct": policy.get("risk_per_trade_pct"),
                "planned_max_loss_usdt": plan.get("planned_max_loss_usdt"),
                "exit_profile": (policy.get("strategy_v2") or {}).get(
                    "exit_profile", "baseline"
                ),
                "long_participation_arm": (policy.get("strategy_v2") or {}).get(
                    "long_participation_arm"
                ),
                "long_participation_momentum": (policy.get("strategy_v2") or {}).get(
                    "long_participation_momentum"
                ),
                "entry_order_ttl_minutes": (policy.get("strategy_v2") or {}).get(
                    "entry_order_ttl_minutes", 0
                ),
            },
            "intent": {
                "symbol": plan["symbol"],
                "side": plan["side"],
                "stop_loss": plan["stop_loss"],
                "take_profit": plan.get("take_profit"),
            },
            "bybit": {
                "executions": primary_fills,
                "plan_executions": plan_executions,
            },
        }
        if attempt.evidence is not None:
            item["entry_evidence"] = attempt.evidence | {
                "attached_closing_orders": [
                    {"order_id": key, "parent_order_link_id": value}
                    for key, value in attached.items()
                ],
                "entry_orders_settled": _entry_orders_settled(
                    attempt,
                    entry_order_history,
                    current_entry_orders,
                    executions_by_order,
                ),
            }
        lineage.append(item)

    if not lineage:
        raise ValueError("No filled V2 primary-entry orders found in Bybit history")

    actions = [
        {
            "status": row["status"],
            "payload_json": row["payload_json"],
            "bybit_order_id": row["bybit_order_id"],
        }
        for row in action_rows
        if row["bybit_order_id"]
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    root = "forensic"
    output_created = False

    try:
        with output_path.open("xb") as destination:
            output_created = True
            with zipfile.ZipFile(
                destination,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=4,
            ) as archive:
                _write_jsonl(
                    archive,
                    root,
                    "analysis/trade_lineage.jsonl",
                    [{"intents": lineage}],
                )
                _write_jsonl(
                    archive,
                    root,
                    "analysis/long_participation_assignments.jsonl",
                    [{"assignments": participation_assignments}],
                )
                _write_jsonl(
                    archive,
                    root,
                    "analysis/probe_order_ids.jsonl",
                    [{"probe_order_ids": probe_order_ids}],
                )
                _write_jsonl(archive, root, "bybit/executions.jsonl", executions)
                _write_jsonl(
                    archive,
                    root,
                    "bybit/entry_order_history.jsonl",
                    entry_order_history,
                )
                _write_jsonl(
                    archive,
                    root,
                    "bybit/open_entry_orders.jsonl",
                    current_entry_orders,
                )
                _write_jsonl(
                    archive,
                    root,
                    "bybit/open_account_orders.jsonl",
                    open_account_orders,
                )
                _write_jsonl(
                    archive,
                    root,
                    "database/position_actions.jsonl",
                    actions,
                )
                _write_jsonl(
                    archive,
                    root,
                    "bybit/transaction_log.jsonl",
                    [
                        row
                        for row in transactions
                        if str(row.get("type", "")).upper() == "SETTLEMENT"
                    ],
                )
                _write_jsonl(
                    archive,
                    root,
                    "bybit/closed_pnl.jsonl",
                    closed_pnl,
                )
                _write_jsonl(
                    archive,
                    root,
                    "bybit/open_positions.jsonl",
                    open_positions,
                )

                candle_symbols, candle_start = _market_candle_coverage(
                    symbols,
                    first_fill_times,
                    participation_assignments,
                )
                if skip_market_candles:
                    candle_symbols = []
                for index, symbol in enumerate(candle_symbols, start=1):
                    candles = _market_candles(
                        client,
                        symbol,
                        candle_start,
                        now_ms,
                    )
                    _write_jsonl(
                        archive,
                        root,
                        f"market_1m/{symbol}.jsonl",
                        (candles[key] for key in sorted(candles)),
                    )
                    print(
                        f"candles {index}/{len(candle_symbols)} "
                        f"{symbol}: {len(candles)}"
                    )

                metadata = {
                    "created_at": datetime.now(UTC).isoformat(),
                    "exchange": "Bybit Demo",
                    "category": "linear",
                    "interval_minutes": 1,
                    "market_candles_included": not skip_market_candles,
                    "market_candle_lookback_minutes": (MARKET_CANDLE_LOOKBACK_MINUTES),
                    "filled_v2_cases": len(lineage),
                    "long_participation_assignment_count": len(
                        participation_assignments
                    ),
                    "symbols": len(candle_symbols),
                    "execution_rows": len(executions),
                    "entry_order_history_rows": len(entry_order_history),
                    "open_entry_order_rows": len(current_entry_orders),
                    "open_account_order_rows": len(open_account_orders),
                    "open_account_orders_as_of": open_account_orders_as_of,
                    "open_entry_orders_as_of": open_entry_orders_as_of,
                    "unmatched_entry_order_count": unmatched_entry_order_count,
                    "entry_order_history_coverage_start": datetime.fromtimestamp(
                        start_ms / 1000,
                        UTC,
                    ).isoformat(),
                    "entry_order_history_as_of": datetime.fromtimestamp(
                        end_ms / 1000,
                        UTC,
                    ).isoformat(),
                    "entry_order_history_note": (
                        "Bybit history retention varies by status; cancelled, "
                        "rejected, and deactivated orders may be unavailable "
                        "outside the recent 24-hour window."
                    ),
                    "closed_pnl_rows": len(closed_pnl),
                    "closed_pnl_coverage_start": datetime.fromtimestamp(
                        start_ms / 1000,
                        UTC,
                    ).isoformat(),
                    "closed_pnl_as_of": datetime.fromtimestamp(
                        end_ms / 1000,
                        UTC,
                    ).isoformat(),
                    "open_position_rows": len(open_positions),
                    "open_positions_as_of": open_positions_as_of,
                    "funding_settlements": sum(
                        str(row.get("type", "")).upper() == "SETTLEMENT"
                        for row in transactions
                    ),
                    "lifecycle_actions": len(actions),
                }
                archive.writestr(
                    f"{root}/metadata.json",
                    json.dumps(metadata, indent=2),
                )
    except Exception:
        if output_created:
            output_path.unlink(missing_ok=True)
        raise

    client.close()
    metadata["output"] = str(output_path)
    metadata["bytes"] = output_path.stat().st_size
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export a read-only Bybit Demo forensic bundle for Strategy V2 replay."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New zip path; existing files are never overwritten.",
    )
    parser.add_argument(
        "--skip-market-candles",
        action="store_true",
        help="PnL-only refresh: retain private reconciliation evidence without candles",
    )
    args = parser.parse_args()

    try:
        summary = export_bundle(
            args.output, skip_market_candles=args.skip_market_candles
        )
    except FileExistsError as exc:
        raise SystemExit(
            f"Refusing to overwrite existing bundle: {args.output}"
        ) from exc

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
