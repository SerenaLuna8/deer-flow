"""Command-line entry point for the independent M6 Worker role."""

from __future__ import annotations

import asyncio
import signal

from app.reliability.cutover import ReliabilityCutoverGuard
from app.reliability.workers import WorkerRegistry
from app.worker.service import JobHandler, WorkerService
from deerflow.config import get_app_config
from deerflow.persistence import close_engine, get_session_factory, init_engine

WORKER_VERSION = "m6"


async def run_worker(
    *,
    handlers: dict[str, JobHandler] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    config = await asyncio.to_thread(get_app_config)
    if not config.worker.enabled:
        return
    await init_engine(config.database)
    try:
        session_factory = get_session_factory()
        await ReliabilityCutoverGuard(session_factory).require_worker_open()
        registry = WorkerRegistry(session_factory, version=WORKER_VERSION)
        service = WorkerService(
            session_factory,
            registry,
            handlers or {},
            config.worker,
        )
        await service.run(stop_event or asyncio.Event())
    finally:
        await close_engine()


def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(run_worker(stop_event=stop_event))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
