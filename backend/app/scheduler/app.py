"""Command-line entry point for the independent M6 Scheduler role."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automations.dispatcher import AutomationDispatcher
from app.automations.errors import AutomationError
from app.automations.occurrences import AutomationOccurrenceService
from app.automations.ownership import AutomationSchedulerOwnership
from app.automations.reconciliation import AutomationReconciler
from app.final_schema import FinalSchemaProbe
from app.private_work.memory_dream_service import MemoryDreamSchedulerService
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.quotas.system_policy import SystemQuotaPolicyReader
from app.reliability.owner_refs import AuditHmacKeyring
from app.scheduler.service import AutomationSchedulerService
from app.system_runtime_settings import SystemRuntimePolicyService
from app.system_settings import SystemModelCatalogService
from deerflow.config import get_app_config
from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.mcp_definition_policy import NetworkMcpEndpointPolicy
from deerflow.persistence import (
    close_engine,
    get_engine,
    get_session_factory,
    init_engine,
)

logger = logging.getLogger(__name__)


class SchedulerConfigurationUnavailable(RuntimeError):
    """Stable, secret-free startup failure for an unusable Scheduler config."""

    def __init__(self) -> None:
        super().__init__("Scheduler configuration is unavailable")


@dataclass(slots=True)
class SchedulerApp:
    """Own the scheduler lock and polling service for one process lifetime."""

    enabled: bool
    ownership: AutomationSchedulerOwnership
    service: AutomationSchedulerService
    session_factory: async_sessionmaker[AsyncSession]
    poll_interval_seconds: float
    dream_service: MemoryDreamSchedulerService | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            return
        async with self.ownership.hold():
            async with self.session_factory() as session, session.begin():
                await self.service.reconcile_admitted_runs(session)
            while not stop_event.is_set():
                now = datetime.now(UTC)
                try:
                    async with self.session_factory() as session, session.begin():
                        await self.service.admit_due_occurrences(
                            session,
                            now=now,
                        )
                except asyncio.CancelledError:
                    raise
                except AutomationError as error:
                    if self.ownership.is_lost:
                        logger.error(
                            "Automation scheduler ownership lost; polling stopped: code=%s",
                            error.code,
                        )
                        return
                    logger.error(
                        "Automation scheduler poll failed: code=%s",
                        error.code,
                    )
                except Exception as error:  # noqa: BLE001 - keep polling after isolated faults
                    logger.error(
                        "Automation scheduler poll failed: error_type=%s",
                        type(error).__name__,
                    )
                if self.dream_service is not None:
                    try:
                        await self.ownership.verify()
                        await self.dream_service.admit_due(now=now)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:  # noqa: BLE001 - isolated poll
                        logger.error(
                            "Memory Dream scheduler poll failed: error_type=%s",
                            type(error).__name__,
                        )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.poll_interval_seconds,
                    )
                except TimeoutError:
                    pass


async def run_scheduler(
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    try:
        config = await asyncio.to_thread(get_app_config)
        raw_mcp_security = getattr(config, "mcp_security", None)
        if isinstance(raw_mcp_security, McpSecurityConfig):
            mcp_security = raw_mcp_security
        elif isinstance(raw_mcp_security, Mapping):
            mcp_security = McpSecurityConfig.model_validate(
                raw_mcp_security,
            )
        else:
            mcp_security = McpSecurityConfig()
        mcp_endpoint_policy = NetworkMcpEndpointPolicy(
            mcp_security.project_remote_allowed_networks,
        )
    except Exception:
        raise SchedulerConfigurationUnavailable() from None
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
        runtime_policy_service = SystemRuntimePolicyService(
            session_factory,
            audit_service,
        )
        audit_sink = OperationalAuditSink(
            audit_service,
            process_context=_bind_scheduler_audit_process(audit_service),
        )
        quota_enforcer = ProjectQuotaEnforcer(
            QuotaService(
                session_factory,
                config.quotas,
                source_ref_hasher=audit_keyring,
                current_policy_reader=SystemQuotaPolicyReader(),
            )
        )
        occurrences = AutomationOccurrenceService(
            session_factory,
            max_concurrent_runs=config.scheduler.max_concurrent_runs,
        )
        service = AutomationSchedulerService(
            occurrences=occurrences,
            dispatcher=AutomationDispatcher(
                session_factory,
                max_concurrent_runs=config.scheduler.max_concurrent_runs,
                model_catalog=SystemModelCatalogService(session_factory),
                runtime_policy=runtime_policy_service,
                endpoint_policy=mcp_endpoint_policy,
                quota=quota_enforcer,
                audit=audit_sink,
            ),
            reconciler=AutomationReconciler(session_factory),
            max_concurrent_runs=config.scheduler.max_concurrent_runs,
            ownership=ownership,
        )
        await SchedulerApp(
            enabled=True,
            ownership=ownership,
            service=service,
            session_factory=session_factory,
            poll_interval_seconds=config.scheduler.poll_interval_seconds,
            dream_service=MemoryDreamSchedulerService(session_factory),
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
    except SchedulerConfigurationUnavailable as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        loop.close()


if __name__ == "__main__":
    main()
