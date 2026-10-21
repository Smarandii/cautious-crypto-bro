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
