"""Exercise the real bot router, source adapter and recovery lifecycle offline."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, Update
from aiogram.types import User as BotUser
from telethon.errors import AuthKeyUnregisteredError, SessionPasswordNeededError
from telethon.tl.types import User
from test_telegram_source import FakeMessage, FakeTelegramClient

from cautious_crypto_bro import source_recovery, telegram_source
from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.source_recovery import SourceRecovery

pytestmark = pytest.mark.integration


class RecoveringClient(FakeTelegramClient):
    def __init__(self, *, authorized=False, outcome="success"):
        super().__init__([])
        self.user = User(id=1, first_name="Test") if authorized else None
        self.outcome = outcome
        self.starts = 0
        self.qrs = 0
        self.history_calls = 0
        self.fail_history_after = None
        self.history_error_auth = True
        self.fail_retry = False
        self.connections = asyncio.Queue()
        self.listening = asyncio.Event()
        self.revoked = asyncio.Event()

    async def connect(self):
        self.revoked.clear()
        self.connections.put_nowait(True)

    async def get_me(self):
        return self.user

    async def start(self, *, phone):
        if self.user is None:
            phone()  # The production callback must refuse an interactive prompt.
        self.starts += 1

    async def disconnect(self):
        pass

    def iter_messages(self, entity, **kwargs):
        self.history_calls += 1
        if (
            self.fail_history_after is not None
            and self.history_calls > self.fail_history_after
        ):

            async def failed():
                if not self.history_error_auth:
                    raise OSError("Synthetic history unavailable")
                self.user = None
                raise AuthKeyUnregisteredError(request=None)
                yield

            return failed()
        return super().iter_messages(entity, **kwargs)

    async def get_messages(self, entity, *, ids):
        if self.fail_retry:
            self.user = None
            raise AuthKeyUnregisteredError(request=None)
        return await super().get_messages(entity, ids=ids)

    async def run_until_disconnected(self):
        self.listening.set()
        await self.revoked.wait()
        raise AuthKeyUnregisteredError(request=None)

    async def qr_login(self):
        self.qrs += 1
        ready, accepted = asyncio.Event(), asyncio.Event()

        async def wait():
            ready.set()
            await accepted.wait()
            if self.outcome == "2fa":
                raise SessionPasswordNeededError(request=None)
            if self.outcome == "expired":
                raise TimeoutError()
            self.user = User(
                id=2 if self.outcome == "wrong-account" else 1, first_name="Test"
            )
            return self.user

        self.qr = SimpleNamespace(
            url="tg://login?token=synthetic-offline-token",
            wait=wait,
            ready=ready,
            accepted=accepted,
        )
        return self.qr


def harness(monkeypatch, tmp_path, client, *, fail_notice_once=False, retry_ids=None):
    monkeypatch.setattr(telegram_source, "TelegramClient", lambda *a, **kw: client)
    source = telegram_source.TelegramSource(
        api_id=1,
        api_hash="fake",
        session_name="fake",
        channels=[-1001234567890],
        on_message=AsyncMock(),
        retry_message_ids=retry_ids,
    )
    bot = ApprovalBot(
        token="123456:synthetic-offline-token",
        approval_chat_id=-100900,
        approver_user_id=1,
        store=object(),
        executor=object(),
    )
    messages, photos, deleted = [], [], []
    notices = asyncio.Queue()
    calls = 0

    async def send(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 and fail_notice_once:
            raise OSError("synthetic notification failure")
        chat = kwargs.get("chat_id", args[0] if args else None)
        text = kwargs.get("text", args[1] if len(args) > 1 else "")
        messages.append((chat, text))
        notices.put_nowait(text)
        return SimpleNamespace(message_id=len(messages))

    async def photo(chat, image, **kwargs):
        assert chat == 1 and kwargs["protect_content"]
        assert image.data.startswith(b"\x89PNG\r\n\x1a\n")
        assert client.qr.ready.is_set(), "QR waiter started after token display"
        photos.append(image)
        client.qr.accepted.set()
        return SimpleNamespace(message_id=100 + len(photos))

    async def delete(chat, message_id):
        assert chat == 1
        deleted.append(message_id)

    monkeypatch.setattr(bot._bot, "send_message", send)
    monkeypatch.setattr(bot._bot, "send_photo", photo)
    monkeypatch.setattr(bot._bot, "delete_message", delete)
    recovery = SourceRecovery(
        source,
        bot,
        tmp_path / "identity.json",
        1,
        retry_seconds=3600,
        auth_retry_seconds=3600,
        read_check_seconds=0.01,
    )
    bot.bind_source_recovery(recovery)
    return SimpleNamespace(
        source=source,
        bot=bot,
        recovery=recovery,
        notices=notices,
        messages=messages,
        photos=photos,
        deleted=deleted,
    )


async def command(bot, text, *, user=1, chat=1):
    await bot._dispatcher.feed_update(
        bot._bot,
        Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=chat, type="private" if chat > 0 else "group"),
                from_user=BotUser(id=user, is_bot=False, first_name="Test"),
                text=text,
            ),
        ),
    )


async def notice(h, containing):
    async with asyncio.timeout(3):
        while True:
            text = await h.notices.get()
            if containing in text:
                return text


@pytest.mark.parametrize("path", ["catchup", "retry", "retry-fetch"])
def test_media_processing_outage_reaches_operator_and_recovers(
    monkeypatch, tmp_path, path
):
    class MediaMessage(FakeMessage):
        fail_download = True

        async def download_media(self, *, file):
            if self.fail_download:
                raise OSError("synthetic media failure")
            return await super().download_media(file=file)

    async def run():
        client = RecoveringClient(authorized=True)
        retry_ids = AsyncMock(return_value=()) if path != "catchup" else None
        h = harness(monkeypatch, tmp_path, client, retry_ids=retry_ids)
        task = asyncio.create_task(h.recovery.run())
        try:
            await asyncio.wait_for(client.listening.wait(), 3)
            message = MediaMessage(
                message_id=1,
                published_at=datetime.now(UTC),
                text="Synthetic image signal",
                image=b"synthetic-image",
            )
            client.messages.append(message)
            original_get_messages = client.get_messages
            if path == "retry-fetch":
                message.fail_download = False
                monkeypatch.setattr(
                    client,
                    "get_messages",
                    AsyncMock(side_effect=OSError("synthetic fetch failure")),
                )
            if retry_ids is not None:
                retry_ids.return_value = (1,)
                h.source._watermarks[message.chat_id] = 1
            previous = h.source._watermarks[message.chat_id]
            await h.source.poll_once()
            assert any("OSError" in warning for warning in h.source.read_warnings())
            await notice(h, "HUMAN REVIEW REQUIRED")
            assert h.recovery.state == "DEGRADED"
            assert h.source._watermarks[message.chat_id] == previous
            assert h.source._on_message.await_count == 0
            message.fail_download = False
            if path == "retry-fetch":
                monkeypatch.setattr(client, "get_messages", original_get_messages)
            await h.source.poll_once()
            await notice(h, "reader restored")
            assert h.recovery.state == "ONLINE" and not h.source.read_warnings()
            assert h.source._on_message.await_count == 1
            assert client.starts == 1 and client.qrs == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await h.bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["seed", "history", "retry"])
def test_authorization_failures_in_history_paths_reach_operator(
    monkeypatch, tmp_path, phase
):
    async def run():
        client = RecoveringClient(authorized=True)
        if phase == "retry":
            client.fail_retry = True
        else:
            client.fail_history_after = 0 if phase == "seed" else 1
        h = harness(
            monkeypatch, tmp_path, client, retry_ids=AsyncMock(return_value=(1,))
        )
        task = asyncio.create_task(h.recovery.run())
        try:
            await notice(h, "HUMAN REVIEW REQUIRED")
            assert h.recovery.state == "AUTH_REQUIRED" and not task.done()
            await command(h.bot, "/source")
            assert "AUTH_REQUIRED" in h.messages[-1][1]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await h.bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("fail_recovery_once", [False, True])
def test_channel_read_outage_escalates_without_disconnecting_valid_session(
    monkeypatch, tmp_path, fail_recovery_once
):
    async def run():
        now = [0.0]
        monkeypatch.setattr(source_recovery, "monotonic", lambda: now[0])
        client = RecoveringClient(authorized=True)
        client.fail_history_after = 0
        client.history_error_auth = False
        h = harness(monkeypatch, tmp_path, client)
        original_send = h.bot._bot.send_message
        recovery_attempts = []
        failed = asyncio.Event()

        async def send(*args, **kwargs):
            text = kwargs.get("text", args[1] if len(args) > 1 else "")
            if "reader restored" in text:
                recovery_attempts.append(now[0])
                if fail_recovery_once and len(recovery_attempts) == 1:
                    failed.set()
                    raise OSError("synthetic recovery notification failure")
            return await original_send(*args, **kwargs)

        monkeypatch.setattr(h.bot._bot, "send_message", send)
        task = asyncio.create_task(h.recovery.run())
        try:
            await notice(h, "HUMAN REVIEW REQUIRED")
            assert h.recovery.state == "DEGRADED" and not task.done()
            assert "OSError" in h.recovery.status()
            await command(h.bot, "/source_login")
            assert client.qrs == 0 and "already authorized" in h.messages[-1][1]
            client.fail_history_after = None
            await h.source.poll_once()
            if fail_recovery_once:
                await asyncio.wait_for(failed.wait(), 3)
                await asyncio.sleep(0.05)
                assert recovery_attempts == [0.0]
                assert h.recovery.state == "ONLINE" and not task.done()
                now[0] = h.recovery._retry_seconds
            await notice(h, "reader restored")
            assert h.recovery.state == "ONLINE" and client.starts == 1
            assert len(client.handlers) == 2
            await asyncio.sleep(0.05)
            assert len(recovery_attempts) == (2 if fail_recovery_once else 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await h.bot.close()

    asyncio.run(run())


def test_revoked_reader_session_recovers_through_private_bot_router(
    monkeypatch, tmp_path, caplog
):
    async def run():
        client = RecoveringClient(authorized=True)
        h = harness(monkeypatch, tmp_path, client)
        task = asyncio.create_task(h.recovery.run())
        try:
            await asyncio.wait_for(client.listening.wait(), 3)
            assert h.recovery.state == "ONLINE" and len(client.handlers) == 2
            cutoff = h.source._history_cutoff
            client.listening.clear()
            client.user = None
            client.revoked.set()
            await notice(h, "HUMAN REVIEW REQUIRED")
            assert not task.done() and h.recovery.state == "AUTH_REQUIRED"
            await command(h.bot, "/source")
            assert h.messages[-1][0] == 1 and "AUTH_REQUIRED" in h.messages[-1][1]
            await command(h.bot, "/source_login")
            await asyncio.wait_for(client.listening.wait(), 3)
            assert h.recovery.state == "ONLINE" and client.starts == 2
            assert len(client.handlers) == 2 and h.source._history_cutoff == cutoff
            assert len(h.photos) == len(h.deleted) == 1
            assert json.loads((tmp_path / "identity.json").read_text()) == {
                "user_id": 1
            }
            assert "synthetic-offline-token" not in " ".join(t for _, t in h.messages)
            assert "synthetic-offline-token" not in caplog.text
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await h.bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["2fa", "wrong-account", "expired"])
def test_incomplete_qr_login_escalates_without_using_another_account(
    monkeypatch, tmp_path, outcome
):
    async def run():
        client = RecoveringClient(outcome=outcome)
        h = harness(monkeypatch, tmp_path, client)
        task = asyncio.create_task(h.recovery.run())
        try:
            await notice(h, "HUMAN REVIEW REQUIRED")
            await command(h.bot, "/source_login")
            await notice(
                h,
                "2FA password"
                if outcome == "2fa"
                else "Authorization did not complete",
            )
            assert h.recovery.state == "AUTH_REQUIRED" and client.starts == 0
            assert len(h.photos) == len(h.deleted) == (3 if outcome == "expired" else 1)
            assert json.loads((tmp_path / "identity.json").read_text()) == {
                "user_id": 1
            }
            before = client.qrs
            await command(h.bot, "/source_login")
            assert client.qrs == before
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await h.bot.close()

    asyncio.run(run())


def test_source_commands_enforce_operator_and_private_chat(monkeypatch, tmp_path):
    async def run():
        client = RecoveringClient()
        h = harness(monkeypatch, tmp_path, client)
        try:
            await command(h.bot, "/source_login", user=2, chat=2)
            await command(h.bot, "/source", user=2, chat=-100900)
            assert h.messages == [] and client.qrs == 0
            await command(h.bot, "/source_login", chat=-100900)
            assert "private chat" in h.messages[-1][1] and client.qrs == 0
            await command(h.bot, "/source_login forbidden-argument")
            assert "without arguments" in h.messages[-1][1] and client.qrs == 0
        finally:
            await h.bot.close()

    asyncio.run(run())


@pytest.mark.parametrize("fail_notice_once", [False, True])
def test_outage_alerts_retry_delivery_and_throttle_successful_escalations(
    monkeypatch, tmp_path, fail_notice_once
):
    async def run():
        now = [0.0]
        monkeypatch.setattr(source_recovery, "monotonic", lambda: now[0])
        client = RecoveringClient()
        h = harness(monkeypatch, tmp_path, client, fail_notice_once=fail_notice_once)
        task = asyncio.create_task(h.recovery.run())
        try:
            await asyncio.wait_for(client.connections.get(), 3)
            if fail_notice_once:
                h.recovery._wake.set()
            await notice(h, "HUMAN REVIEW REQUIRED")
            now[0] = 300
            h.recovery._wake.set()
            await asyncio.wait_for(client.connections.get(), 3)
            assert len(h.messages) == 1
            now[0] = 900
            h.recovery._wake.set()
            await notice(h, "HUMAN REVIEW REQUIRED")
            assert len(h.messages) == 2 and not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await h.bot.close()

    asyncio.run(run())
