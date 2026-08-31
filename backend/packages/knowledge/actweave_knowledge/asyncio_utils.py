"""Cancellation-safe bridges from async Knowledge code to blocking calls.

``asyncio.to_thread`` cannot stop a synchronous call that has already begun.
Cancelling its awaiter alone therefore lets the thread continue as an orphan,
which is unsafe for leased Knowledge Tasks: cleanup or a retry could overlap
the still-running parser or object-store operation.

The helper below delays cancellation propagation until the started call has
settled. This deliberately means a permanently stuck synchronous dependency
can hold a caller beyond its timeout. That is safer than duplicating a
non-idempotent call. The MinIO SDK's own network timeout still bounds its
calls; a parser bug that never returns requires operator process recovery.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


async def run_sync_to_completion[**P, T](
    function: Callable[P, T],
    /,
    *args: P.args,
    cleanup_on_cancel: Callable[[T], object] | None = None,
    **kwargs: P.kwargs,
) -> T:
    """Run ``function`` off-loop and settle it before propagating cancellation.

    ``cleanup_on_cancel`` handles calls such as ``mkdtemp`` whose successful
    result would otherwise be lost when cancellation wins. Its own blocking
    work is also joined before the original cancellation is re-raised.
    """

    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    pending_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as exc:
            if worker.cancelled():
                raise
            if pending_cancellation is None:
                pending_cancellation = exc
        except BaseException:
            if pending_cancellation is None:
                raise
            raise pending_cancellation from None

    if pending_cancellation is None:
        return result

    if cleanup_on_cancel is not None:
        cleanup = asyncio.create_task(asyncio.to_thread(cleanup_on_cancel, result))
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    break
            except BaseException:
                # Cancellation remains authoritative. Avoid logging the raw
                # exception because storage locators can appear in messages.
                logger.error("knowledge blocking-call cancellation cleanup failed")
                break
    raise pending_cancellation
