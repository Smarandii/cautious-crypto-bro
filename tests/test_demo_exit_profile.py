import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from cautious_crypto_bro.config import Settings
from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionPolicy,
    InstrumentContext,
    Side,
    SourceMessage,
    StrategyV2Policy,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.service import SignalService


class PolicyStore:
    def __init__(self) -> None:
        self.policy = ExecutionPolicy()

    async def get_execution_policy(self) -> ExecutionPolicy:
        return self.policy


class Wallet:
    async def wallet_balance_usdt(self) -> Decimal:
        return Decimal("1000")


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_source_channels=[1],
        telegram_bot_token="token",
        telegram_approver_user_id=1,
        telegram_approval_chat_id=1,
        bybit_api_key="demo-key",
        bybit_api_secret="demo-secret",
        **overrides,
    )


def _service(
    store: PolicyStore,
    *,
    profile: str = "baseline",
    long_risk_multiplier: Decimal = Decimal("1"),
    control_fraction: Decimal = Decimal("0"),
    participation_skip_fraction: Decimal = Decimal("0"),
    entry_order_ttl_minutes: int = 0,
) -> SignalService:
    return SignalService(
        store=store,
        extractor=object(),
        planner=object(),
        executor=Wallet(),
        approval_bot=object(),
        coordinator=object(),
        context_provider=object(),
        demo_exit_profile=profile,
        demo_long_risk_multiplier=long_risk_multiplier,
        demo_long_exit_control_fraction=control_fraction,
        demo_long_participation_skip_fraction=participation_skip_fraction,
        demo_entry_order_ttl_minutes=entry_order_ttl_minutes,
    )


def test_payoff_challenger_applies_only_to_new_policy_snapshot() -> None:
    async def run() -> None:
        store = PolicyStore()
        service = _service(store, profile="payoff_challenger")

        policy = await service._capital_frozen_policy()

        assert policy.trading_capital_usdt == Decimal("1000")
        assert policy.strategy_v2.entry_rules == store.policy.strategy_v2.entry_rules
        assert policy.strategy_v2.exit_rules == (
            ("TP1", Decimal("1"), Decimal("15")),
            ("TP2", Decimal("2"), Decimal("20")),
            ("TP3", Decimal("4"), Decimal("25")),
        )
        assert policy.strategy_v2.runner_pct == Decimal("40")
        assert policy.strategy_v2.trailing_activation_r == Decimal("0.4")
        assert policy.strategy_v2.trailing_distance_r == Decimal("0.1")

        # The stored baseline remains unchanged; existing plans use their own
        # serialized policy and are not rewritten by this new-plan setting.
        assert store.policy.strategy_v2.exit_rules == (
            ("TP1", Decimal("0.5"), Decimal("25")),
            ("TP2", Decimal("1"), Decimal("25")),
            ("TP3", Decimal("1.5"), Decimal("25")),
        )
        assert store.policy.strategy_v2.trailing_activation_r == Decimal("0.5")

    asyncio.run(run())


def test_payoff_early_trail_is_separately_tagged_and_uses_020r_activation() -> None:
    async def run() -> None:
        store = PolicyStore()
        policy = await _service(
            store, profile="payoff_early_trail"
        )._capital_frozen_policy()

        assert policy.strategy_v2.exit_profile == "payoff_early_trail"
        assert policy.strategy_v2.trailing_activation_r == Decimal("0.2")
        assert policy.strategy_v2.trailing_distance_r == Decimal("0.1")
        assert policy.strategy_v2.exit_rules == (
            ("TP1", Decimal("1"), Decimal("15")),
            ("TP2", Decimal("2"), Decimal("20")),
            ("TP3", Decimal("4"), Decimal("25")),
        )
        assert store.policy.strategy_v2.trailing_activation_r == Decimal("0.5")

    asyncio.run(run())


def test_payoff_early_tight_trail_is_separately_tagged_for_new_plans() -> None:
    async def run() -> None:
        store = PolicyStore()
        policy = await _service(
            store, profile="payoff_early_tight_trail"
        )._capital_frozen_policy()

        assert policy.strategy_v2.exit_profile == "payoff_early_tight_trail"
        assert policy.strategy_v2.trailing_activation_r == Decimal("0.2")
        assert policy.strategy_v2.trailing_distance_r == Decimal("0.05")
        assert policy.strategy_v2.entry_rules == store.policy.strategy_v2.entry_rules
        assert policy.strategy_v2.exit_rules == (
            ("TP1", Decimal("1"), Decimal("15")),
            ("TP2", Decimal("2"), Decimal("20")),
            ("TP3", Decimal("4"), Decimal("25")),
        )
        assert policy.strategy_v2.runner_pct == Decimal("40")
        assert store.policy.strategy_v2.trailing_distance_r == Decimal("0.3")

    asyncio.run(run())


def test_long_015_profile_changes_only_long_activation_and_keeps_reduced_risk() -> None:
    async def run() -> None:
        service = _service(
            PolicyStore(),
            profile="payoff_early_tight_trail_long_015",
            long_risk_multiplier=Decimal("0.10"),
        )
        policy = await service._capital_frozen_policy()

        long_policy = service._policy_for_intent_side(policy, Side.LONG)
        short_policy = service._policy_for_intent_side(policy, Side.SHORT)

        assert long_policy.strategy_v2.exit_profile == (
            "payoff_early_tight_trail_long_015"
        )
        assert long_policy.strategy_v2.trailing_activation_r == Decimal("0.15")
        assert long_policy.strategy_v2.trailing_distance_r == Decimal("0.05")
        assert long_policy.risk_per_trade_pct == policy.risk_per_trade_pct * Decimal(
            "0.10"
        )
        assert short_policy.strategy_v2.exit_profile == (
            "payoff_early_tight_trail_long_015"
        )
        assert short_policy.strategy_v2.trailing_activation_r == Decimal("0.20")
        assert short_policy.strategy_v2.trailing_distance_r == Decimal("0.05")
        assert short_policy.risk_per_trade_pct == policy.risk_per_trade_pct

    asyncio.run(run())


def test_long_exit_ab_assignment_is_stable_and_changes_only_profile_activation() -> (
    None
):
    async def run() -> None:
        service = _service(
            PolicyStore(),
            profile="payoff_early_tight_trail_long_015",
            long_risk_multiplier=Decimal("0.10"),
            control_fraction=Decimal("0.5"),
            entry_order_ttl_minutes=240,
        )
        policy = await service._capital_frozen_policy()
        assigned = [
            service._policy_for_intent_side(
                policy,
                Side.LONG,
                UUID(int=index),
            )
            for index in range(1, 101)
        ]

        profiles = [item.strategy_v2.exit_profile for item in assigned]
        controls = [
            item
            for item in assigned
            if item.strategy_v2.exit_profile
            == "payoff_early_tight_trail_long_ab_020_control"
        ]
        treatments = [
            item
            for item in assigned
            if item.strategy_v2.exit_profile == "payoff_early_tight_trail_long_ab_015"
        ]

        assert profiles == [
            service._policy_for_intent_side(
                policy, Side.LONG, UUID(int=index)
            ).strategy_v2.exit_profile
            for index in range(1, 101)
        ]
        assert 35 <= len(controls) <= 65
        assert len(controls) + len(treatments) == 100
        assert all(
            item.strategy_v2.trailing_activation_r == Decimal("0.20")
            and item.strategy_v2.trailing_distance_r == Decimal("0.05")
            and item.risk_per_trade_pct == Decimal("0.10")
            and item.strategy_v2.entry_order_ttl_minutes == 240
            for item in controls
        )
        assert all(
            item.strategy_v2.trailing_activation_r == Decimal("0.15")
            and item.strategy_v2.trailing_distance_r == Decimal("0.05")
            and item.risk_per_trade_pct == Decimal("0.10")
            and item.strategy_v2.entry_order_ttl_minutes == 240
            for item in treatments
        )
        assert {
            StrategyV2Policy.model_validate_json(
                item.strategy_v2.model_dump_json()
            ).exit_profile
            for item in controls + treatments
        } == {
            "payoff_early_tight_trail_long_ab_020_control",
            "payoff_early_tight_trail_long_ab_015",
        }

    asyncio.run(run())


def test_long_exit_ab_requires_intent_id_and_long_015_profile() -> None:
    service = _service(
        PolicyStore(),
        profile="payoff_early_tight_trail_long_015",
        control_fraction=Decimal("0.5"),
    )

    with pytest.raises(ValueError, match="requires an intent ID"):
        service._policy_for_intent_side(
            ExecutionPolicy(),
            Side.LONG,
        )

    with pytest.raises(ValueError, match="requires the long_015 profile"):
        _service(
            PolicyStore(),
            profile="payoff_early_tight_trail",
            control_fraction=Decimal("0.5"),
        )


def test_long_participation_assignment_is_stable_separate_and_auto_only() -> None:
    service = _service(
        PolicyStore(),
        profile="payoff_early_tight_trail_long_015",
        long_risk_multiplier=Decimal("0.10"),
        control_fraction=Decimal("0.50"),
        participation_skip_fraction=Decimal("0.50"),
    )
    ids = [UUID(int=index) for index in range(1, 101)]

    assigned = [
        service._policy_for_intent_side(ExecutionPolicy(), Side.LONG, intent_id)
        for intent_id in ids
    ]
    repeated = [
        service._policy_for_intent_side(ExecutionPolicy(), Side.LONG, intent_id)
        for intent_id in ids
    ]
    arms = [item.strategy_v2.long_participation_arm for item in assigned]

    assert arms == [item.strategy_v2.long_participation_arm for item in repeated]
    assert 35 <= arms.count("take") <= 65
    assert 35 <= arms.count("skip") <= 65
    assert all(item.risk_per_trade_pct == Decimal("0.10") for item in assigned)
    assert all(
        item.strategy_v2.exit_profile
        in {
            "payoff_early_tight_trail_long_ab_015",
            "payoff_early_tight_trail_long_ab_020_control",
        }
        for item in assigned
    )

    short = service._policy_for_intent_side(
        ExecutionPolicy(), Side.SHORT, UUID(int=999)
    )
    manual = service._policy_for_intent_side(
        ExecutionPolicy(),
        Side.LONG,
        UUID(int=1000),
        participation_eligible=False,
    )
    assert short.strategy_v2.long_participation_arm is None
    assert manual.strategy_v2.long_participation_arm is None


def test_long_participation_requires_intent_id_when_enabled() -> None:
    service = _service(PolicyStore(), participation_skip_fraction=Decimal("0.50"))
    with pytest.raises(ValueError, match="requires an intent ID"):
        service._policy_for_intent_side(ExecutionPolicy(), Side.LONG)


def test_new_execution_plan_serializes_payoff_challenger_profile() -> None:
    async def run() -> None:
        policy = await _service(
            PolicyStore(), profile="payoff_challenger"
        )._capital_frozen_policy()
        now = datetime.now(UTC)
        intent = TradingIntent(
            source=SourceMessage(
                channel_id=1,
                channel_title="test",
                channel_username=None,
                message_id=1,
                published_at=now,
                received_at=now,
                text="demo profile verification",
            ),
            symbol="BTCUSDT",
            side=Side.LONG,
            entry=Entry(type=EntryType.MARKET),
            stop_loss=90,
            take_profit=None,
            summary="profile integration test",
            confidence=0.99,
        )
        context = InstrumentContext(
            market_price=Decimal("100"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

        plan = ExecutionPlanner().plan(intent, policy, context)
        restored = type(plan).model_validate_json(plan.model_dump_json())

        assert restored.policy.strategy_v2.exit_profile == "payoff_challenger"
        assert [target.r_multiple for target in restored.take_profit_targets] == [
            Decimal("1"),
            Decimal("2"),
            Decimal("4"),
        ]
        assert [target.close_pct for target in restored.take_profit_targets] == [
            Decimal("15"),
            Decimal("20"),
            Decimal("25"),
        ]
        assert restored.runner_pct == Decimal("40")
        assert restored.policy.strategy_v2.entry_order_ttl_minutes == 0

    asyncio.run(run())


def test_baseline_demo_exit_profile_preserves_existing_policy() -> None:
    async def run() -> None:
        store = PolicyStore()
        policy = await _service(store)._capital_frozen_policy()

        assert policy.strategy_v2 == store.policy.strategy_v2

    asyncio.run(run())


def test_unsupported_demo_exit_profile_is_rejected() -> None:
    store = PolicyStore()

    with pytest.raises(ValueError, match="Unsupported Demo exit profile"):
        _service(store, profile="unreviewed")


def test_demo_exit_profile_setting_is_opt_in_and_validated() -> None:
    assert _settings().demo_exit_profile == "baseline"
    assert _settings(demo_exit_profile="payoff_challenger").demo_exit_profile == (
        "payoff_challenger"
    )
    assert _settings(demo_exit_profile="payoff_early_trail").demo_exit_profile == (
        "payoff_early_trail"
    )
    assert (
        _settings(demo_exit_profile="payoff_early_tight_trail").demo_exit_profile
        == "payoff_early_tight_trail"
    )
    assert (
        _settings(
            demo_exit_profile="payoff_early_tight_trail_long_015"
        ).demo_exit_profile
        == "payoff_early_tight_trail_long_015"
    )
    assert _settings(
        demo_long_exit_control_fraction=Decimal("0.5")
    ).demo_long_exit_control_fraction == Decimal("0.5")
    assert _settings(
        demo_long_participation_skip_fraction=Decimal("0.5")
    ).demo_long_participation_skip_fraction == Decimal("0.5")
    with pytest.raises(ValueError):
        _settings(demo_long_participation_skip_fraction=Decimal("1.1"))
    with pytest.raises(ValueError):
        _settings(demo_long_exit_control_fraction=Decimal("1.1"))
    with pytest.raises(ValueError):
        _settings(demo_exit_profile="unreviewed")


def test_demo_portfolio_stop_risk_cap_is_optional_and_positive() -> None:
    assert _settings().demo_portfolio_stop_risk_cap_usdt is None
    assert _settings(
        demo_portfolio_stop_risk_cap_usdt=Decimal("340")
    ).demo_portfolio_stop_risk_cap_usdt == Decimal("340")
    with pytest.raises(ValueError):
        _settings(demo_portfolio_stop_risk_cap_usdt=Decimal("0"))


def test_demo_entry_order_ttl_defaults_off_and_rejects_negative_values() -> None:
    assert _settings().demo_entry_order_ttl_minutes == 0
    assert (
        _settings(demo_entry_order_ttl_minutes=240).demo_entry_order_ttl_minutes == 240
    )
    with pytest.raises(ValueError):
        _settings(demo_entry_order_ttl_minutes=-1)


def test_entry_order_ttl_is_opt_in_and_serialized_into_new_plans() -> None:
    async def run() -> None:
        policy = await _service(
            PolicyStore(), entry_order_ttl_minutes=240
        )._capital_frozen_policy()
        now = datetime.now(UTC)
        intent = TradingIntent(
            source=SourceMessage(
                channel_id=1,
                channel_title="test",
                message_id=2,
                published_at=now,
                received_at=now,
                text="new TTL plan",
            ),
            symbol="BTCUSDT",
            side=Side.LONG,
            entry=Entry(type=EntryType.MARKET),
            stop_loss=90,
            take_profit=None,
            summary="TTL policy serialization",
            confidence=0.99,
        )
        context = InstrumentContext(
            market_price=Decimal("100"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        plan = ExecutionPlanner().plan(intent, policy, context)
        restored = type(plan).model_validate_json(plan.model_dump_json())

        assert restored.policy.strategy_v2.entry_order_ttl_minutes == 240

    asyncio.run(run())
