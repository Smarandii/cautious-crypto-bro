"""Offline v2 shadow diagnostic: exchange-timed actions, frozen engine/policies.

The original run is never overwritten. Unfilled actions remain unresolved;
their local database timestamps cannot safely be compared to exchange time.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import math
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import UUID

METHOD = "exchange-action-lineage-v2"
ENGINE_SHA256 = "f798acdb95e1fa45588b679b899765fcbba691e9f3cf2bda444722280e51bdf2"


def unique_rows(rows, key):
    result = {}
    for row in rows:
        ident = row[key]
        if not ident or (ident in result and result[ident] != row):
            raise ValueError(f"Missing or conflicting {key}: {ident}")
        result[ident] = row
    return result


def reconcile_actions(inputs, snapshot):
    """Match executions before filtering chronology; change only case.actions."""
    corrected = deepcopy(inputs)
    cases = [item["case"] for item in corrected]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate case IDs")
    for case in cases:
        case["actions"] = []
    audit = {"method": METHOD, "applied": [], "unresolved": [], "excluded": []}
    fills = unique_rows(
        [f for f in snapshot["exchange"]["fills"] if f["execType"] == "Trade"],
        "execId",
    )
    orders = unique_rows(snapshot["exchange"]["orders"], "orderId")
    claimed_orders = set()
    actions = unique_rows(snapshot["db"]["position_actions"], "action_id")
    for row in actions.values():
        action = json.loads(row["payload_json"])
        if action["action"] not in {"REDUCE", "CLOSE"}:
            continue
        candidates = [
            case
            for case in cases
            if int(case["channel_id"]) == int(row["channel_id"])
            and case["symbol"] == action["symbol"]
            and action.get("expected_side") in (None, case["side"])
        ]
        if not candidates:
            continue
        aid = row["action_id"]
        link = f"ccb-action-{UUID(aid).hex[:24]}"
        oid = row.get("bybit_order_id")
        matched = sorted(
            (
                f
                for f in fills.values()
                if (oid and f["orderId"] == oid) or f.get("orderLinkId") == link
            ),
            key=lambda f: (int(f["execTime"]), f["execId"]),
        )
        reason = None
        if not matched:
            reason = "No attributable execution; local clock and episode unresolved"
        else:
            ids = {f["orderId"] for f in matched}
            if len(ids) != 1 or (oid and ids != {oid}):
                raise ValueError(f"Conflicting order IDs for {aid}")
            matched_oid = matched[0]["orderId"]
            if matched_oid in claimed_orders:
                raise ValueError(f"Order attributed to multiple actions: {matched_oid}")
            claimed_orders.add(matched_oid)
            for fill in matched:
                if (
                    fill["symbol"] != action["symbol"]
                    or fill.get("orderLinkId") not in (None, "", link)
                    or not Decimal(fill["execQty"]) == Decimal(fill["closedSize"]) > 0
                ):
                    raise ValueError(f"Invalid closing execution for {aid}")
            first, last = int(matched[0]["execTime"]), int(matched[-1]["execTime"])
            for case in candidates:
                if first >= case["end_ms"] or last < case["start_ms"]:
                    audit["excluded"].append(
                        dict(
                            case_id=case["id"],
                            action_id=aid,
                            reason="Execution outside case/window",
                            first_execution_ms=first,
                        )
                    )
            candidates = [
                case
                for case in candidates
                if first < case["end_ms"] and last >= case["start_ms"]
            ]
            if len(candidates) > 1:
                reason = "Execution spans multiple cases"
            elif candidates:
                case = candidates[0]
                expected = "Sell" if case["side"] == "LONG" else "Buy"
                if any(fill["side"] != expected for fill in matched):
                    raise ValueError(f"Wrong closing side for {aid}")
                order = orders.get(matched_oid)
                qty = sum(Decimal(fill["execQty"]) for fill in matched)
                if order is None:
                    reason = "Order history missing; full execution unverified"
                elif (
                    order.get("symbol", case["symbol"]) != case["symbol"]
                    or order.get("side", expected) != expected
                    or order.get("orderLinkId") not in (None, "", link)
                ):
                    raise ValueError(f"Conflicting order history for {aid}")
                elif order["orderStatus"] != "Filled" or Decimal(order["qty"]) != qty:
                    reason = (
                        "Partial or incomplete execution unsupported by frozen engine"
                    )
                elif not case["start_ms"] <= first <= last < case["end_ms"]:
                    reason = "Execution crosses case cutoff"
                elif (first + 59999) // 60000 != (last + 59999) // 60000:
                    reason = "Split execution crosses replay minutes"
                elif (first + 59999) // 60000 * 60000 >= case["end_ms"]:
                    reason = "Execution rounds beyond final replay candle"
                if reason is None:
                    pct = (
                        100.0
                        if action["action"] == "CLOSE"
                        else float(action["close_pct"])
                    )
                    if not math.isfinite(pct) or not 0 < pct <= 100:
                        raise ValueError(f"Invalid reduction percentage for {aid}")
                    event = dict(
                        ts=first,
                        action=action["action"],
                        close_pct=pct,
                        action_id=aid,
                        observed_status=row["status"],
                    )
                    case["actions"].append(event)
                    audit["applied"].append(
                        dict(
                            **event,
                            case_id=case["id"],
                            order_id=matched_oid,
                            execution_ids=[fill["execId"] for fill in matched],
                            executed_qty=str(qty),
                            local_created_at=row["created_at"],
                        )
                    )
        if reason:
            audit["unresolved"].extend(
                dict(
                    case_id=case["id"],
                    action_id=aid,
                    status=row["status"],
                    reason=reason,
                )
                for case in candidates
            )
    for case in cases:
        case["actions"].sort(key=lambda event: (event["ts"], event["action_id"]))
    return corrected, audit


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(rows):
    groups = {}
    for cohort in ("unseen_replay", "forward_shadow"):
        chosen = [row for row in rows if row["cohort"] == cohort]
        totals = defaultdict(lambda: defaultdict(float))
        closed = defaultdict(set)
        for row in chosen:
            key = (row["path"], row["slippage_bps"])
            totals[key][row["variant"]] += row["net_pnl"]
            closed[row["id"]].add(row["closed"])

        def span(values):
            return [min(values), max(values)] if values else None

        complete = sum(values == {True} for values in closed.values())
        groups[cohort] = dict(
            cases=len(closed),
            completed_pairs=complete,
            censored_cases=len(closed) - complete,
            baseline_net=span([t["baseline"] for t in totals.values()]),
            delayed_net=span([t["delayed"] for t in totals.values()]),
            paired_delta=span([t["delayed"] - t["baseline"] for t in totals.values()]),
        )
    return groups


def run(run_dir, engine_path, output):
    run_dir, engine_path, output = (
        path.resolve() for path in (run_dir, engine_path, output)
    )
    if (
        output.exists()
        or output.is_relative_to(run_dir)
        or run_dir.is_relative_to(output)
    ):
        raise ValueError("Output must be a new directory outside the frozen run")
    names = (
        "config.json",
        "snapshot.json",
        "inputs.json",
        "summary.json",
        "actual_ledger.json",
    )
    hashes = {name: digest(run_dir / name) for name in names}
    data = {
        name: json.loads((run_dir / name).read_text(encoding="utf-8-sig"))
        for name in names
    }
    config = data["config.json"]
    if digest(engine_path) != ENGINE_SHA256 or config["engine_sha256"] != ENGINE_SHA256:
        raise ValueError("Frozen engine hash mismatch")
    if config["policies"] != ["baseline", "delayed"]:
        raise ValueError("Expected frozen baseline/delayed policies")
    if any(data["summary.json"].get(key) != value for key, value in config.items()):
        raise ValueError("Summary/config provenance mismatch")
    if data["summary.json"]["snapshot_ms"] != data["snapshot.json"]["end_ms"]:
        raise ValueError("Snapshot/summary provenance mismatch")
    inputs, audit = reconcile_actions(data["inputs.json"], data["snapshot.json"])
    spec = importlib.util.spec_from_file_location("frozen_shadow_engine", engine_path)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load frozen Python engine")
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    engine.self_check()
    rows, baseline = [], []
    actual = {row["id"]: row for row in data["actual_ledger.json"]}
    for item in inputs:
        case = item["case"]
        if not config["training_end_ms"] < case["start_ms"] < config["forward_end_ms"]:
            raise ValueError("Case outside frozen enrollment")
        if (
            case["end_ms"] > config["forward_end_ms"]
            or case["channel_id"] not in config["allowed_channels"]
        ):
            raise ValueError("Case outside frozen cutoff or channels")
        expected = (
            "unseen_replay"
            if case["start_ms"] < config["forward_start_ms"]
            else "forward_shadow"
        )
        if case["cohort"] != expected:
            raise ValueError("Frozen cohort mismatch")
        for variant, path, slip in itertools.product(
            config["policies"], config["paths"], config["slippage_bps"]
        ):
            row = engine.replay(case, item["bars"], variant, path, slip)
            row.pop("equity_samples")
            row.update(symbol=case["symbol"], cohort=case["cohort"])
            rows.append(row)
        ledger = actual.get(case["id"], {})
        if (
            ledger.get("closed")
            and case.get("actual_end_ms") is not None
            and case["actual_end_ms"] <= case["end_ms"]
        ):
            values = [
                row["net_pnl"]
                for row in rows
                if row["id"] == case["id"] and row["variant"] == "baseline"
            ]
            net = ledger["net_realized_cashflow"]
            baseline.append(
                dict(
                    case_id=case["id"],
                    symbol=case["symbol"],
                    actual_net=net,
                    baseline_range=[min(values), max(values)],
                    nearest_error=min(abs(value - net) for value in values),
                    outside_range=not min(values) - 1e-7 <= net <= max(values) + 1e-7,
                )
            )
    if hashes != {name: digest(run_dir / name) for name in names}:
        raise ValueError("Input archive changed while replaying")
    summary = dict(
        method=METHOD,
        frozen_config=config,
        snapshot_ms=data["summary.json"]["snapshot_ms"],
        groups=summarize(rows),
        baseline_checks=baseline,
        unresolved_actions=len(audit["unresolved"]),
        applied_actions=len(audit["applied"]),
        interpretation="Executed-action diagnostic only; unresolved source actions and baseline mismatches prevent a complete counterfactual claim.",
    )
    output.mkdir(parents=True)
    for name, value in (
        ("inputs.json", inputs),
        ("actions.json", audit),
        ("rows.json", rows),
        ("summary.json", summary),
        (
            "manifest.json",
            dict(
                method=METHOD,
                source_run=str(run_dir),
                source_sha256=hashes,
                runner_sha256=digest(Path(__file__)),
                engine_sha256=ENGINE_SHA256,
                original_groups=data["summary.json"]["groups"],
            ),
        ),
    ):
        (output / name).write_text(
            json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    lines = [
        "# Exchange-timed shadow diagnostic v2",
        "",
        summary["interpretation"],
        "",
        "Frozen original untouched. Same engine, entry fills, bars, fees, policies and observation window.",
        "Only attributable fully filled lifecycle actions are applied, once, at their first exchange execution time.",
        "Unfilled source actions are excluded and listed unresolved; historical clock offsets are not guessed.",
        "Frozen minute-candle execution, fill and trailing assumptions still apply. Ranges are sensitivity cases, not confidence bounds.",
        "",
        "| Cohort | Cases | Completed pairs | Censored | Baseline net | Delayed net | Paired delta |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    for name, group in summary["groups"].items():
        spans = [
            "—"
            if group[key] is None
            else f"{group[key][0]:+.2f} to {group[key][1]:+.2f}"
            for key in ("baseline_net", "delayed_net", "paired_delta")
        ]
        lines.append(
            f"| {name} | {group['cases']} | {group['completed_pairs']} | {group['censored_cases']} | "
            + " | ".join(spans)
            + " |"
        )
    lines += [
        "",
        f"Applied actions: {summary['applied_actions']}. Unresolved case/action pairs: {summary['unresolved_actions']}.",
        "",
        "## Baseline versus actual closed net",
        "",
    ]
    for check in baseline:
        lines.append(
            f"- {check['symbol']}: actual {check['actual_net']:+.4f}; baseline {check['baseline_range']}; nearest error {check['nearest_error']:.4f}; outside modeled range: {check['outside_range']}."
        )
    lines += [
        "",
        "Open cases include mark-to-market and remain censored. Simulated differences do not establish profitability or authorize a policy change.",
    ]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir", type=Path, required=True, help="Immutable archived shadow run"
    )
    parser.add_argument(
        "--engine", type=Path, required=True, help="Original frozen replay_engine.py"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New separate output directory"
    )
    args = parser.parse_args()
    print(json.dumps(run(args.run_dir, args.engine, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
