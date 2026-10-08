import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cautious_crypto_bro.config import Settings
from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    ExecutionPolicy,
    InstrumentContext,
    Side,
    SourceMessage,
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


def _service(store: PolicyStore, *, profile: str = "baseline") -> SignalService:
    return SignalService(
        store=store,
        extractor=object(),
        planner=object(),
        executor=Wallet(),
        approval_bot=object(),
        coordinator=object(),
        context_provider=object(),
        demo_exit_profile=profile,
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
    with pytest.raises(ValueError):
        _settings(demo_exit_profile="unreviewed")
