"""Knowledge task-worker integration for the Worker process.

The Knowledge task worker runs inside the existing ``app.worker`` process and
shares its stop event. Either loop failing stops the other and the shared
failure propagates, so the process exits and the operator's restart policy
recovers both loops together.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from actweave_knowledge import KnowledgeModule

logger = logging.getLogger(__name__)


async def run_knowledge_task_worker(module: KnowledgeModule, stop_event: asyncio.Event) -> None:
    """Run the module's task worker until ``stop_event`` is set."""

    await module.run_worker(stop_event)


async def run_worker_loops(
    *,
    run_main: Callable[[], Awaitable[None]],
    run_knowledge: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event,
) -> None:
    """Run the main Worker loop and the Knowledge loop as one unit.

    When either loop finishes — normally via the stop event or by raising —
    the stop event is set so the other loop drains and exits, then the first
    real failure (if any) is re-raised.
    """

    main_task = asyncio.create_task(run_main(), name="worker-main-loop")
    knowledge_task = asyncio.create_task(run_knowledge(), name="knowledge-task-worker")
    tasks = (main_task, knowledge_task)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    stop_event.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [(task.get_name(), result) for task, result in zip(tasks, results, strict=True) if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)]
    if not failures:
        return
    # Only the first failure propagates; the rest must still reach the
    # operator's log (e.g. a shared database outage killing both loops).
    for name, failure in failures[1:]:
        logger.error("%s also failed while shutting down together", name, exc_info=failure)
    raise failures[0][1]
