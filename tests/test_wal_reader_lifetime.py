import asyncio
import sqlite3

import pytest

from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.storage._connection import keep_wal_readable

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("cancel", [False, True])
def test_idle_connection_retains_live_wal_without_blocking_checkpoints(
    tmp_path, cancel
):
    async def run():
        database = tmp_path / "state.sqlite3"
        await IntentStore(database).initialize()
        wal = database.with_name(database.name + "-wal")
        shm = database.with_name(database.name + "-shm")
        ready = asyncio.Event()
        release = asyncio.Event()

        async def owner():
            async with keep_wal_readable(database):
                ready.set()
                await release.wait()

        task = asyncio.create_task(owner())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            assert wal.exists() and shm.exists()
            with sqlite3.connect(database) as writer:
                writer.execute("CREATE TABLE lifetime_probe (value INTEGER)")
                writer.execute("INSERT INTO lifetime_probe VALUES (1)")
            writer.close()
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as reader:
                assert reader.execute(
                    "SELECT value FROM lifetime_probe"
                ).fetchall() == [(1,)]
            reader.close()
            with sqlite3.connect(database) as writer:
                writer.execute("INSERT INTO lifetime_probe VALUES (2)")
                writer.commit()
                # A keeper with an unfinished read would prevent full truncation.
                assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                    0,
                    0,
                    0,
                )
            writer.close()
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as reader:
                assert reader.execute(
                    "SELECT value FROM lifetime_probe"
                ).fetchall() == [(1,), (2,)]
            reader.close()
        finally:
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                await task
        assert not wal.exists() and not shm.exists()

    asyncio.run(run())
