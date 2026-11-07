"""Pure Demo exit-profile, A/B control, and LONG participation-arm assignment."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from uuid import UUID

from ..domain import ExecutionPolicy, Side, StrategyV2Policy


def freeze_capital_policy(
    policy: ExecutionPolicy,
    trading_capital_usdt: Decimal,
    *,
    demo_exit_profile: str = "baseline",
    demo_entry_order_ttl_minutes: int = 0,
) -> ExecutionPolicy:
    updates: dict[str, object] = {"trading_capital_usdt": trading_capital_usdt}
    strategy = policy.strategy_v2
    strategy_changed = False
    if demo_exit_profile in {
        "payoff_challenger",
        "payoff_early_trail",
        "payoff_early_tight_trail",
        "payoff_early_tight_trail_long_015",
    }:
        trailing_activation_r = (
            Decimal("0.2")
            if demo_exit_profile
            in {
                "payoff_early_trail",
                "payoff_early_tight_trail",
                "payoff_early_tight_trail_long_015",
            }
            else Decimal("0.4")
        )
        strategy = strategy.model_copy(
            update={
                "exit_profile": demo_exit_profile,
                "first_take_profit_r": Decimal("1"),
                "second_take_profit_r": Decimal("2"),
                "third_take_profit_r": Decimal("4"),
                "first_take_profit_pct": Decimal("15"),
                "second_take_profit_pct": Decimal("20"),
                "third_take_profit_pct": Decimal("25"),
                "runner_pct": Decimal("40"),
                "trailing_activation_r": trailing_activation_r,
                "trailing_distance_r": (
                    Decimal("0.05")
                    if demo_exit_profile
                    in {
                        "payoff_early_tight_trail",
                        "payoff_early_tight_trail_long_015",
                    }
                    else Decimal("0.1")
                ),
            }
        )
        strategy_changed = True
    if demo_entry_order_ttl_minutes:
        strategy = strategy.model_copy(
            update={
                "entry_order_ttl_minutes": demo_entry_order_ttl_minutes,
            }
        )
        strategy_changed = True
    if strategy_changed:
        updates["strategy_v2"] = strategy
    return policy.model_copy(update=updates)


def _assign_exit_profile_arm(
    strategy: StrategyV2Policy,
    side: Side,
    intent_id: UUID | None,
    *,
    demo_exit_profile: str,
    demo_long_exit_control_fraction: Decimal,
) -> StrategyV2Policy:
    if (
        demo_long_exit_control_fraction > 0
        and demo_exit_profile == "payoff_early_tight_trail_long_015"
    ):
        if side is Side.SHORT:
            return strategy.model_copy(
                update={"exit_profile": "payoff_early_tight_trail"}
            )
        if intent_id is None:
            raise ValueError("Randomized LONG exit assignment requires an intent ID")
        control_bucket = int.from_bytes(
            hashlib.sha256(intent_id.bytes).digest()[:8],
            "big",
        )
        control_threshold = int(demo_long_exit_control_fraction * (1 << 64))
        is_control = control_bucket < control_threshold
        return strategy.model_copy(
            update={
                "exit_profile": (
                    "payoff_early_tight_trail_long_ab_020_control"
                    if is_control
                    else "payoff_early_tight_trail_long_ab_015"
                ),
                "trailing_activation_r": (
                    Decimal("0.20") if is_control else Decimal("0.15")
                ),
            }
        )
    if side is Side.LONG and demo_exit_profile == "payoff_early_tight_trail_long_015":
        return strategy.model_copy(update={"trailing_activation_r": Decimal("0.15")})
    return strategy


def assign_participation_arm(
    strategy: StrategyV2Policy,
    side: Side,
    intent_id: UUID | None,
    momentum_stratum: str | None,
    *,
    demo_long_momentum_gate: bool,
    demo_long_participation_skip_fraction: Decimal,
) -> StrategyV2Policy:
    """Assign the Demo LONG take/skip arm; the gate beats the coin flip."""
    if intent_id is None:
        raise ValueError(
            "Randomized LONG participation assignment requires an intent ID"
        )
    participation_updates: dict[str, object] = {}
    gated = demo_long_momentum_gate and momentum_stratum is not None
    if gated:
        participation_updates["long_participation_momentum"] = (
            "positive" if momentum_stratum == "skip_positive" else "nonpositive"
        )
        participation_updates["long_participation_arm"] = (
            "take" if momentum_stratum == "skip_positive" else "skip"
        )
        return strategy.model_copy(update=participation_updates)

    if demo_long_momentum_gate:
        participation_updates["long_participation_momentum"] = "unavailable"
    participation_bucket = int.from_bytes(
        hashlib.sha256(b"long-participation-v1:" + intent_id.bytes).digest()[:8],
        "big",
    )
    skip_threshold = int(demo_long_participation_skip_fraction * (1 << 64))
    participation_updates["long_participation_arm"] = (
        "skip" if participation_bucket < skip_threshold else "take"
    )
    return strategy.model_copy(update=participation_updates)


def policy_for_intent_side(
    policy: ExecutionPolicy,
    side: Side,
    intent_id: UUID | None = None,
    *,
    demo_long_risk_multiplier: Decimal,
    demo_exit_profile: str,
    demo_long_exit_control_fraction: Decimal,
    demo_long_momentum_gate: bool,
    demo_long_participation_skip_fraction: Decimal,
    participation_eligible: bool = True,
    momentum_stratum: str | None = None,
) -> ExecutionPolicy:
    strategy = policy.strategy_v2
    updates: dict[str, object] = {}
    if side is Side.LONG:
        updates["risk_per_trade_pct"] = (
            policy.risk_per_trade_pct * demo_long_risk_multiplier
        )
    strategy = _assign_exit_profile_arm(
        strategy,
        side,
        intent_id,
        demo_exit_profile=demo_exit_profile,
        demo_long_exit_control_fraction=demo_long_exit_control_fraction,
    )
    if (
        side is Side.LONG
        and participation_eligible
        and (demo_long_momentum_gate or demo_long_participation_skip_fraction > 0)
    ):
        strategy = assign_participation_arm(
            strategy,
            side,
            intent_id,
            momentum_stratum,
            demo_long_momentum_gate=demo_long_momentum_gate,
            demo_long_participation_skip_fraction=demo_long_participation_skip_fraction,
        )
    if strategy != policy.strategy_v2:
        updates["strategy_v2"] = strategy
    return policy.model_copy(update=updates) if updates else policy
