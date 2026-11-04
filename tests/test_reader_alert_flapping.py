"""Real source/bot lifecycle with controlled transport and monotonic clock."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from test_source_recovery import RecoveringClient, harness
from test_telegram_source import FakeMessage

from cautious_crypto_bro import source_recovery, telegram_source

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("cancel_duplicate", [False, True])
@pytest.mark.parametrize("album", [False, True])
def test_live_and_catchup_same_post_wait_without_blocking_other_posts(
    monkeypatch, cancel_duplicate, album
):
    async def run():
        client = RecoveringClient(authorized=True)
        monkeypatch.setattr(telegram_source, "TelegramClient", lambda *a, **kw: client)
        entered, release, other = asyncio.Event(), asyncio.Event(), asyncio.Event()
        completed, active, calls = set(), set(), []

        async def callback(post):
            key = post.source.message_id
            calls.append(key)
            if key in active:
                return  # The real durable service also declines an active claim.
            active.add(key)
            if key == 1:
                entered.set()
                await release.wait()
            else:
                other.set()
            completed.add(key)

        source = telegram_source.TelegramSource(
            api_id=1,
            api_hash="synthetic",
            session_name="synthetic",
            channels=[-1001234567890],
            on_message=callback,
            source_completed=AsyncMock(
                side_effect=lambda channel, key: key in completed
            ),
            load_history_cursor=AsyncMock(return_value=0),
            save_history_cursor=AsyncMock(),
        )
        first = FakeMessage(
            message_id=1,
            published_at=datetime.now(UTC),
            text="synthetic",
            grouped_id=99 if album else None,
        )
        second = FakeMessage(message_id=3, published_at=datetime.now(UTC), text="other")
        tail = FakeMessage(message_id=2, published_at=first.date, grouped_id=99)
        client.messages = [first, tail] if album else [first]
        live = asyncio.create_task(
            source._handle_messages([tail if album else first], chat=client.entity)
        )
        await asyncio.wait_for(entered.wait(), 3)
        duplicate = asyncio.create_task(
            source._replay_catchup_messages(client.entity, [first], {})
        )
        independent = asyncio.create_task(
            source._handle_messages([second], chat=client.entity)
        )
        try:
            await asyncio.wait_for(other.wait(), 3)
            if cancel_duplicate:
                duplicate.cancel()
                await asyncio.gather(duplicate, return_exceptions=True)
            release.set()
            await asyncio.wait_for(asyncio.gather(live, independent), 3)
            if not cancel_duplicate:
                assert await duplicate is True
                assert source._save_history_cursor.await_args.args[-1] == 1
            assert calls.count(1) == 1 and calls.count(3) == 1
            assert not source.read_warnings()
            # A retained cancelled task may retain its traceback/lock until GC;
            # the adapter holds weak references only, and no lock stays acquired.
            assert all(not lock.locked() for lock in source._post_locks.values())
            if not cancel_duplicate:
                assert not source._post_locks
        finally:
            release.set()
            for task in (live, duplicate, independent):
                task.cancel()
            await asyncio.gather(live, duplicate, independent, return_exceptions=True)

    asyncio.run(run())


def test_failed_warning_retries_and_authorization_bypasses_grace(monkeypatch, tmp_path):
    async def run():
        now = [0.0]
        monkeypatch.setattr(source_recovery, "monotonic", lambda: now[0])
        h = harness(monkeypatch, tmp_path, RecoveringClient(), fail_notice_once=True)
        recovery = source_recovery.SourceRecovery(
            h.source, h.bot, tmp_path / "pin.json", 1
        )
        assert recovery._degraded_grace_seconds == 120
        try:
            recovery.state = "DEGRADED"
            await recovery._alert()
            assert not h.messages
            now[0] = 120
            await recovery._alert()
            assert not recovery._outage and not h.messages
            now[0] = 121
            await recovery._alert()
            assert recovery._outage and len(h.messages) == 1
            await recovery._announce_restored()
            assert len(h.messages) == 2
            await recovery._announce_restored()
            assert len(h.messages) == 2
            recovery.state = "AUTH_REQUIRED"
            recovery._next_alert = 0
            await recovery._alert()
            assert "AUTH_REQUIRED" in h.messages[-1][1]
        finally:
            await h.bot.close()

    asyncio.run(run())


def test_grace_and_reminder_cooldown_survive_flapping(monkeypatch, tmp_path):
    async def run():
        now = [0.0]
        monkeypatch.setattr(source_recovery, "monotonic", lambda: now[0])
        h = harness(
            monkeypatch,
            tmp_path,
            RecoveringClient(authorized=True),
            degraded_grace_seconds=120,
        )
        warnings = ["synthetic channel: RuntimeError"]
        monkeypatch.setattr(h.source, "read_warnings", lambda: tuple(warnings))
        startup = asyncio.Event()
        startup.set()
        h.recovery.state = "ONLINE"
        task = asyncio.create_task(h.recovery._monitor_reads(startup))

        async def tick(time):
            now[0] = time
            await asyncio.sleep(0.04)

        try:
            await tick(0)
            assert h.recovery.state == "DEGRADED" and not h.messages
            warnings.clear()
            await tick(60)
            assert h.recovery.state == "ONLINE" and not h.messages
            warnings.append("another synthetic channel: RuntimeError")
            await tick(100)
            await tick(219)
            assert not h.messages
            await tick(220)
            assert len(h.messages) == 1 and "HUMAN REVIEW REQUIRED" in h.messages[0][1]
            await tick(221)
            assert len(h.messages) == 1
            warnings.clear()
            await tick(230)
            assert len(h.messages) == 2 and "reader restored" in h.messages[-1][1]
            warnings.append("synthetic channel: RuntimeError")
            await tick(240)
            await tick(360)
            assert len(h.messages) == 2
            await tick(1120)
            assert len(h.messages) == 3
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await h.bot.close()

    asyncio.run(run())
