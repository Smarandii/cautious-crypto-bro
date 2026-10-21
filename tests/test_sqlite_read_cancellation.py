import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from cautious_crypto_bro.state_ownership import state_ownership
from cautious_crypto_bro.storage import IntentStore


def test_cancelled_read_keeps_ownership_until_connection_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    asyncio.run(IntentStore(database).initialize())
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    connect = sqlite3.connect
    workers: list[threading.Thread] = []

    class Connection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            closed.set()

    def blocked_connect(*args, **kwargs):
        workers.append(threading.current_thread())
        entered.set()
        if not release.wait(5):
            raise TimeoutError("SQLite connector was not released")
        return connect(*args, factory=Connection, **kwargs)

    async def run() -> None:
        async def owner() -> None:
            with state_ownership(database):
                await IntentStore(database).get_active_position_strategies()

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
        assert not any(worker.is_alive() for worker in workers)
        with state_ownership(database):
            pass

    monkeypatch.setattr(sqlite3, "connect", blocked_connect)
    asyncio.run(run())
