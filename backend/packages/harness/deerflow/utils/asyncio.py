"""Cancellation-safe asyncio helpers for resource-owning thread work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def joined_to_thread[ResultT](
    operation: Callable[..., ResultT],
    /,
    *args: Any,
    **kwargs: Any,
) -> ResultT:
    """Run blocking work and join its real thread before propagating cancel.

    ``asyncio.to_thread`` cancels only its asyncio wrapper. Resource-owning
    callers need the underlying worker to finish before their ``finally`` can
    acknowledge quiescence, so cancellation is remembered and re-raised only
    after the thread Future reaches a real terminal state.
    """

    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except Exception:
            break
    if cancellation is not None:
        # Cancellation is the caller-visible semantic outcome. Consume any
        # later worker failure only after its real thread has terminated.
        if not task.cancelled():
            task.exception()
        raise cancellation
    return task.result()


__all__ = ["joined_to_thread"]
