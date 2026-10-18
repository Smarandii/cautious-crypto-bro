"""Cross-process ownership and application startup ordering, without network I/O."""

import asyncio
import os
import sqlite3
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration

HOLDER = """
import sqlite3, sys
from pathlib import Path
from cautious_crypto_bro.state_ownership import state_ownership
with state_ownership(Path(sys.argv[1])):
    with sqlite3.connect(sys.argv[1]) as db:
        db.execute('CREATE TABLE IF NOT EXISTS owner_work(status TEXT)')
        db.execute("INSERT INTO owner_work VALUES ('EXECUTING')")
    print('OWNED', flush=True)
    sys.stdin.readline()
"""
CONTENDER = """
import sys
from pathlib import Path
from cautious_crypto_bro.state_ownership import state_ownership
with state_ownership(Path(sys.argv[1])):
    print('ACQUIRED', flush=True)
"""


@pytest.mark.parametrize("crash", [False, True])
def test_second_startup_cannot_touch_owned_state_and_owner_exit_releases_it(
    tmp_path, monkeypatch, crash
):
    database = tmp_path / "state.db"
    env = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))
    owner = subprocess.Popen(
        [sys.executable, "-u", "-c", HOLDER, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert owner.stdout.readline().strip() == "OWNED"
        app = import_module("cautious_crypto_bro.main")
        from cautious_crypto_bro.supervision_health import (
            publish_success,
            supervision_health_path,
        )

        progress = supervision_health_path(database)
        publish_success(progress)
        owner_progress = progress.read_bytes()
        # Independent state paths are not blocked by an unrelated owner.
        with app.state_ownership(tmp_path / "other.db"):
            pass
        monkeypatch.setattr(
            app,
            "get_settings",
            lambda: SimpleNamespace(database_path=database, log_level="INFO"),
        )

        def forbidden(*args, **kwargs):
            pytest.fail("Duplicate startup reached storage or exchange construction")

        monkeypatch.setattr(app, "IntentStore", forbidden)
        monkeypatch.setattr(app, "BybitDemoExecutor", forbidden)
        with pytest.raises(RuntimeError, match="already owned"):
            asyncio.run(app.async_main())
        assert progress.read_bytes() == owner_progress
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT status FROM owner_work").fetchall() == [
                ("EXECUTING",)
            ]
        # The same owner remains protected after a rejected contender exits.
        rejected = subprocess.run(
            [sys.executable, "-c", CONTENDER, str(database)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert rejected.returncode != 0 and "already owned" in rejected.stderr
        if crash:
            owner.kill()
        else:
            owner.stdin.write("stop\n")
            owner.stdin.flush()
        owner.wait(timeout=10)
        successor = subprocess.run(
            [sys.executable, "-c", CONTENDER, str(database)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert successor.returncode == 0, successor.stderr
        assert successor.stdout.strip() == "ACQUIRED"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        for stream in (owner.stdin, owner.stdout, owner.stderr):
            stream.close()
