"""Local authorization validates a staged real session before replacing state."""

import asyncio
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession
from test_state_backup import inputs

from cautious_crypto_bro.state_ownership import state_ownership
from cautious_crypto_bro.telegram_source import SourceAuthorizationRequired

pytestmark = pytest.mark.integration
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/authorize_telegram.py"


def helper(monkeypatch, database, session, user_id):
    spec = importlib.util.spec_from_file_location("authorize_telegram_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    created = []

    class Client:
        def __init__(self, name, api_id, api_hash):
            created.append(name)
            self.path = Path(name)
            self.session = SQLiteSession(name)

        async def start(self, *, phone, code_callback, password):
            assert phone() == code_callback() == password() == "synthetic-hidden-input"
            self.session.set_dc(1, "127.0.0.1", 443)
            self.session.auth_key = AuthKey(bytes(reversed(range(256))))
            self.session.save()

        async def get_me(self):
            return SimpleNamespace(id=user_id)

        async def disconnect(self):
            self.session.close()

        async def log_out(self):
            self.session.close()
            self.path.unlink()

    monkeypatch.setattr(module, "TelegramClient", Client)
    monkeypatch.setattr(
        module.getpass, "getpass", lambda prompt: "synthetic-hidden-input"
    )
    settings = SimpleNamespace(
        database_path=database,
        telegram_session_name=str(session),
        telegram_approver_user_id=77,
        telegram_api_id=1,
        telegram_api_hash="fake",
    )
    return module, settings, created


@pytest.mark.parametrize("user_id", [77, 88])
def test_local_authorization_preserves_old_session_until_identity_is_verified(
    monkeypatch, tmp_path, user_id, capsys
):
    database, session, _ = inputs(tmp_path)
    before = session.read_bytes()
    identity = database.with_suffix(".telegram-identity.json")
    identity.write_text('{"user_id": 77}')
    module, settings, created = helper(monkeypatch, database, session, user_id)
    if user_id == 88:
        with pytest.raises(SourceAuthorizationRequired, match="identity mismatch"):
            asyncio.run(module.authorize(settings))
        assert session.read_bytes() == before
        assert not list(session.parent.glob(session.name + ".before-authorization-*"))
    else:
        asyncio.run(module.authorize(settings))
        backups = list(session.parent.glob(session.name + ".before-authorization-*"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as db:
            assert db.execute("SELECT auth_key FROM sessions").fetchone()[0] == bytes(
                range(256)
            )
        replaced = SQLiteSession(str(session))
        try:
            assert replaced.auth_key.key == bytes(reversed(range(256)))
        finally:
            replaced.close()
    assert len(created) == 1
    assert not list(session.parent.glob(".telegram-authorization-*"))
    assert identity.read_text() == '{"user_id": 77}'
    captured = capsys.readouterr()
    assert "synthetic-hidden-input" not in captured.out + captured.err
    with state_ownership(database):
        pass


def test_active_application_owner_blocks_local_authorization(monkeypatch, tmp_path):
    database, session, _ = inputs(tmp_path)
    before = session.read_bytes()
    module, settings, created = helper(monkeypatch, database, session, 77)
    with state_ownership(database):
        with pytest.raises(RuntimeError, match="already owned"):
            asyncio.run(module.authorize(settings))
    assert created == [] and session.read_bytes() == before


def test_failed_client_construction_cleans_staged_session(monkeypatch, tmp_path):
    database, session, _ = inputs(tmp_path)
    before = session.read_bytes()
    module, settings, _ = helper(monkeypatch, database, session, 77)

    def unavailable_client(*args):
        raise OSError("synthetic construction failure")

    monkeypatch.setattr(module, "TelegramClient", unavailable_client)
    with pytest.raises(OSError, match="construction failure"):
        asyncio.run(module.authorize(settings))
    assert session.read_bytes() == before
    assert not list(session.parent.glob(".telegram-authorization-*"))
