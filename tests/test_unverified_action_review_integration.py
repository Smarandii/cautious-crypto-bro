"""Unverified close symbols reach humans without becoming executable approvals."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from test_remaining_concerns import executor_for
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.domain import (
    ApprovalMode,
    AutoApprovalMode,
    ImageAttachment,
    IncomingPost,
    IntentStatus,
    PositionActionIntent,
    SourceMessage,
)
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.signals.delivery import SignalDelivery
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class ActionModel:
    def __init__(self, *, symbol="BTCCUSDT", action="CLOSE", side="LONG"):
        self.symbol, self.action, self.side = symbol, action, side

    async def complete(self, request):
        caption = "Reduce 50% now" if self.action == "REDUCE" else "Close now"
        return LLMResponse(
            json.dumps(
                {
                    "actionable": True,
                    "reason": "Synthetic image close instruction",
                    "position_actions": [
                        {
                            "symbol": self.symbol,
                            "action": self.action,
                            "expected_side": self.side,
                            "close_pct": 50 if self.action == "REDUCE" else None,
                            "evidence_text": caption,
                            "summary": "Close now",
                            "confidence": 1,
                        }
                    ],
                }
            )
        )


class MissingContractExchange(ExchangeTransport):
    def __init__(self, *, lookup="missing", account_available=True):
        super().__init__()
        self.lookup = lookup
        self.account_available = account_available

    def handle(self, request):
        if request.url.path == "/v5/position/list" and not self.account_available:
            raise httpx.ConnectError("Synthetic account outage", request=request)
        if request.url.path == "/v5/market/instruments-info" and self.lookup != "valid":
            self.requests.append((request.url.path, dict(request.url.params)))
            if self.lookup == "outage":
                raise httpx.ConnectError(
                    "synthetic-private-error-must-not-leak", request=request
                )
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
        return super().handle(request)


def post(*, action="CLOSE", age=0, recovered=False):
    return IncomingPost(
        source=SourceMessage(
            channel_id=-1001234567890,
            channel_title="Synthetic channel",
            message_id=1,
            published_at=datetime.now(UTC) - timedelta(seconds=age),
            received_at=datetime.now(UTC),
            text="Reduce 50% now" if action == "REDUCE" else "Close now",
            recovery_review_reason="Historical instruction" if recovered else None,
        ),
        images=(ImageAttachment(media_type="image/png", data=b"synthetic transport"),),
    )


@pytest.mark.parametrize("action", ["CLOSE", "REDUCE"])
@pytest.mark.parametrize("lookup", ["missing", "outage"])
@pytest.mark.parametrize("account_available", [True, False])
def test_unverified_close_delivers_review_instead_of_silent_skip(
    tmp_path, action, lookup, account_available
):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        exchange = MissingContractExchange(
            lookup=lookup, account_available=account_available
        )
        exchange.size = Decimal("1")
        incoming = post(action=action)
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, ActionModel(action=action), mode=AutoApprovalMode.ALL
            )
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()) as sender:
                    await service.on_message(incoming)
                    await service.on_message(incoming)
                assert sender.await_count == 1
                message = sender.call_args.kwargs
                assert "CONTRACT NEEDS REVIEW" in message["text"]
                assert (
                    "BTCCUSDT" in message["text"]
                    and incoming.source.telegram_url in message["text"]
                )
                assert not message.get("reply_markup") and not exchange.orders
                assert "synthetic-private-error-must-not-leak" not in message["text"]
                with closing(sqlite3.connect(path)) as db:
                    assert db.execute(
                        "SELECT status,approval_mode FROM position_actions"
                    ).fetchall() == [("SKIPPED", "MANUAL")]
                    assert db.execute(
                        "SELECT delivered_at FROM manual_deliveries"
                    ).fetchone()[0]
                    assert (
                        db.execute("SELECT status FROM source_messages").fetchone()[0]
                        == "COMPLETED"
                    )
                    action_id, payload, error = db.execute(
                        "SELECT action_id,payload_json,error FROM position_actions"
                    ).fetchone()
                    saved_payload = json.loads(payload)
                    assert saved_payload["symbol"] == "BTCCUSDT"
                    assert (
                        "contract_review_reason" not in saved_payload
                        and "status" not in saved_payload
                    )
                    assert (
                        PositionActionIntent.model_validate_json(payload).symbol
                        == "BTCCUSDT"
                    )
                    assert error.startswith("Unverified lifecycle contract: ")
                from uuid import UUID

                before = list(exchange.requests)
                outcome = await service._coordinator.execute_position_action(
                    UUID(action_id), approval_mode=ApprovalMode.MANUAL, user_id=1
                )
                assert outcome.status is IntentStatus.SKIPPED
                assert exchange.requests == before and not exchange.orders
            finally:
                await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "case", ["valid_unheld", "opposite_held", "delisted_held", "stale_recovered"]
)
def test_review_does_not_change_existing_skip_exit_or_expiry_paths(tmp_path, case):
    async def run():
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        exchange = MissingContractExchange(
            lookup="valid" if case == "valid_unheld" else "missing"
        )
        exchange.size = Decimal("1")
        model = ActionModel(
            symbol="BTCUSDT"
            if "held" in case and case != "valid_unheld"
            else "BTCCUSDT",
            side="SHORT" if case == "opposite_held" else "LONG",
        )
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, model, mode=AutoApprovalMode.OPEN_ONLY
            )
            try:
                with patch.object(bot._bot, "send_message", new=AsyncMock()) as sender:
                    await service.on_message(
                        post(
                            age=1800 if case == "stale_recovered" else 0,
                            recovered=case == "stale_recovered",
                        )
                    )
                if case in {"valid_unheld", "opposite_held"}:
                    assert sender.await_count == 0
                elif case == "delisted_held":
                    assert sender.await_count == 2 and sender.call_args.kwargs.get(
                        "reply_markup"
                    )
                else:
                    assert (
                        sender.await_count == 1
                        and "EXPIRED LIFECYCLE" in sender.call_args.kwargs["text"]
                    )
                    assert not sender.call_args.kwargs.get("reply_markup")
                reads = [
                    p
                    for p, _ in exchange.requests
                    if p == "/v5/market/instruments-info"
                ]
                assert len(reads) == (1 if case == "valid_unheld" else 0)
                assert not exchange.orders
            finally:
                await bot.close()

    asyncio.run(run())


def test_review_notice_survives_restart_and_competing_delivery_without_payload_changes(
    tmp_path,
):
    async def run():
        path = tmp_path / "state.sqlite3"
        store = IntentStore(path)
        await store.initialize()
        exchange = MissingContractExchange()
        exchange.size = Decimal("1")
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, ActionModel(), mode=AutoApprovalMode.ALL
            )
            try:
                with patch.object(
                    bot._bot,
                    "send_message",
                    new=AsyncMock(side_effect=OSError("offline")),
                ):
                    await service.on_message(post())
                with closing(sqlite3.connect(path)) as db:
                    payload = db.execute(
                        "SELECT payload_json FROM position_actions"
                    ).fetchone()[0]
                    assert (
                        db.execute(
                            "SELECT delivered_at FROM manual_deliveries"
                        ).fetchone()[0]
                        is None
                    )
                    db.execute(
                        "UPDATE manual_deliveries SET retry_after=datetime('now','-1 second')"
                    )
                    db.commit()
                restored = SignalDelivery(
                    store=IntentStore(path), executor=executor, approval_bot=bot
                )
                entered, release = asyncio.Event(), asyncio.Event()

                async def send(**kwargs):
                    entered.set()
                    await release.wait()

                with patch.object(
                    bot._bot, "send_message", new=AsyncMock(side_effect=send)
                ) as sender:
                    first = asyncio.create_task(restored.recover_manual())
                    try:
                        await asyncio.wait_for(entered.wait(), 3)
                        await service.delivery.recover_manual()
                    finally:
                        release.set()
                        await first
                    await restored.recover_manual()
                    assert sender.await_count == 1
                    assert "CONTRACT NEEDS REVIEW" in sender.call_args.kwargs["text"]
                    assert not sender.call_args.kwargs.get("reply_markup")
                with closing(sqlite3.connect(path)) as db:
                    assert (
                        db.execute(
                            "SELECT payload_json FROM position_actions"
                        ).fetchone()[0]
                        == payload
                    )
                    assert db.execute(
                        "SELECT delivered_at FROM manual_deliveries"
                    ).fetchone()[0]
                assert (
                    not await store.pending_manual_deliveries() and not exchange.orders
                )
            finally:
                await bot.close()

    asyncio.run(run())
