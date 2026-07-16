"""Command-line entry point for the independent M6 Worker role."""

from __future__ import annotations

import asyncio
import signal
from contextlib import AsyncExitStack
from functools import partial

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.reliability.cutover import ReliabilityCutoverGuard
from app.reliability.execution import (
    PrivateRunJobHandler,
    PrivateRunJobTerminalPort,
    RunAgentPrivateExecutor,
)
from app.reliability.owner_refs import AuditHmacKeyring
from app.reliability.workers import WorkerRegistry
from app.worker.service import JobHandler, WorkerService
from deerflow.config import get_app_config
from deerflow.persistence import close_engine, get_session_factory, init_engine
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.runtime import make_store, make_stream_bridge
from deerflow.runtime.checkpointer.async_provider import make_checkpointer
from deerflow.runtime.events.store.db import DbRunEventStore

WORKER_VERSION = "m6"


async def run_worker(
    *,
    handlers: dict[str, JobHandler] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    config = await asyncio.to_thread(get_app_config)
    if not config.worker.enabled:
        return
    async with AsyncExitStack() as stack:
        await init_engine(config.database)
        stack.push_async_callback(close_engine)
        session_factory = get_session_factory()
        await ReliabilityCutoverGuard(session_factory).require_worker_open()
        audit_keyring = AuditHmacKeyring.from_environment()
        repository_builder = partial(
            JobRepository,
            owner_ref_hasher=audit_keyring.job_owner_ref,
            terminal_port=PrivateRunJobTerminalPort(),
        )
        active_handlers = handlers
        if active_handlers is None:
            bridge = await stack.enter_async_context(
                make_stream_bridge(config),
            )
            raw_checkpointer = await stack.enter_async_context(
                make_checkpointer(config),
            )
            store = await stack.enter_async_context(make_store(config))
            project_checkpointer = ProjectScopedCheckpointer(
                raw_checkpointer,
                session_factory,
            )
            executor = RunAgentPrivateExecutor(
                session_factory,
                app_config=config,
                bridge=bridge,
                project_checkpointer=project_checkpointer,
                store=store,
                event_store=DbRunEventStore(
                    session_factory,
                    max_trace_content=config.run_events.max_trace_content,
                ),
            )
            active_handlers = {
                "private_run": PrivateRunJobHandler(
                    session_factory,
                    executor=executor,
                    retry_initial_seconds=(config.worker.retry_initial_seconds),
                    retry_max_seconds=config.worker.retry_max_seconds,
                    job_repository_builder=repository_builder,
                    project_checkpointer=project_checkpointer,
                )
            }
        registry = WorkerRegistry(session_factory, version=WORKER_VERSION)
        service = WorkerService(
            session_factory,
            registry,
            active_handlers,
            config.worker,
            repository_builder=repository_builder,
        )
        await service.run(stop_event or asyncio.Event())


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
