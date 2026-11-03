"""Account-scoped, atomic retention for the read-only publisher's balance series."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .bybit.client import DEMO_BASE_URL, BybitClient
from .dashboard_balance import (
    MAX_BALANCE_BYTES,
    encode_balance_feed,
    validate_balance_history,
)
from .demo_balance import fetch_daily_wallet_history


def _retained(path: Path, scope: str) -> dict | None:
    if not path.exists():
        return None
    if path.stat().st_size > MAX_BALANCE_BYTES + 1024:
        raise ValueError("Retained balance history exceeds its size budget")
    saved = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(saved, dict) or set(saved) != {"scope", "history"}:
        raise ValueError("Invalid retained balance history")
    if saved["scope"] != scope:
        return None  # Never carry a previous account's balance into another key.
    return validate_balance_history(saved["history"])


def _merge(previous: dict | None, current: dict) -> dict:
    if previous is None:
        return current
    if datetime.fromisoformat(current["as_of"]) < datetime.fromisoformat(
        previous["as_of"]
    ):
        raise ValueError("Balance capture moved backwards")
    first = current["days"][0]
    cutoff = (
        (datetime.fromisoformat(current["as_of"]) - timedelta(days=89))
        .date()
        .isoformat()
    )
    older = [day for day in previous["days"] if cutoff <= day["date"] < first["date"]]
    if not older:
        return current
    last = older[-1]
    adjacent = date.fromisoformat(last["date"]) + timedelta(
        days=1
    ) == date.fromisoformat(first["date"])
    if (
        adjacent
        and last["balance"] is not None
        and first["balance"] is not None
        and first["change"] is not None
    ):
        if Decimal(last["balance"]) + Decimal(first["change"]) != Decimal(
            first["balance"]
        ):
            raise ValueError("Retained and refreshed wallet history disagree")
    gap = date.fromisoformat(last["date"]) + timedelta(days=1)
    while gap < date.fromisoformat(first["date"]):
        older.append(
            {
                "date": gap.isoformat(),
                "balance": None,
                "change": None,
                "complete": False,
                "activity": [],
            }
        )
        gap += timedelta(days=1)
    result = {
        **current,
        "start": older[0]["date"] + "T00:00:00+00:00",
        "days": older + current["days"],
    }
    return validate_balance_history(result)


def _public_window(history: dict | None) -> dict | None:
    if history is None:
        return None
    cutoff = (
        (datetime.fromisoformat(history["as_of"]) - timedelta(days=30))
        .date()
        .isoformat()
    )
    days = [day for day in history["days"] if day["date"] >= cutoff]
    return {
        **history,
        "start": max(history["start"], days[0]["date"] + "T00:00:00+00:00"),
        "days": days,
    }


def collect_balance_feed(
    client: BybitClient, path: Path, *, api_key: str, database_path: Path
) -> dict:
    """Never refresh stale evidence's timestamp or truncate a day to fit dispatch.

    Cache retains up to 90 UTC days; every refresh rechecks the last 30 full days.
    No raw ledger IDs or credentials are retained. Corrupt cache is an explicit
    failure, not permission to overwrite evidence or publish another account.
    """
    scope = hashlib.sha256(
        (DEMO_BASE_URL + "\0" + str(database_path.resolve()) + "\0" + api_key).encode()
    ).hexdigest()
    previous = _retained(path, scope)
    try:
        current = fetch_daily_wallet_history(client)
        history = _merge(previous, current)
        feed = {"schema_version": 1, "refresh_failed": False, "history": current}
        encode_balance_feed(feed)  # Fail before saving anything that cannot dispatch.
    except Exception:
        # The calling publisher already reports failures; no raw exchange errors
        # or private values are included in this strictly public status.
        return {
            "schema_version": 1,
            "refresh_failed": True,
            "history": _public_window(previous),
        }
    temporary = path.with_suffix(".next")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump({"scope": scope, "history": history}, output, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return feed
