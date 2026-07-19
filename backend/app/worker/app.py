"""Command-line entry point for the independent M6 Worker role."""

from __future__ import annotations

import asyncio
import signal
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from functools import partial

from app.automations.reconciliation import AutomationReconciler
from app.final_schema import FinalSchemaProbe
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.execution import (
    PrivateRunJobHandler,
    PrivateRunJobTerminalPort,
    RunAgentPrivateExecutor,
)
from app.reliability.owner_refs import AuditHmacKeyring
from app.reliability.workers import WorkerRegistry
from app.worker.service import JobHandler, WorkerService
from deerflow.config import get_app_config
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence import close_engine, get_session_factory, init_engine
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.runtime import make_store
from deerflow.runtime.checkpointer.async_provider import make_checkpointer
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.events.stream import PostgresStreamBridge

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
        async with session_factory() as session:
            await FinalSchemaProbe().require_ready(session)
        automation_reconciler = AutomationReconciler(session_factory)
        await automation_reconciler.reconcile_restart(
            datetime.now(UTC),
        )
        audit_keyring = AuditHmacKeyring.from_environment()
        from app.audit.service import AuditService, _bind_worker_audit_process
        from app.audit.sinks import OperationalAuditSink

        audit_service = AuditService(session_factory, audit_keyring)
        audit_sink = OperationalAuditSink(
            audit_service,
            process_context=_bind_worker_audit_process(audit_service),
        )
        quota_config = getattr(config, "quotas", None) or QuotaConfig()
        quota_enforcer = ProjectQuotaEnforcer(
            QuotaService(
                session_factory,
                quota_config,
                source_ref_hasher=audit_keyring,
            )
        )
        terminal_port = PrivateRunJobTerminalPort(
            quota=quota_enforcer,
            audit=audit_sink,
        )

        async def reconcile_deferred_automation_terminals() -> None:
            if not terminal_port.take_automation_reconciliation_pending():
                return
            try:
                await automation_reconciler.reconcile_restart(datetime.now(UTC))
            except asyncio.CancelledError:
                terminal_port.restore_automation_reconciliation_pending()
                raise
            except Exception:
                terminal_port.restore_automation_reconciliation_pending()
                raise

        repository_builder = partial(
            JobRepository,
            owner_ref_hasher=audit_keyring.job_owner_ref,
            terminal_port=terminal_port,
        )
        active_handlers = handlers
        if active_handlers is None:
            bridge = PostgresStreamBridge(session_factory)
            raw_checkpointer = await stack.enter_async_context(
                make_checkpointer(config),
            )
            store = await stack.enter_async_context(make_store(config))
            project_checkpointer = ProjectScopedCheckpointer(
                raw_checkpointer,
                session_factory,
                quota=quota_enforcer,
            )
            executor = RunAgentPrivateExecutor(
                session_factory,
                app_config=config,
                bridge=bridge,
                project_checkpointer=project_checkpointer,
                store=store,
                event_store=DbRunEventStore(session_factory),
                quota=quota_enforcer,
            )
            private_run_handler = PrivateRunJobHandler(
                session_factory,
                executor=executor,
                retry_initial_seconds=(config.worker.retry_initial_seconds),
                retry_max_seconds=config.worker.retry_max_seconds,
                job_repository_builder=repository_builder,
                project_checkpointer=project_checkpointer,
                quota=quota_enforcer,
                audit=audit_sink,
            )
            active_handlers = {
                "private_run": private_run_handler,
                "automation_run": private_run_handler,
            }
        registry = WorkerRegistry(session_factory, version=WORKER_VERSION)
        service = WorkerService(
            session_factory,
            registry,
            active_handlers,
            config.worker,
            repository_builder=repository_builder,
            after_claim_commit=reconcile_deferred_automation_terminals,
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
