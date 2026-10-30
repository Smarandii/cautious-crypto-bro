from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

import aiosqlite

from cautious_crypto_bro.cancellation import finish_before_cancellation
from cautious_crypto_bro.domain import StoreError


@asynccontextmanager
async def sqlite_connection(database_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    db = aiosqlite.connect(database_path)

    async def open_connection() -> None:
        await db

    try:
        # Drain opening even if cancelled before __aenter__ returns, so the
        # connection can be closed before its caller releases state ownership.
        await finish_before_cancellation(open_connection())
        yield db
    finally:
        # Shield cleanup, not the transaction body: cancellation must still
        # roll back uncommitted writes rather than carry on to a later commit.
        await finish_before_cancellation(db.close())


@asynccontextmanager
async def keep_wal_readable(database_path: Path) -> AsyncIterator[None]:
    """Retain WAL sidecars for external readers without pinning a snapshot.

    SQLite removes the sidecars after its last connection closes. A reader on a
    read-only mount cannot recreate them. Complete this read before yielding so
    the idle connection holds no transaction and writers can still checkpoint.
    The application owns this lifetime; per-operation transactions stay separate.
    """
    async with sqlite_connection(database_path) as db:
        await db.execute("PRAGMA query_only=ON")
        async with db.execute("SELECT count(*) FROM sqlite_master") as cursor:
            await cursor.fetchone()
        yield


class _Store(Protocol):
    _database_path: Path

    async def _fetch(
        self,
        query: str,
        parameters: Sequence[object] = (),
        *,
        many: bool = False,
        row_factory: bool = False,
    ) -> Any: ...


class SQLiteConnectionMixin(_Store):
    def __init__(
        self,
        database_path: Path,
    ) -> None:
        self._database_path = database_path

    async def _fetch(
        self,
        query: str,
        parameters: Sequence[object] = (),
        *,
        many: bool = False,
        row_factory: bool = False,
    ) -> Any:
        try:
            async with sqlite_connection(self._database_path) as db:
                if row_factory:
                    db.row_factory = aiosqlite.Row

                cursor = await db.execute(query, parameters)

                return await (cursor.fetchall() if many else cursor.fetchone())
        except (sqlite3.Error, aiosqlite.Error) as exc:
            raise StoreError(f"SQLite operation failed: {exc}") from exc
