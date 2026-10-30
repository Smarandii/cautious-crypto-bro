import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cautious_crypto_bro.domain import IntentStatus
from cautious_crypto_bro.execution_coordinator import (
    IntentExecutionOutcome,
    PositionActionExecutionOutcome,
)
from cautious_crypto_bro.signals.delivery import SignalDelivery


@pytest.mark.parametrize(
    "kind", [IntentExecutionOutcome, PositionActionExecutionOutcome]
)
@pytest.mark.parametrize("failure", [None, "state", "pnl", "preview"])
def test_auto_preview_precedes_outcome_and_failures_do_not_hide_result(kind, failure):
    async def run():
        events = []
        state, pnl = object(), object()

        async def preview(value, **kwargs):
            events.append("preview")
            assert value is (None if failure == "state" else state)
            assert kwargs["pnl"] is (None if failure == "pnl" else pnl)
            assert bool(kwargs["state_error"]) is (failure == "state")
            assert bool(kwargs["pnl_error"]) is (failure == "pnl")
            if failure == "preview":
                raise RuntimeError("Telegram unavailable")

        async def outcome(value):
            events.append("outcome")

        executor = SimpleNamespace(
            account_state=AsyncMock(
                return_value=state,
                side_effect=RuntimeError("Bybit unavailable")
                if failure == "state"
                else None,
            )
        )
        store = SimpleNamespace(
            get_account_pnl_summary=AsyncMock(
                return_value=pnl,
                side_effect=RuntimeError("PnL unavailable")
                if failure == "pnl"
                else None,
            )
        )
        sender = SimpleNamespace(
            send_account_snapshot=AsyncMock(side_effect=preview),
            send_auto_intent_outcome=AsyncMock(side_effect=outcome),
            send_auto_action_outcome=AsyncMock(side_effect=outcome),
        )
        delivery = SignalDelivery(store=store, executor=executor, approval_bot=sender)
        result = kind(status=IntentStatus.EXECUTED, message="done")
        await delivery._send_auto_outcome(result)
        assert events == ["preview", "outcome"]
        selected = (
            sender.send_auto_intent_outcome
            if kind is IntentExecutionOutcome
            else sender.send_auto_action_outcome
        )
        selected.assert_awaited_once_with(result)

    asyncio.run(run())
