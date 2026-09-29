from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import aiosqlite

from cautious_crypto_bro.domain import StoreError


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
            async with aiosqlite.connect(self._database_path) as db:
                if row_factory:
                    db.row_factory = aiosqlite.Row

                cursor = await db.execute(query, parameters)

                return await (cursor.fetchall() if many else cursor.fetchone())
        except (sqlite3.Error, aiosqlite.Error) as exc:
            raise StoreError(f"SQLite operation failed: {exc}") from exc
