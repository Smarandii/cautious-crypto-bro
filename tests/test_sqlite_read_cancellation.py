import asyncio
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from cautious_crypto_bro.state_ownership import state_ownership
from cautious_crypto_bro.storage import IntentStore


@pytest.mark.parametrize("operation", ["read", "write"])
def test_cancelled_connection_keeps_ownership_until_connection_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    database = tmp_path / "state.sqlite3"
    asyncio.run(IntentStore(database).initialize())
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    connect = sqlite3.connect
    workers: list[threading.Thread] = []
    statements: list[str] = []

    class Connection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            closed.set()

    def blocked_connect(*args, **kwargs):
        workers.append(threading.current_thread())
        entered.set()
        if not release.wait(5):
            raise TimeoutError("SQLite connector was not released")
        connection = connect(*args, factory=Connection, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    async def run() -> None:
        async def owner() -> None:
            with state_ownership(database):
                store = IntentStore(database)
                if operation == "read":
                    await store.get_active_position_strategies()
                else:
                    await store.defer_unavailable_source_messages(
                        1, (1,), lease_seconds=300
                    )

        task = asyncio.create_task(owner())
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done(), "Cancellation released SQLite ownership early"
                with pytest.raises(RuntimeError, match="already owned"):
                    with state_ownership(database):
                        pass
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            for worker in workers:
                await asyncio.to_thread(worker.join, 3)
        assert closed.is_set()
        assert not statements, "Cancelled connection setup submitted application SQL"
        assert not any(worker.is_alive() for worker in workers)
        with state_ownership(database):
            pass

    monkeypatch.setattr(sqlite3, "connect", blocked_connect)
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["query", "close"])
def test_cancelled_write_preserves_transaction_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    database = tmp_path / "state.sqlite3"
    store = IntentStore(database)
    asyncio.run(store.initialize())
    original_policy = asyncio.run(store.get_execution_policy())
    replacement = original_policy.model_copy(
        update={"risk_per_trade_pct": Decimal("2")}
    )
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    connect = sqlite3.connect

    def block() -> None:
        entered.set()
        if not release.wait(5):
            raise TimeoutError("SQLite operation was not released")

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if stage == "query" and "INSERT INTO execution_policy" in sql:
                block()
            return cursor

        def close(self) -> None:
            if stage == "close":
                block()
            super().close()
            closed.set()

    def tracked_connect(*args, **kwargs):
        return connect(*args, factory=Connection, **kwargs)

    async def run() -> None:
        async def owner() -> None:
            with state_ownership(database):
                await store.set_execution_policy(replacement)

        task = asyncio.create_task(owner())
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done()
                with pytest.raises(RuntimeError, match="already owned"):
                    with state_ownership(database):
                        pass
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert closed.is_set()

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", tracked_connect)
        asyncio.run(run())
    saved = asyncio.run(store.get_execution_policy())
    assert saved == (original_policy if stage == "query" else replacement)
