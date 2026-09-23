"""Safety checks for the opt-in live suite; these tests never access a network."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cautious_crypto_bro.domain import Side

spec = importlib.util.spec_from_file_location(
    "demo_e2e", Path(__file__).parents[1] / "scripts" / "e2e_bybit_demo.py"
)
assert spec is not None and spec.loader is not None
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_live_suite_requires_opt_in_before_creating_client(monkeypatch):
    def forbidden():
        raise AssertionError("Client must not be created without opt-in")

    monkeypatch.setattr(demo, "executor", forbidden)
    with pytest.raises(AssertionError, match="Explicit --execute-demo required"):
        asyncio.run(demo.main(SimpleNamespace(resume_db=None, execute_demo=False)))


def test_cleanup_ignores_other_symbols():
    fake = SimpleNamespace(
        account_state=AsyncMock(
            return_value=SimpleNamespace(
                positions=[SimpleNamespace(symbol="BTCUSDT", side=Side.LONG)],
                open_orders=[],
            )
        ),
        execute_position_action=AsyncMock(),
        cancel_order=AsyncMock(),
    )
    asyncio.run(demo.cleanup(fake, Side.LONG, "ccb-v2-owned-"))
    fake.execute_position_action.assert_not_called()
    fake.cancel_order.assert_not_called()


def test_cleanup_refuses_unowned_orders_on_test_symbol():
    fake = SimpleNamespace(
        account_state=AsyncMock(
            return_value=SimpleNamespace(
                positions=[],
                open_orders=[
                    SimpleNamespace(
                        symbol=demo.SYMBOL,
                        order_link_id="other-owner",
                        is_protective=False,
                    )
                ],
            )
        ),
        execute_position_action=AsyncMock(),
        cancel_order=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="Cleanup unverified"):
        asyncio.run(demo.cleanup(fake, Side.LONG, "ccb-v2-owned-"))
    fake.execute_position_action.assert_not_called()
    fake.cancel_order.assert_not_called()


def test_poll_failure_is_bounded():
    fake = SimpleNamespace(account_state=AsyncMock(return_value=object()))
    with pytest.raises(AssertionError, match="Timed out: never ready"):
        asyncio.run(demo.wait_account(fake, lambda _: False, "never ready", seconds=0))
    fake.account_state.assert_awaited_once()
