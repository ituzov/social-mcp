"""Keep a serialized side effect inside its lock until completion on cancellation."""

import asyncio
from collections.abc import Awaitable
from contextlib import suppress


async def finish_before_cancel[T](operation: Awaitable[T]) -> T:
    """Hold the lock until the side effect finishes, even when cancelled."""
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        with suppress(Exception, asyncio.CancelledError):
            task.result()
        raise asyncio.CancelledError
    return task.result()
