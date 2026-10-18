"""Exercise real POSIX SIGTERM in an isolated process without external APIs."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cautious_crypto_bro.approval_bot import ApprovalBot

pytestmark = pytest.mark.integration


def test_polling_startup_failure_is_propagated_and_logged(caplog):
    async def run():
        bot = ApprovalBot(
            token="123456:synthetic-offline-token",
            approval_chat_id=1,
            approver_user_id=1,
            store=object(),
            executor=object(),
        )

        async def fail(**kwargs):
            raise RuntimeError("Polling startup failed")

        bot._dispatcher.startup.register(fail)
        try:
            with pytest.raises(RuntimeError, match="Polling startup failed"):
                await bot.run()
            assert "Telegram task failed during shutdown" in caplog.text
        finally:
            await bot.close()

    asyncio.run(run())


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX SIGTERM; also run in Linux container"
)
@pytest.mark.parametrize("phase", ["startup", "polling"])
def test_sigterm_drains_work_and_releases_ownership(tmp_path, phase):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("shutdown_probe.py")),
            phase,
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        # Allow cold dependency imports; the probe times the application separately.
        timeout=60,
        env=os.environ
        | {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result.stderr
    events = json.loads(result.stdout.strip().splitlines()[-1])
    assert events[-3:] == ["client.closed", "cleanup.complete", "owner.released"]
    if phase == "polling":
        assert events.index("worker.finished") < events.index("outcome.persisted")
        assert events.index("outcome.persisted") < events.index("handler.finished")
        assert events.index("handler.finished") < events.index("client.closed")
        assert events.count("background.stopped") >= 6
