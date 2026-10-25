"""Current percentage shorthand must reach execution without weakening evidence gates."""

import asyncio
import json
import sqlite3
from decimal import ROUND_DOWN, Decimal
from unittest.mock import AsyncMock, patch

import pytest
from test_remaining_concerns import executor_for, prepare, source, state_for
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.domain import (
    ApprovalMode,
    AutoApprovalMode,
    IncomingPost,
)
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "caption,kind,expected_pct",
    [
        ("Фикс 25% сейчас", "REDUCE", 25),
        ("Фикс25%", "REDUCE", 25),
        ("Фикс 1/4", "REDUCE", 25),
        ("Профит 80%, фикс 25% сейчас", "REDUCE", 25),
        ("Не фикс 25%", "REDUCE", None),
        ("Завтра фикс 25%", "REDUCE", None),
        ("Если вырастет, фикс 25%", "REDUCE", None),
        ("Вчера фикс 25%", "REDUCE", None),
        ("Фикс без количества", "REDUCE", None),
        ("$ETH фикс 25%", "REDUCE", None),
        ("Фикс 25%", "CLOSE", None),
    ],
)
def test_shorthand_reduction_runs_only_with_current_bound_amount(
    tmp_path, caption, kind, expected_pct
):
    class Model:
        async def complete(self, request):
            return LLMResponse(
                json.dumps(
                    {
                        "actionable": False,  # Advisory; executable evidence is derived below.
                        "reason": "Synthetic lifecycle candidate",
                        "position_actions": [
                            {
                                "symbol": "BTCUSDT",
                                "action": kind,
                                "close_pct": 99 if kind == "REDUCE" else None,
                                "expected_side": "LONG",
                                "evidence_text": caption,
                                "summary": "Synthetic lifecycle action",
                                "confidence": 1,
                            }
                        ],
                    }
                )
            )

        async def close(self):
            pass

    async def run():
        database = tmp_path / "state.db"
        exchange = ExchangeTransport()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, supervisor = await prepare(
                database, exchange, executor
            )
            await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            await supervisor.reconcile_once()
            initial_size = exchange.size
            service, bot, supervisor = workflow(
                store, executor, Model(), mode=AutoApprovalMode.ALL
            )
            post = IncomingPost(source=source(caption, 2))
            before = len(exchange.requests)
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await service.on_message(post)
                with sqlite3.connect(database) as db:
                    actions = db.execute(
                        "SELECT action_id,status,payload_json FROM position_actions"
                    ).fetchall()
                    status = db.execute(
                        "SELECT status FROM source_messages WHERE message_id=2"
                    ).fetchone()
                assert status == ("COMPLETED",)
                if expected_pct is None:
                    assert not actions and exchange.size == initial_size
                    assert not any(
                        path
                        in {
                            "/v5/order/create",
                            "/v5/order/create-batch",
                            "/v5/order/cancel",
                            "/v5/position/trading-stop",
                        }
                        for path, _ in exchange.requests[before:]
                    )
                    return
                assert len(actions) == 1 and actions[0][1] == "EXECUTED"
                assert json.loads(actions[0][2])["close_pct"] == expected_pct
                expected_reduction = (initial_size / 4).quantize(
                    Decimal("0.001"), rounding=ROUND_DOWN
                )
                assert exchange.size == initial_size - expected_reduction
                await supervisor.reconcile_once()
                assert exchange.stop == Decimal("90") and len(exchange.exits()) == 3
                assert not (await state_for(IntentStore(database))).rebalance_needed
                after = len(exchange.requests)
                await service.on_message(post)
                assert not exchange.requests[after:]
                assert (
                    sum(
                        path == "/v5/order/create"
                        and body.get("orderLinkId", "").startswith("ccb-action-")
                        for path, body in exchange.requests[before:]
                    )
                    == 1
                )
            finally:
                await bot.close()

    asyncio.run(run())
