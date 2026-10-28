import asyncio
import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_telegram_source import FakeMessage, FakeTelegramClient

from cautious_crypto_bro.domain import SourceMessage
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.telegram_coverage import telegram_history_coverage

pytestmark = pytest.mark.integration


def test_visible_supported_post_missing_from_ledger_is_reported(tmp_path):
    async def run():
        from cautious_crypto_bro.telegram_coverage import telegram_history_coverage

        database = tmp_path / "state.db"
        await IntentStore(database).initialize()
        now = datetime.now(UTC)
        client = FakeTelegramClient(
            [FakeMessage(message_id=2, published_at=now, text="signal")]
        )
        report = await telegram_history_coverage(
            database,
            client,
            [client.entity.id],
            since=now - timedelta(hours=1),
            until=now + timedelta(seconds=1),
        )
        assert report["history_complete"]
        assert not report["intake_coverage_complete"]
        assert report["channels"][0]["missing_source_ids"] == [2]
        with closing(sqlite3.connect(database)) as db, db:
            assert db.execute("SELECT COUNT(*) FROM source_messages").fetchone() == (0,)

    asyncio.run(run())


async def seed(
    database,
    message,
    *,
    status="COMPLETED",
    decision=True,
    reason=None,
    old_attempt=False,
):
    store = IntentStore(database)
    await store.initialize()
    source = SourceMessage(
        channel_id=message.chat_id,
        channel_title="Private channel",
        message_id=message.id,
        published_at=message.date,
        received_at=datetime.now(UTC),
        text=message.message or "image",
    )
    token = await store.claim_source(source, lease_seconds=300)
    if decision:
        await store.record_source_decision(source, token, "{}")
    if old_attempt:
        await store.mark_source_failed(source, token, "retry")
        token = await store.claim_source(source, lease_seconds=300)
    if status == "COMPLETED":
        await store.mark_source_completed(source, token, reason=reason)
    elif status != "PROCESSING":
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE source_messages SET status=?", (status,))


@pytest.mark.parametrize(
    "status,decision,reason,old_attempt,complete",
    [
        ("COMPLETED", True, None, False, True),
        ("COMPLETED", False, "Image budget exceeded", False, True),
        ("COMPLETED", False, None, False, False),
        ("COMPLETED", True, None, True, False),
        ("FAILED", True, None, False, False),
        ("PROCESSING", False, None, False, False),
        ("UNAVAILABLE", False, None, False, False),
    ],
)
def test_current_attempt_and_terminal_disposition_are_compared_read_only(
    tmp_path, status, decision, reason, old_attempt, complete
):
    async def run():
        database = tmp_path / "state.db"
        now = datetime.now(UTC)
        message = FakeMessage(message_id=2, published_at=now, text="private source")
        await seed(
            database,
            message,
            status=status,
            decision=decision,
            reason=reason,
            old_attempt=old_attempt,
        )
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        client = FakeTelegramClient([message])
        result = await telegram_history_coverage(
            database,
            client,
            [1],
            since=now - timedelta(hours=1),
            until=now + timedelta(seconds=1),
        )
        assert result["history_complete"]
        assert result["intake_coverage_complete"] is complete
        row = result["channels"][0]
        assert row["source_statuses"] == {status: 1}
        if old_attempt or (status == "COMPLETED" and not decision and not reason):
            assert row["completed_without_decision_ids"] == [2]
        assert "private source" not in json.dumps(result)
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before

    asyncio.run(run())


def test_boundary_album_media_metadata_and_settling_margin_without_downloads(
    tmp_path, monkeypatch
):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Metadata audit must not download media")

    monkeypatch.setattr(FakeMessage, "download_media", forbidden)

    async def run():
        database = tmp_path / "state.db"
        now = datetime.now(UTC)
        since = now - timedelta(hours=1)
        messages = [
            FakeMessage(
                message_id=1, published_at=since - timedelta(days=1), text="old"
            ),
            FakeMessage(
                message_id=2,
                published_at=since - timedelta(seconds=1),
                grouped_id=9,
                image=b"head",
            ),
            FakeMessage(
                message_id=3,
                published_at=since + timedelta(seconds=1),
                grouped_id=9,
                image=b"tail",
            ),
            FakeMessage(message_id=4, published_at=since + timedelta(seconds=2)),
            FakeMessage(
                message_id=5, published_at=since + timedelta(seconds=3), image=b"only"
            ),
            FakeMessage(
                message_id=6,
                published_at=now - timedelta(seconds=1),
                text="settling",
                grouped_id=10,
            ),
            FakeMessage(
                message_id=7,
                published_at=now + timedelta(seconds=1),
                image=b"unsettled",
                grouped_id=10,
            ),
        ]
        for message in (messages[1], messages[4]):
            await seed(database, message)
        result = await telegram_history_coverage(
            database, FakeTelegramClient(messages), [1], since=since, until=now
        )
        assert result["intake_coverage_complete"]
        row = result["channels"][0]
        assert row["supported_posts"] == 2 and row["unsupported_groups"] == 1
        assert row["completed_with_decision"] == 2
        assert not row["missing_source_ids"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "cap,old_boundary,history_complete,omitted,supported",
    [
        (1, False, False, True, 0),
        (3, False, False, False, 1),
        (4, False, True, False, 2),
        (3, True, True, False, 1),
    ],
)
def test_cap_and_partial_album_never_claim_complete_history(
    tmp_path, cap, old_boundary, history_complete, omitted, supported
):
    async def run():
        database = tmp_path / "state.db"
        now = datetime.now(UTC)
        since = now - timedelta(hours=1)
        messages = [
            FakeMessage(
                message_id=n,
                published_at=now,
                text="supported",
                grouped_id=9 if n > 1 else None,
            )
            for n in range(1, 5)
        ]
        if old_boundary:
            messages[0].date = since - timedelta(seconds=1)
        for message in messages[:2]:
            await seed(database, message)
        result = await telegram_history_coverage(
            database,
            FakeTelegramClient(messages),
            [1],
            since=since,
            until=now,
            max_messages=cap,
        )
        assert result["history_complete"] is history_complete
        assert result["intake_coverage_complete"] is history_complete
        row = result["channels"][0]
        assert row["omitted_partial_album"] is omitted
        assert row["supported_posts"] == supported
        assert not row["missing_source_ids"]

    asyncio.run(run())


def test_channel_failure_does_not_hide_healthy_channel_or_disclose_error_text(tmp_path):
    class Client(FakeTelegramClient):
        async def get_entity(self, channel):
            if channel == "unavailable":
                raise OSError("private channel/session details")
            return self.entity

    async def run():
        database = tmp_path / "state.db"
        now = datetime.now(UTC)
        message = FakeMessage(message_id=2, published_at=now, text="signal")
        await seed(database, message)
        result = await telegram_history_coverage(
            database,
            Client([message]),
            ["unavailable", "healthy"],
            since=now - timedelta(hours=1),
            until=now,
        )
        assert not result["history_complete"] and not result["intake_coverage_complete"]
        assert result["channels"][0]["error"] == "OSError"
        assert result["channels"][1]["intake_coverage_complete"]
        assert "private channel" not in json.dumps(result)

    asyncio.run(run())


def load_cli():
    path = Path(__file__).parents[1] / "scripts" / "report_source_coverage.py"
    spec = importlib.util.spec_from_file_location("coverage_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_missing_decision_table_stays_unknown_without_migration(tmp_path):
    async def run():
        database = tmp_path / "legacy.db"
        now = datetime.now(UTC)
        message = FakeMessage(message_id=2, published_at=now, text="signal")
        await seed(database, message)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("DROP TABLE source_decisions")
            db.execute("PRAGMA user_version=10")
        before = database.read_bytes()
        result = await telegram_history_coverage(
            database,
            FakeTelegramClient([message]),
            [1],
            since=now - timedelta(hours=1),
            until=now,
        )
        assert result["history_complete"] and not result["intake_coverage_complete"]
        assert result["channels"][0]["completed_without_decision_ids"] == [2]
        assert database.read_bytes() == before

    asyncio.run(run())


def test_default_cli_remains_offline(tmp_path, monkeypatch, capsys):
    database = tmp_path / "state.db"
    asyncio.run(IntentStore(database).initialize())
    import cautious_crypto_bro.config as config

    def forbidden():
        raise AssertionError("Offline report loaded Telegram credentials")

    monkeypatch.setattr(config, "get_settings", forbidden)
    module = load_cli()
    monkeypatch.setattr("sys.argv", ["report", "--database", str(database)])
    module.main()
    assert json.loads(capsys.readouterr().out)["sources"] == 0


@pytest.mark.parametrize(
    "authorized,missing", [(True, False), (True, True), (False, False)]
)
def test_opt_in_cli_clones_session_cleans_up_and_never_initializes_production(
    tmp_path, monkeypatch, capsys, authorized, missing
):
    import telethon

    import cautious_crypto_bro.config as config

    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    database = tmp_path / "state.db"
    asyncio.run(IntentStore(database).initialize())
    session = tmp_path / "original.session"
    with closing(sqlite3.connect(session)) as db, db:
        db.execute("CREATE TABLE marker(value TEXT)")
        db.execute("INSERT INTO marker VALUES ('original')")
    original = session.read_bytes()
    clients = []

    class Client(FakeTelegramClient):
        def __init__(self, cloned, api_id, api_hash, **kwargs):
            super().__init__(
                [
                    FakeMessage(
                        message_id=2,
                        published_at=datetime.now(UTC) - timedelta(minutes=3),
                        text="missing",
                    )
                ]
                if missing
                else []
            )
            assert kwargs == {"receive_updates": False}
            assert api_id == 1 and api_hash == "test"
            self.session = Path(cloned + ".session")
            assert self.session != session and self.session.exists()
            self.disconnected = False
            clients.append(self)

        async def connect(self):
            with closing(sqlite3.connect(self.session)) as db, db:
                db.execute("UPDATE marker SET value='clone-only'")

        async def is_user_authorized(self):
            return authorized

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(telethon, "TelegramClient", Client)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_session_name=str(session),
            telegram_api_id=1,
            telegram_api_hash="test",
            telegram_source_channels=[1],
        ),
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("Audit initialized production store")

    monkeypatch.setattr(IntentStore, "initialize", forbidden)
    monkeypatch.setattr(
        "sys.argv", ["report", "--database", str(database), "--telegram-history"]
    )
    module = load_cli()
    if authorized and not missing:
        module.main()
        assert json.loads(capsys.readouterr().out)["intake_coverage_complete"]
    else:
        with pytest.raises(SystemExit) as error:
            module.main()
        assert error.value.code == (2 if authorized else 1)
        captured = capsys.readouterr()
        if authorized:
            report = json.loads(captured.out)
            assert not report["intake_coverage_complete"]
            assert report["channels"][0]["missing_source_ids"] == [2]
        else:
            assert "private details withheld" in captured.err
    assert len(clients) == 1 and clients[0].disconnected
    assert not clients[0].session.exists()
    assert session.read_bytes() == original
