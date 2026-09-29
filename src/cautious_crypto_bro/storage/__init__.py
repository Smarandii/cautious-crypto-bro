from __future__ import annotations

from .migrations import (
    MIGRATION_4_TO_5_SQL,
    MIGRATION_5_TO_6_SQL,
    MIGRATION_6_TO_7_SQL,
    MIGRATION_7_TO_8_SQL,
)
from .schema import LATEST_SCHEMA_SQL, LATEST_SCHEMA_VERSION
from .store import SQLiteStore

IntentStore = SQLiteStore

__all__ = [
    "IntentStore",
    "LATEST_SCHEMA_SQL",
    "LATEST_SCHEMA_VERSION",
    "MIGRATION_4_TO_5_SQL",
    "MIGRATION_5_TO_6_SQL",
    "MIGRATION_6_TO_7_SQL",
    "MIGRATION_7_TO_8_SQL",
    "SQLiteStore",
]
