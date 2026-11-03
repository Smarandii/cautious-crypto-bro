"""Filled-entry ambiguity stays non-executable at the source/store/bot boundary."""

import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

import pytest
from test_remaining_concerns import executor_for, make_intent
from test_workflow_integration import ExchangeTransport, workflow

from cautious_crypto_bro.domain import AutoApprovalMode, ImageAttachment, IncomingPost
from cautious_crypto_bro.llm_provider import LLMResponse
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class FilledEntryModel:
    def __init__(self, intents):
        self.intents = intents
        self.requests = 0

    async def complete(self, request):
        self.requests += 1
        return LLMResponse(
            json.dumps(
                {
                    "actionable": bool(self.intents),
                    "reason": "Synthetic filled-entry evidence",
                    "intents": self.intents,
                }
            )
        )


def candidate(symbol="BTCUSDT", *, side=None, direction=None):
    return {
        "symbol": symbol,
        "side": side,
        "direction": direction,
        "entry": "MARKET",
        "relation": "NEW",
        "summary": "Synthetic current entry declaration",
        "confidence": 1,
    }


def run_source(tmp_path, caption, intents, *, images=()):
    async def run():
        database = tmp_path / "state.sqlite3"
        store = IntentStore(database)
        await store.initialize()
        source = make_intent().source.model_copy(update={"text": caption})
        model = FilledEntryModel(intents)
        exchange = ExchangeTransport()
        sender = AsyncMock()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, model, mode=AutoApprovalMode.DISABLED
            )
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    post = IncomingPost(source=source, images=images)
                    await service.on_message(post)
                    sent = sender.await_count
                    await service.on_message(post)
                    assert sender.await_count == sent
            finally:
                await bot.close()
        assert model.requests == 1
        assert not exchange.orders
        with closing(sqlite3.connect(database)) as db:
            decision = json.loads(
                db.execute(
                    "SELECT payload_json FROM source_decisions ORDER BY created_at DESC LIMIT 1"
                ).fetchone()[0]
            )
            saved = db.execute("SELECT payload_json,status FROM intents").fetchall()
            assert db.execute("SELECT status FROM source_messages").fetchall() == [
                ("COMPLETED",)
            ]
            assert (
                db.execute("SELECT count(*) FROM position_actions").fetchone()[0] == 0
            )
        return decision, saved, sender

    return asyncio.run(run())


@pytest.mark.parametrize(
    "caption",
    [
        "My BTCUSDT limit entry just filled.",
        "Лимитный ордер на вход по BTCUSDT только что исполнился.",
    ],
)
@pytest.mark.parametrize("omitted", [True, False])
def test_omitted_or_incomplete_filled_entry_has_no_plan_order_or_approval(
    tmp_path, caption, omitted
):
    decision, saved, sender = run_source(
        tmp_path, caption, [] if omitted else [candidate()]
    )
    assert decision["normalized"]["open_intents"] == []
    assert not saved and sender.await_count == 0
    assert len(decision["extraction"]["intents"]) == int(not omitted)


def test_missing_direction_does_not_discard_unrelated_valid_current_entry(tmp_path):
    decision, saved, _ = run_source(
        tmp_path,
        "My ETHUSDT limit entry just filled. I just opened BTCUSDT LONG at market.",
        [candidate("ETHUSDT"), candidate(side="LONG")],
    )
    assert len(decision["extraction"]["intents"]) == 2
    assert [row["symbol"] for row in decision["normalized"]["open_intents"]] == [
        "BTCUSDT"
    ]
    assert len(saved) == 1 and saved[0][1] == "PENDING"


def test_current_panel_can_supply_its_own_direction_without_caption_side(tmp_path):
    decision, saved, _ = run_source(
        tmp_path,
        "My BTCUSDT limit entry just filled.",
        [candidate(side="LONG")],
        images=(
            ImageAttachment(media_type="image/png", data=b"synthetic-current-panel"),
        ),
    )
    assert decision["normalized"]["open_intents"][0]["side"] == "LONG"
    assert len(saved) == 1 and saved[0][1] == "PENDING"


def test_explicit_short_caption_keeps_supported_direction_alias(tmp_path):
    decision, saved, _ = run_source(
        tmp_path,
        "My BTCUSDT SHORT limit entry just filled.",
        [candidate(direction="SHORT")],
    )
    assert decision["normalized"]["open_intents"][0]["side"] == "SHORT"
    assert len(saved) == 1 and saved[0][1] == "PENDING"
