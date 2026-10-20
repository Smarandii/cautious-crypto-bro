"""Post-startup outages through real SQLite, HTTP adapter and bot router."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from aiogram.methods import SendMessage
from test_manual_lifecycle_smoke import TelegramTransport, mutation_count
from test_remaining_concerns import executor_for, prepare
from test_source_recovery import command
from test_supervision_health import stop, until
from test_workflow_integration import ExchangeTransport

from cautious_crypto_bro import approval_bot
from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.domain import ApprovalMode, IntentStatus
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.supervision_health import supervision_health_path

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("failure", "fail_notice"),
    [("account", False), ("account", True), ("mutation", False)],
)
def test_running_supervision_escalates_and_recovers_without_notification_spam(
    monkeypatch, tmp_path, caplog, failure, fail_notice
):
    async def run():
        class OutageExchange(ExchangeTransport):
            unavailable = False
            failures = 0

            def handle(self, request):
                target = (
                    "/v5/position/list"
                    if failure == "account"
                    else "/v5/position/trading-stop"
                )
                if self.unavailable and request.url.path == target:
                    self.failures += 1
                    return httpx.Response(
                        200,
                        json={"retCode": 10010, "retMsg": "synthetic-private-detail"},
                    )
                return super().handle(request)

        class Notices(TelegramTransport):
            outage_calls = 0
            recovery_calls = 0

            async def make_request(self, bot, method, timeout=None):
                if isinstance(method, SendMessage):
                    if "HUMAN REVIEW REQUIRED" in method.text:
                        self.outage_calls += 1
                        if fail_notice and self.outage_calls == 1:
                            raise OSError("Synthetic notification unavailable")
                    elif "supervision recovered" in method.text:
                        self.recovery_calls += 1
                        if fail_notice and self.recovery_calls == 1:
                            raise OSError("Synthetic recovery notification unavailable")
                        if not fail_notice:
                            await release_recovery.wait()
                return await super().make_request(bot, method, timeout)

        clock = 0.0
        release_recovery = asyncio.Event()
        monkeypatch.setattr(approval_bot, "monotonic", lambda: clock)
        exchange = OutageExchange()
        with executor_for(exchange) as executor:
            database = tmp_path / "state.db"
            store, intent, _, coordinator, initial = await prepare(
                database, exchange, executor
            )
            assert (
                await coordinator.execute_intent(
                    intent.intent_id, approval_mode=ApprovalMode.MANUAL
                )
            ).status is IntentStatus.EXECUTED
            await initial.reconcile_once()
            transport = Notices()
            bot = ApprovalBot(
                token="123456:synthetic-offline-token",
                approval_chat_id=1,
                approver_user_id=1,
                store=store,
                executor=coordinator,
            )
            await bot._bot.session.close()
            bot._bot.session = transport
            path = supervision_health_path(database)
            supervisor = PositionSupervisor(
                store=store,
                executor=executor,
                mutation_lock=initial._mutation_lock,
                poll_interval_seconds=0.01,
                failure_retry_seconds=0.05 if failure == "account" else 30,
                progress_path=path,
                report_state=bot.report_supervision_state,
            )
            task = asyncio.create_task(supervisor.run())
            alerts = asyncio.create_task(bot.run_strategy_alert_delivery())
            try:
                await until(path.exists)
                previous_progress = path.read_bytes()
                baseline = mutation_count(exchange)
                request_count = len(exchange.requests)
                if failure == "mutation":
                    exchange.mark = Decimal("106")
                exchange.unavailable = True
                await until(lambda: bot._supervision_warning is not None)
                await command(bot, "/status")
                status = list(transport.messages.values())[-1].text
                assert "Supervision unavailable" in status and "not verified" in status
                assert "synthetic-private-detail" not in status
                count = len(transport.messages)
                await command(bot, "/status", user=2)
                assert len(transport.messages) == count
                assert path.read_bytes() == previous_progress
                await until(lambda: transport.outage_calls >= 1)
                if fail_notice:
                    clock = 31
                    bot._supervision_notice_changed.set()
                    await until(lambda: transport.outage_calls == 2)
                else:
                    await until(lambda: exchange.failures >= 2)
                    assert transport.outage_calls == 1
                clock = 1000
                bot._supervision_notice_changed.set()
                calls = transport.outage_calls
                await until(lambda: transport.outage_calls > calls)
                if failure == "account":
                    await until(
                        lambda: (
                            "Supervision account unavailable; retry in 0.1 seconds"
                            in caplog.text
                        )
                    )
                    assert mutation_count(exchange) == baseline
                    assert (
                        "Supervision account unavailable; retry in 0.05 seconds"
                        in caplog.text
                    )
                    assert (
                        "Supervision account unavailable; retry in 0.1 seconds"
                        in caplog.text
                    )
                else:
                    # Per-symbol mutation errors must retain fast protection passes.
                    assert "Supervision account unavailable" not in caplog.text
                    cancelled = [
                        body
                        for path, body in exchange.requests[request_count:]
                        if path == "/v5/order/cancel"
                    ]
                    assert cancelled
                    for body in cancelled:
                        order = next(
                            item
                            for item in exchange.orders.values()
                            if item["orderId"] == body.get("orderId")
                            or item["orderLinkId"] == body.get("orderLinkId")
                        )
                        assert not order["reduceOnly"], (
                            "Freezing entries must preserve protective exits"
                        )
                exchange.unavailable = False
                clock = 1031
                await until(lambda: bot._supervision_warning is None)
                await until(lambda: transport.recovery_calls >= 1)
                assert path.read_bytes() != previous_progress
                if fail_notice:
                    assert bot._supervision_recovery_pending
                    # Telegram failure must not hold up real protective mutations.
                    exchange.mark = Decimal("106")
                    await until(lambda: exchange.stop == Decimal("100.5"))
                    clock = 1062
                    bot._supervision_notice_changed.set()
                    await until(lambda: transport.recovery_calls == 2)
                else:
                    # A hung Telegram send cannot stop subsequent supervision passes.
                    progress = path.read_bytes()
                    await until(lambda: path.read_bytes() != progress)
                    release_recovery.set()
                await until(lambda: not bot._supervision_recovery_pending)
                await command(bot, "/status")
                assert (
                    "Supervision unavailable"
                    not in list(transport.messages.values())[-1].text
                )
                recovery_calls = transport.recovery_calls
                await bot.report_supervision_state(None)
                assert transport.recovery_calls == recovery_calls
                assert all(
                    "synthetic-private-detail" not in message.text
                    for message in transport.messages.values()
                )
                assert (
                    len(
                        [
                            r
                            for r in exchange.requests
                            if r[0] == "/v5/order/create" and not r[1].get("reduceOnly")
                        ]
                    )
                    == 1
                )
            finally:
                await stop(task)
                await stop(alerts)
                await bot.close()
            # Exercise transition coalescing without waking another SQLite read
            # at the same instant as the fixture shuts down its worker tasks.
            outage_calls = transport.outage_calls
            recovery_calls = transport.recovery_calls
            for _ in range(3):
                await bot.report_supervision_state("Exchange account state unavailable")
                await bot.report_supervision_state(None)
            await bot._deliver_supervision_notice()
            assert transport.outage_calls == outage_calls
            assert transport.recovery_calls == recovery_calls

    asyncio.run(run())
