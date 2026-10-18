"""Real supervision, SQLite and read-only subprocess health checks."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare

from cautious_crypto_bro import supervision_health as health
from cautious_crypto_bro.domain import ApprovalMode, IntentStatus
from cautious_crypto_bro.position_supervisor import PositionSupervisor
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cli_is_read_only_and_checks_real_cross_process_progress(tmp_path):
    database = tmp_path / "state.sqlite3"
    database.write_bytes(b"This must never be opened as SQLite by the probe")
    path = health.supervision_health_path(database)
    now = health.monotonic()
    cases = [
        (None, 1),
        (json.dumps({"version": 1, "last_success_monotonic": now}), 0),
        (json.dumps({"version": 1, "last_success_monotonic": now - 301}), 1),
        (json.dumps({"version": 1, "last_success_monotonic": now + 60}), 1),
        (json.dumps({"version": 1, "last_success_monotonic": float("nan")}), 1),
        (json.dumps({"version": 1, "last_success_monotonic": True}), 1),
        (json.dumps({"version": 1, "last_success_monotonic": 10**1000}), 1),
        ('{"version":', 1),
        ("{}" + " " * 4097, 1),
    ]
    env = os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    for contents, expected in cases:
        if contents is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(contents, encoding="utf-8")
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cautious_crypto_bro.supervision_health",
                "--database",
                str(database),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == expected, result.stderr
        assert "supervision" in result.stdout.lower()
        assert not result.stderr
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_success_failure_and_recovery_follow_actual_supervision_passes(
    tmp_path, caplog
):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        path = health.supervision_health_path(database)
        supervisor = PositionSupervisor(
            store=store,
            executor=object(),
            mutation_lock=asyncio.Lock(),
            poll_interval_seconds=0.05,
            progress_path=path,
        )
        task = asyncio.create_task(supervisor.run())
        try:
            await until(lambda: path.exists() and health.check_progress(path)[0])
        finally:
            await stop(task)
        with sqlite3.connect(database) as db:
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'position_strategies'"
            ).fetchone()[0]
            db.execute("DROP TABLE position_strategies")
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "last_success_monotonic": health.monotonic() - 301,
                }
            )
        )
        stale = path.read_bytes()
        task = asyncio.create_task(supervisor.run())
        try:
            await until(
                lambda: (
                    caplog.text.count("Position supervisor reconciliation failed") >= 2
                )
            )
            assert not task.done()
            assert path.read_bytes() == stale
            assert not health.check_progress(path)[0]
        finally:
            await stop(task)
        with sqlite3.connect(database) as db:
            db.execute(schema)
        task = asyncio.create_task(supervisor.run())
        try:
            await until(lambda: health.check_progress(path)[0])
            assert path.read_bytes() != stale
        finally:
            await stop(task)

    asyncio.run(run())


def test_progress_write_failure_does_not_stop_protection_loop(tmp_path, caplog):
    async def run():
        database = tmp_path / "state.db"
        store = IntentStore(database)
        await store.initialize()
        path = health.supervision_health_path(database)
        path.mkdir()
        supervisor = PositionSupervisor(
            store=store,
            executor=object(),
            mutation_lock=asyncio.Lock(),
            poll_interval_seconds=0.05,
            progress_path=path,
        )
        task = asyncio.create_task(supervisor.run())
        try:
            await until(
                lambda: (
                    caplog.text.count(
                        "Failed to publish successful supervision progress"
                    )
                    >= 2
                )
            )
            assert not task.done()
            assert not health.check_progress(path)[0]
            assert list(tmp_path.glob(path.name + "*")) == [path]
        finally:
            await stop(task)

    asyncio.run(run())


def test_exchange_failures_cannot_publish_successful_progress(tmp_path):
    async def run():
        class UnavailableExchange(Exchange):
            unavailable = False
            failures = 0

            def handle(self, request):
                if self.unavailable and request.url.path == "/v5/position/list":
                    self.failures += 1
                    raise httpx.ReadTimeout(
                        "Synthetic exchange outage", request=request
                    )
                return super().handle(request)

        database = tmp_path / "state.db"
        path = health.supervision_health_path(database)
        exchange = UnavailableExchange()
        with executor_for(exchange) as executor:
            store, intent, _, coordinator, _ = await prepare(
                database, exchange, executor
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
            path.write_text(
                json.dumps(
                    {"version": 1, "last_success_monotonic": health.monotonic() - 301}
                )
            )
            stale = path.read_bytes()
            exchange.unavailable = True
            supervisor = PositionSupervisor(
                store=store,
                executor=executor,
                mutation_lock=asyncio.Lock(),
                poll_interval_seconds=0.05,
                progress_path=path,
            )
            task = asyncio.create_task(supervisor.run())
            try:
                await until(lambda: exchange.failures >= 2)
                assert path.read_bytes() == stale
                assert not health.check_progress(path)[0]
                exchange.unavailable = False
                await until(lambda: health.check_progress(path)[0])
            finally:
                await stop(task)

    asyncio.run(run())
