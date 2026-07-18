"""Command-line entry point for the independent M6 Scheduler role."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass

from app.automations.dispatcher import AutomationDispatcher
from app.automations.occurrences import AutomationOccurrenceService
from app.automations.ownership import AutomationSchedulerOwnership
from app.automations.reconciliation import AutomationReconciler
from app.final_schema import FinalSchemaProbe
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from app.scheduler.service import ScheduledTaskService
from deerflow.config import get_app_config
from deerflow.persistence import (
    close_engine,
    get_engine,
    get_session_factory,
    init_engine,
)


@dataclass(slots=True)
class SchedulerApp:
    """Own the scheduler lock and polling service for one process lifetime."""

    enabled: bool
    ownership: AutomationSchedulerOwnership
    service: ScheduledTaskService

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            return
        async with self.ownership.hold():
            await self.service.start()
            stop_wait = asyncio.create_task(
                stop_event.wait(),
                name="automation-scheduler-stop-wait",
            )
            try:
                poll_task = self.service.task
                if poll_task is None:
                    await stop_wait
                else:
                    done, _pending = await asyncio.wait(
                        {stop_wait, poll_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if poll_task in done:
                        await poll_task
            finally:
                stop_wait.cancel()
                await asyncio.gather(stop_wait, return_exceptions=True)
                await self.service.stop()


async def run_scheduler(
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    config = await asyncio.to_thread(get_app_config)
    if not config.scheduler.enabled:
        return
    await init_engine(config.database)
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            await FinalSchemaProbe().require_ready(session)
        engine = get_engine()
        if engine is None:
            raise RuntimeError("scheduler persistence engine is unavailable")
        ownership = AutomationSchedulerOwnership(engine)
        audit_keyring = AuditHmacKeyring.from_environment()
        from app.audit.service import AuditService, _bind_scheduler_audit_process
        from app.audit.sinks import OperationalAuditSink

        audit_service = AuditService(session_factory, audit_keyring)
        audit_sink = OperationalAuditSink(
            audit_service,
            process_context=_bind_scheduler_audit_process(audit_service),
        )
        quota_enforcer = ProjectQuotaEnforcer(
            QuotaService(
                session_factory,
                config.quotas,
                source_ref_hasher=audit_keyring,
            )
        )
        occurrences = AutomationOccurrenceService(
            session_factory,
            max_concurrent_runs=config.scheduler.max_concurrent_runs,
        )
        service = ScheduledTaskService(
            occurrences=occurrences,
            dispatcher=AutomationDispatcher(
                session_factory,
                max_concurrent_runs=config.scheduler.max_concurrent_runs,
                quota=quota_enforcer,
                audit=audit_sink,
            ),
            reconciler=AutomationReconciler(session_factory),
            poll_interval_seconds=config.scheduler.poll_interval_seconds,
            max_concurrent_runs=config.scheduler.max_concurrent_runs,
            ownership=ownership,
        )
        await SchedulerApp(
            enabled=True,
            ownership=ownership,
            service=service,
        ).run(stop_event or asyncio.Event())
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
        loop.run_until_complete(run_scheduler(stop_event=stop_event))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
