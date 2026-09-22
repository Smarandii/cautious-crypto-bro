import asyncio
from decimal import Decimal

from cautious_crypto_bro.domain import (
    ExecutionPolicy,
    StrategyV2Policy,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)


def test_execution_policy_defaults_and_updates(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "test.sqlite3")

        await store.initialize()

        default = await store.get_execution_policy()

        assert default.trading_capital_usdt == Decimal("6800")
        assert default.risk_per_trade_pct == Decimal("1")
        assert default.range_order_count == 3

        updated = ExecutionPolicy(
            trading_capital_usdt=(Decimal("6800")),
            risk_per_trade_pct=(Decimal("2")),
            range_order_count=5,
        )

        await store.set_execution_policy(updated)

        loaded = await store.get_execution_policy()

        assert loaded == updated
        assert loaded.risk_budget_usdt == Decimal("136")

    asyncio.run(run())


def test_strategy_v2_defaults() -> None:
    policy = StrategyV2Policy()

    assert policy.entry_rules == (
        (
            Decimal("0"),
            Decimal("60"),
        ),
        (
            Decimal("0.33"),
            Decimal("25"),
        ),
        (
            Decimal("0.66"),
            Decimal("15"),
        ),
    )

    assert policy.exit_rules == (
        (
            "TP1",
            Decimal("0.5"),
            Decimal("25"),
        ),
        (
            "TP2",
            Decimal("1"),
            Decimal("25"),
        ),
        (
            "TP3",
            Decimal("1.5"),
            Decimal("25"),
        ),
    )

    assert policy.runner_pct == Decimal("25")
    assert policy.trailing_activation_r == Decimal("0.5")
    assert policy.trailing_distance_r == Decimal("0.3")
    assert policy.minimum_locked_profit_r == Decimal("0.05")


def test_strategy_v2_rejects_invalid_entry_weights() -> None:
    try:
        StrategyV2Policy(
            primary_entry_risk_pct=Decimal("50"),
        )
    except ValueError as exc:
        assert "entry risk percentages" in str(exc)
    else:
        raise AssertionError("Expected invalid V2 entry weights")


def test_strategy_v2_rejects_invalid_exit_allocation() -> None:
    try:
        StrategyV2Policy(
            runner_pct=Decimal("20"),
        )
    except ValueError as exc:
        assert "fixed exits and runner" in str(exc)
    else:
        raise AssertionError("Expected invalid V2 exit allocation")


def test_strategy_v2_rejects_non_positive_initial_floor() -> None:
    try:
        StrategyV2Policy(
            trailing_activation_r=Decimal("0.5"),
            trailing_distance_r=Decimal("0.5"),
        )
    except ValueError as exc:
        assert "trailing distance" in str(exc)
    else:
        raise AssertionError("Expected invalid V2 trailing geometry")
