"""Read-only freshness probe for successful supervision passes."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

MAX_SUCCESS_AGE_SECONDS = 300


def supervision_health_path(database: Path) -> Path:
    database = database.resolve()
    return database.with_name(database.name + ".supervision-health.json")


@contextmanager
def supervision_health_lifecycle(path: Path) -> Iterator[None]:
    # Enter only after acquiring state ownership; another owner must not erase it.
    path.unlink(missing_ok=True)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def publish_success(path: Path) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name, delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump({"version": 1, "last_success_monotonic": monotonic()}, output)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_progress(path: Path) -> tuple[bool, str]:
    try:
        with path.open("rb") as source:
            data = source.read(4097)
        if len(data) > 4096:
            raise ValueError("Oversized progress record")
        record = json.loads(data)
        if not isinstance(record, dict) or record.get("version") != 1:
            raise ValueError("Unsupported progress record")
        last_success = record["last_success_monotonic"]
        if (
            isinstance(last_success, bool)
            or not isinstance(last_success, (int, float))
            or not math.isfinite(last_success)
        ):
            raise ValueError("Invalid progress time")
        age = monotonic() - last_success
        if age < 0:
            raise ValueError("Progress clock moved backwards")
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return False, "Supervision progress unavailable or invalid"
    if age > MAX_SUCCESS_AGE_SECONDS:
        return False, f"Successful supervision pass is stale ({age:.0f}s old)"
    return True, f"Successful supervision pass is recent ({age:.0f}s old)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    healthy, message = check_progress(supervision_health_path(args.database))
    print(message)
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
