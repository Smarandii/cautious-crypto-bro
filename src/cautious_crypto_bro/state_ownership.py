"""One application owner for a shared local state database."""

from __future__ import annotations

import errno
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def state_ownership(database_path: Path) -> Iterator[None]:
    database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database_path.with_name(database_path.name + ".owner.lock")
    # Keep the inode stable: unlinking a lock file permits a second owner.
    with lock_path.open("a+b") as handle:
        if sys.platform == "win32":
            if lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
        try:
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise RuntimeError(
                    f"Application state already owned by another process: {database_path}"
                ) from exc
            raise
        # Closing the descriptor releases ownership, including process exit/crash.
        yield
