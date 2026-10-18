"""Backup CLI integration against real application SQLite and Telethon sessions."""

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from cautious_crypto_bro.state_ownership import state_ownership
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/state_backup.py"


def inputs(tmp_path):
    state = tmp_path / "state with spaces"
    state.mkdir()
    database = state / "application.sqlite3"
    asyncio.run(IntentStore(database).initialize())
    session_path = state / "account.session"
    session = SQLiteSession(str(session_path))
    session.set_dc(1, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    session.save()
    session.close()
    return database, session_path, tmp_path / "backups/state.tgz"


def invoke(tmp_path, *arguments):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, arguments)],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=tmp_path,
        env=os.environ
        | {
            "PYTHONPATH": str(ROOT / "src"),
            "TMPDIR": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
        },
    )


def create(tmp_path, database, session, output):
    return invoke(
        tmp_path,
        "create",
        "--database",
        database,
        "--session",
        session,
        "--output",
        output,
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_backup_preserves_wal_rows_schema_session_and_sources(tmp_path):
    database, session, output = inputs(tmp_path)
    with sqlite3.connect(database) as original:
        original.execute("PRAGMA journal_mode=WAL")
        original.execute("PRAGMA wal_autocheckpoint=0")
        original.execute(
            "CREATE TABLE backup_probe (id INTEGER, content TEXT, opaque BLOB)"
        )
        original.execute(
            "INSERT INTO backup_probe VALUES (?, ?, ?)",
            (1, "SYNTHETIC_PRIVATE", b"\x00\xff"),
        )
        original.commit()
        assert database.with_name(database.name + "-wal").stat().st_size > 0
        before = digest(database), digest(session)
        result = create(tmp_path, database, session, output)
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["isolated_restore_verified"]
        assert receipt["archive_sha256"] == digest(output)
        assert receipt["files"]["database.sqlite3"]["schema_version"] == 9
        assert "SYNTHETIC_PRIVATE" not in result.stdout + result.stderr
        assert (digest(database), digest(session)) == before
        restored = tmp_path / "independent-restore"
        restored.mkdir()
        with tarfile.open(output, "r:gz") as archive:
            assert set(archive.getnames()) == {"database.sqlite3", "telegram.session"}
            archive.extractall(restored, filter="data")
        with sqlite3.connect(restored / "database.sqlite3") as recovered:
            assert tuple(recovered.iterdump()) == tuple(original.iterdump())
        recovered_session = SQLiteSession(str(restored / "telegram.session"))
        try:
            assert recovered_session.auth_key.key == bytes(range(256))
            assert recovered_session.server_address == "127.0.0.1"
        finally:
            recovered_session.close()
    verification = invoke(
        tmp_path, "verify", output, "--sha256", receipt["archive_sha256"]
    )
    assert verification.returncode == 0, verification.stderr
    assert json.loads(verification.stdout) == receipt
    assert not list((tmp_path / "scratch").iterdir())
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600


def test_active_owner_refuses_backup_before_creating_output(tmp_path):
    database, session, output = inputs(tmp_path)
    before = digest(database), digest(session)
    with state_ownership(database):
        result = create(tmp_path, database, session, output)
    assert result.returncode != 0 and "already owned" in result.stderr
    assert not output.exists()
    assert (digest(database), digest(session)) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt_session", "empty_database"])
def test_invalid_input_never_leaves_a_successful_archive(tmp_path, damage):
    database, session, output = inputs(tmp_path)
    if damage == "missing":
        database.unlink()
    elif damage == "corrupt_session":
        session.write_bytes(b"not a SQLite database")
    else:
        database.write_bytes(b"")
    result = create(tmp_path, database, session, output)
    assert result.returncode != 0
    assert not output.exists()
    assert "isolated_restore_verified" not in result.stdout


def test_existing_backup_is_never_overwritten(tmp_path):
    database, session, output = inputs(tmp_path)
    assert create(tmp_path, database, session, output).returncode == 0
    previous = output.read_bytes()
    with sqlite3.connect(database) as db:
        db.execute("UPDATE execution_policy SET risk_per_trade_pct = '0.5'")
    result = create(tmp_path, database, session, output)
    assert result.returncode != 0
    assert output.read_bytes() == previous


def test_receipt_mismatch_is_not_reported_as_a_verified_restore(tmp_path):
    database, session, output = inputs(tmp_path)
    assert create(tmp_path, database, session, output).returncode == 0
    result = invoke(tmp_path, "verify", output, "--sha256", "0" * 64)
    assert result.returncode != 0 and "SHA-256" in result.stderr
    assert "isolated_restore_verified" not in result.stdout


@pytest.mark.parametrize("unsafe", ["path", "link"])
def test_restore_rejects_unsafe_archive_members(tmp_path, unsafe):
    archive_path = tmp_path / "unsafe.tgz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo(
            "../outside" if unsafe == "path" else "database.sqlite3"
        )
        if unsafe == "link":
            member.type = tarfile.SYMTYPE
            member.linkname = "../outside"
        archive.addfile(member, io.BytesIO())
        archive.addfile(tarfile.TarInfo("telegram.session"), io.BytesIO())
    result = invoke(tmp_path, "verify", archive_path, "--sha256", digest(archive_path))
    assert result.returncode != 0 and "regular state files" in result.stderr
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "scratch/outside").exists()
