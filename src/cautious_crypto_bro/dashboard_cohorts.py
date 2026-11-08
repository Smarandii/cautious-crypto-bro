"""Exit-profile, ladder-depth, signal-setup, and source-decision cohorts for the public feed."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .dashboard_metrics import (
    _cohort_metrics,
    _number,
    _optional_number,
    _rounded,
    summarize_reconciled_positions,
)

_LADDER_DEPTH_ORDER = ("E1_only", "E1_E2", "E1_E2_E3_full")
_SIGNAL_SETUP_ORDER = (
    "MARKET_TRADER",
    "MARKET_POLICY",
    "LIMIT_TRADER",
    "LIMIT_POLICY",
    "RANGE_TRADER",
    "RANGE_POLICY",
)


def _cohort_row(
    key_field: str,
    key_value: str,
    p_comp: list[Mapping[str, object]],
    p_size: list[Mapping[str, object]],
    p_cur: list[Mapping[str, object]],
) -> dict[str, object]:
    perf = summarize_reconciled_positions(p_comp)
    comp_count = int(perf["position_count"])  # type: ignore[arg-type]
    return {
        key_field: key_value,
        "completed_trades": comp_count,
        "winning_trades": int(perf["positive_count"]),  # type: ignore[arg-type]
        "losing_trades": int(perf["negative_count"]),  # type: ignore[arg-type]
        "win_rate_pct": perf["win_rate_pct"],
        "net_pnl_usdt": perf["net_pnl_usdt"] if comp_count else 0.0,
        "net_r": (
            _rounded(sum(_number(p, "net_r") for p in p_comp), 6) if comp_count else 0.0
        ),
        "expectancy_r": perf["expectancy_r"],
        "profit_factor": perf["profit_factor"],
        **_cohort_metrics(p_size, "size_reconciled"),
        **_cohort_metrics(p_cur, "current_risk"),
    }


def build_exit_cohorts(
    complete: list[Mapping[str, object]],
    size_reconciled: list[Mapping[str, object]],
    current_risk: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    profiles = sorted(
        {str(p.get("exit_profile") or "baseline") for p in size_reconciled}
    )
    cohorts = [
        _cohort_row(
            "exit_profile",
            profile,
            [
                p
                for p in complete
                if str(p.get("exit_profile") or "baseline") == profile
            ],
            [
                p
                for p in size_reconciled
                if str(p.get("exit_profile") or "baseline") == profile
            ],
            [
                p
                for p in current_risk
                if str(p.get("exit_profile") or "baseline") == profile
            ],
        )
        for profile in profiles
    ]
    cohorts.sort(
        key=lambda c: (
            -int(c["current_risk_trades"]),  # type: ignore[arg-type]
            -int(c["size_reconciled_trades"]),  # type: ignore[arg-type]
            str(c["exit_profile"]),
        )
    )
    return cohorts


def _plan_leg_thresholds(database, tables: set[str]) -> dict[str, tuple[float, float]]:
    if "execution_plans" not in tables:
        return {}
    thresholds: dict[str, tuple[float, float]] = {}
    for intent_id, payload_str in database.execute(
        "SELECT CAST(intent_id AS TEXT), payload_json FROM execution_plans"
    ):
        try:
            orders = json.loads(payload_str).get("orders") or []
            q1 = float(orders[0]["quantity"])
            q12 = q1 + (float(orders[1]["quantity"]) if len(orders) > 1 else 0.0)
            thresholds[str(intent_id)] = (q1, q12)
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return thresholds


def _classify_ladder_depth(
    position: Mapping[str, object],
    thresholds: dict[str, tuple[float, float]],
) -> str:
    explicit = position.get("ladder_depth")
    if explicit in _LADDER_DEPTH_ORDER:
        return str(explicit)
    pair = thresholds.get(str(position.get("intent_id") or ""))
    entry_qty = _optional_number(position.get("entry_qty")) or 0.0
    if pair is None or entry_qty <= 0:
        return "E1_only"
    q1, q12 = pair
    if abs(entry_qty - q1) <= max(1e-6, q1 * 0.01):
        return "E1_only"
    if abs(entry_qty - q12) <= max(1e-6, q12 * 0.01):
        return "E1_E2"
    return "E1_E2_E3_full"


def build_ladder_cohorts(
    database,
    tables: set[str],
    complete: list[Mapping[str, object]],
    size_reconciled: list[Mapping[str, object]],
    current_risk: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    thresholds = _plan_leg_thresholds(database, tables)
    cohorts: list[dict[str, object]] = []
    for depth in _LADDER_DEPTH_ORDER:
        p_size = [
            p for p in size_reconciled if _classify_ladder_depth(p, thresholds) == depth
        ]
        if not p_size:
            continue
        p_comp = [p for p in complete if _classify_ladder_depth(p, thresholds) == depth]
        p_cur = [
            p for p in current_risk if _classify_ladder_depth(p, thresholds) == depth
        ]
        cohorts.append(_cohort_row("ladder_depth", depth, p_comp, p_size, p_cur))
    return cohorts


def _intent_setups(database, tables: set[str]) -> dict[str, str]:
    entry_types: dict[str, str] = {}
    if "intents" in tables:
        for intent_id, payload_str in database.execute(
            "SELECT CAST(intent_id AS TEXT), payload_json FROM intents"
        ):
            try:
                etype = str(
                    (json.loads(payload_str).get("entry") or {}).get("type") or "MARKET"
                ).upper()
                if etype in {"MARKET", "LIMIT", "RANGE"}:
                    entry_types[str(intent_id)] = etype
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    stop_sources: dict[str, str] = {}
    if "execution_plans" in tables:
        for intent_id, payload_str in database.execute(
            "SELECT CAST(intent_id AS TEXT), payload_json FROM execution_plans"
        ):
            try:
                src = str(
                    json.loads(payload_str).get("stop_loss_source") or "TRADER"
                ).upper()
                stop_sources[str(intent_id)] = "TRADER" if src == "TRADER" else "POLICY"
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return {
        iid: f"{entry_types.get(iid, 'MARKET')}_{stop_sources.get(iid, 'TRADER')}"
        for iid in set(entry_types) | set(stop_sources)
    }


def _classify_signal_setup(
    position: Mapping[str, object], setups: dict[str, str]
) -> str:
    explicit = position.get("setup")
    if explicit in _SIGNAL_SETUP_ORDER:
        return str(explicit)
    return setups.get(str(position.get("intent_id") or ""), "MARKET_TRADER")


def build_setup_cohorts(
    database,
    tables: set[str],
    complete: list[Mapping[str, object]],
    size_reconciled: list[Mapping[str, object]],
    current_risk: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    setups = _intent_setups(database, tables)
    cohorts: list[dict[str, object]] = []
    for setup in _SIGNAL_SETUP_ORDER:
        p_size = [
            p for p in size_reconciled if _classify_signal_setup(p, setups) == setup
        ]
        if not p_size:
            continue
        p_comp = [p for p in complete if _classify_signal_setup(p, setups) == setup]
        p_cur = [p for p in current_risk if _classify_signal_setup(p, setups) == setup]
        cohorts.append(_cohort_row("setup", setup, p_comp, p_size, p_cur))
    cohorts.sort(
        key=lambda c: (
            -int(c["current_risk_trades"]),  # type: ignore[arg-type]
            -int(c["size_reconciled_trades"]),  # type: ignore[arg-type]
            str(c["setup"]),
        )
    )
    return cohorts


def _classify_decision_payload(payload: Mapping[str, object]) -> tuple[str, bool]:
    ext = payload.get("extraction")
    norm = payload.get("normalized")
    plan = payload.get("planning")
    ext_dict = ext if isinstance(ext, Mapping) else {}
    norm_dict = norm if isinstance(norm, Mapping) else {}
    plan_dict = plan if isinstance(plan, Mapping) else {}
    ext_i = ext_dict.get("intents") or ()
    ext_a = ext_dict.get("position_actions") or ()
    ext_r = ext_dict.get("entry_reviews") or ()
    norm_n = (
        len(norm_dict.get("open_intents") or ())  # type: ignore[arg-type]
        + len(norm_dict.get("position_actions") or ())  # type: ignore[arg-type]
        + len(norm_dict.get("entry_reviews") or ())  # type: ignore[arg-type]
    )
    if not ext_i and not ext_a and not ext_r:
        return "non_signal_commentary", False
    if norm_n > 0:
        has_err = bool(plan_dict.get("errors") or plan_dict.get("rejections"))
        return "normalized_actionable", has_err
    if (
        isinstance(ext_i, list)
        and ext_i
        and all(
            isinstance(i, Mapping) and i.get("relation") == "UPDATE_EXISTING"
            for i in ext_i
        )
        and not ext_a
    ):
        return "update_existing_recap", False
    return "evidence_filtered", False


def build_decision_cohorts(
    database,
    tables: set[str],
    channel_aliases: Mapping[str, str],
) -> list[dict[str, object]]:
    if "source_decisions" not in tables:
        return []
    sm_cols = {row[1] for row in database.execute("PRAGMA table_info(source_messages)")}
    join_sql = (
        "JOIN source_messages s USING(channel_id, message_id) "
        "WHERE d.attempt_count = s.attempt_count"
        if "attempt_count" in sm_cols
        else ""
    )
    counts: dict[str, dict[str, int]] = {
        alias: {
            "audited_posts": 0,
            "non_signal_commentary": 0,
            "normalized_actionable": 0,
            "update_existing_recap": 0,
            "evidence_filtered": 0,
            "planning_rejected": 0,
        }
        for alias in channel_aliases.values()
    }
    for cid, payload_str in database.execute(
        f"SELECT CAST(d.channel_id AS TEXT), d.payload_json FROM source_decisions d {join_sql}"
    ):
        alias = channel_aliases.get(str(cid))
        if alias is None:
            continue
        try:
            payload = json.loads(payload_str)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        bucket = counts[alias]
        bucket["audited_posts"] += 1
        kind, rejected = _classify_decision_payload(payload)
        bucket[kind] += 1
        if rejected:
            bucket["planning_rejected"] += 1
    cohorts: list[dict[str, object]] = []
    for alias, bucket in counts.items():
        total = bucket["audited_posts"]
        if total == 0:
            continue
        cohorts.append(
            {
                "channel": alias,
                **bucket,
                "actionable_ratio_pct": _rounded(
                    100 * bucket["normalized_actionable"] / total, 2
                ),
            }
        )
    cohorts.sort(
        key=lambda c: (
            -int(c["audited_posts"]),  # type: ignore[arg-type]
            str(c["channel"]),
        )
    )
    return cohorts
