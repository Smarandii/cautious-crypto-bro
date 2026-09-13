import asyncio
from decimal import Decimal

from cautious_crypto_bro.domain import (
    ExecutionPolicy,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)


def test_execution_policy_defaults_and_updates(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(
            tmp_path
            / "test.sqlite3"
        )

        await store.initialize()

        default = (
            await store
            .get_execution_policy()
        )

        assert (
            default.trading_capital_usdt
            == Decimal("6800")
        )
        assert (
            default.risk_per_trade_pct
            == Decimal("1")
        )
        assert (
            default.range_order_count
            == 3
        )

        updated = ExecutionPolicy(
            trading_capital_usdt=(
                Decimal("6800")
            ),
            risk_per_trade_pct=(
                Decimal("2")
            ),
            range_order_count=5,
        )

        await store.set_execution_policy(
            updated
        )

        loaded = (
            await store
            .get_execution_policy()
        )

        assert loaded == updated
        assert (
            loaded.risk_budget_usdt
            == Decimal("136")
        )

    asyncio.run(
        run()
    )


def test_exit_policy_defaults() -> None:
    policy = ExecutionPolicy(
        trading_capital_usdt=Decimal("6800"),
        risk_per_trade_pct=Decimal("1"),
        range_order_count=3,
    )

    assert (
        policy.exit_policy.basic_r_multiple
        == Decimal("0.5")
    )
    assert (
        policy.exit_policy.medium_r_multiple
        == Decimal("1")
    )
    assert (
        policy.exit_policy.high_r_multiple
        == Decimal("2")
    )

    assert (
        policy.exit_policy.basic_close_pct
        + policy.exit_policy.medium_close_pct
        + policy.exit_policy.high_close_pct
        == Decimal("100")
    )
