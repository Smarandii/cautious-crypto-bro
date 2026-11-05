"""Keep terminal capacity admission separate from successful sibling outcomes."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from test_remaining_concerns import executor_for, make_intent
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.domain import AutoApprovalMode, IncomingPost
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.source_coverage import coverage_summary
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "cap,mixed,action_status,transient,expected",
    [
        ("100", False, None, False, "PLANNING_REJECTED"),
        ("50", True, "PENDING", False, "PARTIAL_PLANNING_FAILURE"),
        ("50", True, "EXECUTED", False, "PARTIAL_PLANNING_FAILURE"),
        ("50", True, "UNCERTAIN", False, "UNRESOLVED"),
        ("50", True, "EXECUTING", False, "UNRESOLVED"),
        ("50", True, "AUTO_PENDING", False, "UNRESOLVED"),
        ("50", True, "PENDING", True, "UNRESOLVED"),
        ("500", False, None, False, "AWAITING_DECISION"),
    ],
)
def test_original_cap_rejection_is_visible_without_retry_or_orders(
    tmp_path, cap, mixed, action_status, transient, expected
):
    class Model:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            return LLMResponse(
                json.dumps(
                    {
                        "actionable": True,
                        "reason": "Synthetic entry and optional close",
                        "intents": [
                            {
                                "symbol": symbol,
                                "side": "LONG",
                                "entry": {"type": "LIMIT", "price": 100},
                                "stop_loss": 90,
                                "relation": "NEW",
                                "summary": "Enter now",
                                "confidence": 1,
                            }
                            for symbol in (
                                ["ETHUSDT", "PEPEUSDT"] if transient else ["ETHUSDT"]
                            )
                        ],
                        "position_actions": [
                            {
                                "symbol": "BTCUSDT",
                                "action": "CLOSE",
                                "expected_side": "LONG",
                                "evidence_text": "Close BTCUSDT now",
                                "summary": "Close now",
                                "confidence": 1,
                            }
                        ]
                        if mixed
                        else [],
                    }
                )
            )

        async def close(self):
            pass

    class Exchange(ExchangeTransport):
        def __init__(self):
            super().__init__()
            self.size, self.stop = Decimal("10"), Decimal("90")
            self.methods = []

        def handle(self, request):
            self.methods.append(request.method)
            if transient and request.url.params.get("symbol") == "PEPEUSDT":
                raise httpx.ConnectTimeout("Synthetic unavailable contract read")
            return super().handle(request)

    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        source = make_intent().source.model_copy(
            update={"text": "Enter ETHUSDT LONG at 100, stop 90. Close BTCUSDT now."}
        )
        provider, exchange = Model(), Exchange()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store,
                executor,
                provider,
                mode=AutoApprovalMode.DISABLED,
                portfolio_cap=Decimal(cap),
            )
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()):
                    await service.on_message(IncomingPost(source=source))
                    await service.on_message(IncomingPost(source=source))
                assert provider.calls == 1
                assert set(exchange.methods) == {"GET"} and not exchange.orders
                with sqlite3.connect(path) as db:
                    status, attempts, reason = db.execute(
                        "SELECT status,attempt_count,last_error FROM source_messages"
                    ).fetchone()
                    decision = json.loads(
                        db.execute(
                            "SELECT payload_json FROM source_decisions"
                        ).fetchone()[0]
                    )
                    entries = db.execute("SELECT intent_id FROM intents").fetchall()
                    candidates = db.execute(
                        "SELECT payload_json,status FROM planning_candidates"
                    ).fetchall()
                    actions = db.execute(
                        "SELECT action_id FROM position_actions"
                    ).fetchall()
                assert status == "COMPLETED" and attempts == 1
                rejected = Decimal(cap) <= Decimal("100")
                rejections = decision["planning"]["rejections"]
                assert bool(rejections) is rejected
                if rejected:
                    assert rejections == ["ETHUSDT: portfolio stop-risk cap is full"]
                    assert not entries
                    assert bool(candidates) is transient
                    if not mixed:
                        assert "portfolio stop-risk cap" in reason
                else:
                    assert len(entries) == 1 and not candidates
                if candidates:
                    assert len(candidates) == 1 and candidates[0][1] == "PENDING"
                    assert "PEPEUSDT" in candidates[0][0]
                assert len(actions) == int(mixed)
                if action_status == "EXECUTED":
                    action = await store.get_position_action(actions[0][0])
                    assert action
                    await store.complete_position_action(action, "synthetic-close")
                elif action_status in {"UNCERTAIN", "EXECUTING", "AUTO_PENDING"}:
                    # Seed the persisted execution boundary without submitting orders.
                    with sqlite3.connect(path) as db:
                        db.execute(
                            "UPDATE position_actions SET status=?,approval_mode=? WHERE action_id=?",
                            (
                                "PENDING"
                                if action_status == "AUTO_PENDING"
                                else action_status,
                                "AUTO" if action_status == "AUTO_PENDING" else "MANUAL",
                                actions[0][0],
                            ),
                        )
                before = path.read_bytes()
                report = await coverage_summary(
                    path, since=datetime.now(UTC) - timedelta(days=1)
                )
                assert path.read_bytes() == before
                assert report["dispositions"] == {expected: 1}
                if rejected:
                    assert report["planning_rejection_reasons"] == {
                        "portfolio_risk_cap": 1
                    }
                assert source.text not in json.dumps(report)
            finally:
                await bot.close()

    asyncio.run(run())
