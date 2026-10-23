"""Accounting stays independent of trading; only external transports are fake."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from test_remaining_concerns import executor_for, source
from test_workflow_integration import ExchangeTransport, ModelTransport, workflow

from cautious_crypto_bro.domain import (
    AutoApprovalMode,
    ImageAttachment,
    IncomingPost,
    TradeExecutionError,
)
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "bad_pnl", ["missing", None, "", " ", "invalid-private-value", "NaN", "Infinity"]
)
def test_incomplete_history_preserves_ledger_and_sync_watermark(tmp_path, bad_pnl):
    class HistoryExchange(ExchangeTransport):
        rows = []

        def handle(self, request):
            if request.url.path == "/v5/position/closed-pnl":
                return httpx.Response(
                    200,
                    json={
                        "retCode": 0,
                        "result": {"list": self.rows, "nextPageCursor": ""},
                    },
                )
            return super().handle(request)

    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        exchange = HistoryExchange()
        row = {
            "symbol": "BTCUSDT",
            "orderId": "close-one",
            "side": "Sell",
            "closedPnl": "12.5",
            "closedSize": "1",
            "updatedTime": str(int(datetime.now(UTC).timestamp() * 1000)),
        }
        exchange.rows = [row]
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, model())
            try:
                original = await service._sync_account_pnl()
                watermark = await store.get_account_pnl_sync_state()
                # A valid new row must not leak into storage before a bad row fails.
                broken = dict(row)
                if bad_pnl == "missing":
                    del broken["closedPnl"]
                else:
                    broken["closedPnl"] = bad_pnl
                exchange.rows = [dict(row, orderId="close-two"), broken]
                with pytest.raises(TradeExecutionError) as error:
                    await service._sync_account_pnl()
                assert "invalid-private-value" not in str(error.value)
                restarted = IntentStore(database)
                assert await restarted.get_account_pnl_summary() == original
                assert await restarted.get_account_pnl_sync_state() == watermark
                exchange.rows = [dict(row, closedPnl="0")]
                recovered = await service._sync_account_pnl()
                assert recovered.realized_pnl == 0
                assert recovered.record_count == 1
            finally:
                await bot.close()

    asyncio.run(run())


class AccountingExchange(ExchangeTransport):
    def __init__(self):
        super().__init__()
        self.history_calls = 0
        self.history_started = Event()
        self.history_release = Event()
        self.history_release.set()

    def handle(self, request):
        if request.url.path == "/v5/position/closed-pnl":
            self.history_calls += 1
            self.history_started.set()
            assert self.history_release.wait(20), "Test history gate was never released"
        return super().handle(request)


def signal(kind):
    if kind == "close":
        return IncomingPost(source=source("Close BTCUSDT now"))
    return IncomingPost(
        source=source("Enter BTCUSDT LONG at market, stop 90"),
        images=(ImageAttachment("image/png", b"synthetic chart"),),
    )


def model():
    provider = ModelTransport()
    provider.fail_image_once = False
    return provider


@pytest.mark.parametrize("kind", ["open", "close", "manual"])
def test_signal_and_result_preview_do_not_fetch_accounting_history(tmp_path, kind):
    async def run():
        store = IntentStore(tmp_path / "state.db")
        await store.initialize()
        exchange = AccountingExchange()
        if kind == "close":
            exchange.size, exchange.stop = Decimal("1"), Decimal("90")
        with executor_for(exchange) as executor:
            mode = (
                AutoApprovalMode.DISABLED if kind == "manual" else AutoApprovalMode.ALL
            )
            service, bot, _ = workflow(store, executor, model(), mode=mode)
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    await service.on_message(signal(kind))
                assert exchange.history_calls == 0, (
                    f"{kind} performed {exchange.history_calls} history requests on the signal path"
                )
                assert sender.await_count >= 2
                if kind == "manual":
                    assert exchange.size == 0 and not exchange.orders
                    assert await store.pending_manual_deliveries() == ()
                else:
                    assert any(
                        "AUTO-EXECUTED" in c.kwargs["text"]
                        for c in sender.call_args_list
                    )
                    assert exchange.size > 0 if kind == "open" else exchange.size == 0
            finally:
                await bot.close()

    asyncio.run(run())


def test_blocked_background_history_does_not_block_auto_close(tmp_path):
    async def run():
        store = IntentStore(tmp_path / "state.db")
        await store.initialize()
        exchange = AccountingExchange()
        exchange.size, exchange.stop = Decimal("1"), Decimal("90")
        exchange.history_release.clear()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(
                store, executor, model(), mode=AutoApprovalMode.ALL
            )
            with patch.object(bot._bot, "send_message", new=AsyncMock()):
                background = asyncio.create_task(
                    service.run_periodic_account_pnl_sync()
                )
                message = None
                try:
                    assert await asyncio.to_thread(exchange.history_started.wait, 5)
                    message = asyncio.create_task(service.on_message(signal("close")))
                    done, _ = await asyncio.wait({message}, timeout=5)
                    assert message in done, "AUTO close waited for accounting history"
                    await message
                    assert exchange.size == 0
                    assert not background.done()
                    assert exchange.history_calls == 1
                    exchange.history_release.set()
                    async with asyncio.timeout(5):
                        while await store.get_account_pnl_sync_state() is None:
                            await asyncio.sleep(0.01)
                    assert await store.get_account_pnl_summary() is not None
                    assert exchange.history_calls == 1
                finally:
                    exchange.history_release.set()
                    if message is not None:
                        await asyncio.gather(message, return_exceptions=True)
                    background.cancel()
                    await asyncio.gather(background, return_exceptions=True)
                    await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", [AutoApprovalMode.DISABLED, AutoApprovalMode.ALL])
def test_previews_show_stored_sync_time_and_survive_accounting_storage_failure(
    tmp_path, mode
):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        synced = datetime.now(UTC) - timedelta(days=2)
        await store.mark_account_pnl_synced(
            history_start_at=synced - timedelta(days=7), last_synced_at=synced
        )
        exchange = AccountingExchange()
        with executor_for(exchange) as executor:
            service, bot, _ = workflow(store, executor, model(), mode=mode)
            sender = AsyncMock()
            try:
                with patch.object(bot._bot, "send_message", new=sender):
                    await service.on_message(signal("manual"))
                    assert (
                        synced.strftime("%Y-%m-%d %H:%M:%S UTC")
                        in sender.call_args_list[0].kwargs["text"]
                    )
                    with sqlite3.connect(database) as db:
                        db.execute("DROP TABLE account_closed_pnl")
                    sender.reset_mock()
                    second = IncomingPost(
                        source=source("Close BTCUSDT now", message_id=2)
                    )
                    exchange.size, exchange.stop = Decimal("1"), Decimal("90")
                    await service.on_message(second)
                assert sender.await_count >= 2
                assert "unavailable" in sender.call_args_list[0].kwargs["text"]
                assert exchange.history_calls == 0
                if mode is AutoApprovalMode.ALL:
                    assert exchange.size == 0
            finally:
                await bot.close()

    asyncio.run(run())
