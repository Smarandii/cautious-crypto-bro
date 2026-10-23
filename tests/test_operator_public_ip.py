"""Actual notice delivery; replace only public-IP and Telegram transports."""

import asyncio
import html
from types import SimpleNamespace

import httpx
import pytest
from test_manual_lifecycle_smoke import TelegramTransport

from cautious_crypto_bro.approval_bot import BYBIT_IP_BINDING_HELP, ApprovalBot
from cautious_crypto_bro.main import _startup_notice

pytestmark = pytest.mark.integration


def install_ip_transport(monkeypatch, outcome="valid"):
    requests = []
    client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        assert str(request.url) == "https://api.ipify.org"
        assert "authorization" not in request.headers
        if outcome == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC_PRIVATE", request=request)
        if outcome == "deadline":
            raise TimeoutError("SYNTHETIC_PRIVATE")
        body = {
            "valid": "8.8.8.8\n",
            "private": "192.168.1.1",
            "garbage": "SYNTHETIC_PRIVATE",
        }.get(outcome, "SYNTHETIC_PRIVATE")
        if outcome == "changing":
            body = "8.8.8.8" if len(requests) == 1 else "1.1.1.1"
        return httpx.Response(503 if outcome == "http_error" else 200, text=body)

    def create(*args, **kwargs):
        assert kwargs["timeout"] == 5
        return client(*args, **kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", create)
    return requests


@pytest.mark.parametrize("path", ["startup", "runtime"])
@pytest.mark.parametrize(
    "outcome", ["valid", "timeout", "deadline", "private", "garbage", "http_error"]
)
def test_outage_notice_contains_current_public_ip_or_explicit_failure(
    monkeypatch, path, outcome, caplog
):
    requests = install_ip_transport(monkeypatch, outcome)

    async def run():
        bot = ApprovalBot(
            token="123456:synthetic-notice-token",
            approval_chat_id=1,
            approver_user_id=1,
            store=None,
            executor=None,
        )
        await bot._bot.session.close()
        transport = TelegramTransport()
        bot._bot.session = transport
        try:
            if path == "startup":
                assert await _startup_notice(
                    SimpleNamespace(bot=bot),
                    "Exchange startup reconciliation unavailable.\n"
                    + BYBIT_IP_BINDING_HELP,
                )
            else:
                await bot.report_supervision_state("Exchange account state unavailable")
                await bot._deliver_supervision_notice()
                await bot._deliver_supervision_notice()
            assert len(requests) == 1 and len(transport.messages) == 1
            text = html.unescape(list(transport.messages.values())[-1].text)
            assert "https://www.bybit.com/en/app/user/api-management" in text
            assert "10010 (Unmatched IP)" in text
            if outcome == "valid":
                assert "Current runtime public IP (at alert time): 8.8.8.8" in text
            else:
                assert "Current runtime public IP unavailable: lookup failed" in text
                assert "192.168.1.1" not in text
            assert "SYNTHETIC_PRIVATE" not in text + caplog.text
            await bot.send_source_notice("Exchange reconciliation recovered.")
            assert len(requests) == 1
            assert "public IP" not in list(transport.messages.values())[-1].text
        finally:
            await bot.close()

    asyncio.run(run())


def test_later_outage_alert_refreshes_changed_ip(monkeypatch):
    requests = install_ip_transport(monkeypatch, "changing")
    clock = [0]
    monkeypatch.setattr("cautious_crypto_bro.approval_bot.monotonic", lambda: clock[0])

    async def run():
        bot = ApprovalBot(
            token="123456:synthetic-notice-token",
            approval_chat_id=1,
            approver_user_id=1,
            store=None,
            executor=None,
        )
        await bot._bot.session.close()
        transport = TelegramTransport()
        bot._bot.session = transport
        try:
            await bot.report_supervision_state("Exchange account state unavailable")
            await bot._deliver_supervision_notice()
            clock[0] = 901
            await bot._deliver_supervision_notice()
            messages = list(transport.messages.values())
            assert len(requests) == len(messages) == 2
            assert "at alert time): 8.8.8.8" in messages[0].text
            assert "at alert time): 1.1.1.1" in messages[1].text
            assert "8.8.8.8" not in messages[1].text
        finally:
            await bot.close()

    asyncio.run(run())
