"""Keep non-abortable work owned until it finishes, then propagate cancellation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)


async def finish_before_cancellation[Result](
    operation: Coroutine[object, object, Result],
) -> Result:
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Shield alone still cancels the caller immediately. Drain before its
        # surrounding account lock or client cleanup can unwind, even on recancel.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                logger.exception("Operation failed while deferring caller cancellation")
        raise
