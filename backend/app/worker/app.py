"""Command-line entry point for the independent M6 Worker role."""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Mapping
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from functools import partial

from app.automations.reconciliation import AutomationReconciler
from app.final_schema import FinalSchemaProbe
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.memory_source_admission import MemorySourceAdmissionService
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.quotas.system_policy import SystemQuotaPolicyReader
from app.reliability.execution import (
    PrivateRunJobHandler,
    PrivateRunJobTerminalPort,
    RunAgentPrivateExecutor,
)
from app.reliability.owner_refs import AuditHmacKeyring
from app.reliability.workers import WorkerRegistry
from app.system_runtime_settings.materializer import (
    SystemRuntimePolicyMaterializer,
)
from app.system_settings import SystemModelMaterializer
from app.worker.mcp_discovery import McpToolDiscoveryJobHandler
from app.worker.retention import RetentionPurgeJobHandler
from app.worker.service import JobHandler, WorkerService
from deerflow.config import get_app_config
from deerflow.config.database_config import (
    DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
)
from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.config.quota_config import QuotaConfig
from deerflow.logging_config import configure_logging
from deerflow.mcp.http_security import make_secure_mcp_http_client_factory
from deerflow.mcp_definition_policy import NetworkMcpEndpointPolicy
from deerflow.persistence import close_engine, get_session_factory, init_engine
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.runtime import make_store
from deerflow.runtime.checkpoint_mode import (
    freeze_checkpoint_channel_mode,
    freeze_checkpoint_snapshot_frequency,
)
from deerflow.runtime.checkpointer.async_provider import make_checkpointer
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.events.stream import PostgresStreamBridge

WORKER_VERSION = "m6"


class WorkerConfigurationUnavailable(RuntimeError):
    """Stable, secret-free startup failure for an unusable Worker config."""

    def __init__(self) -> None:
        super().__init__("Worker configuration is unavailable")


async def run_worker(
    *,
    handlers: dict[str, JobHandler] | None = None,
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
        raise WorkerConfigurationUnavailable() from None
    configure_logging(config)
    if not config.worker.enabled:
        return
    try:
        database = config.database
        mode = getattr(database, "checkpoint_channel_mode", "full")
        checkpoint_delta = getattr(database, "checkpoint_delta", None)
        snapshot_frequency = getattr(
            checkpoint_delta,
            "snapshot_frequency",
            DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
        )
        freeze_checkpoint_channel_mode(mode)
        freeze_checkpoint_snapshot_frequency(snapshot_frequency)
    except Exception:
        raise WorkerConfigurationUnavailable() from None
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
        from app.audit.sinks import OperationalAuditSink, TrustedOperationAuditSink

        audit_service = AuditService(session_factory, audit_keyring)
        worker_audit_context = _bind_worker_audit_process(audit_service)
        audit_sink = OperationalAuditSink(
            audit_service,
            process_context=worker_audit_context,
        )
        retention_audit_sink = TrustedOperationAuditSink(
            audit_service,
            process_context=worker_audit_context,
        )
        quota_config = getattr(config, "quotas", None) or QuotaConfig()
        quota_enforcer = ProjectQuotaEnforcer(
            QuotaService(
                session_factory,
                quota_config,
                source_ref_hasher=audit_keyring,
                current_policy_reader=SystemQuotaPolicyReader(),
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
            mcp_http_client_factory = make_secure_mcp_http_client_factory(
                proxy_url=mcp_security.egress_proxy_url,
                timeout_seconds=mcp_security.discovery_timeout_seconds,
            )
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
                model_materializer=SystemModelMaterializer(session_factory),
                runtime_policy_materializer=SystemRuntimePolicyMaterializer(
                    session_factory,
                ),
                bridge=bridge,
                project_checkpointer=project_checkpointer,
                store=store,
                event_store=DbRunEventStore(session_factory),
                quota=quota_enforcer,
                audit=audit_sink,
            )
            private_run_handler = PrivateRunJobHandler(
                session_factory,
                executor=executor,
                retry_initial_seconds=(config.worker.retry_initial_seconds),
                retry_max_seconds=config.worker.retry_max_seconds,
                job_repository_builder=repository_builder,
                project_checkpointer=project_checkpointer,
                endpoint_policy=mcp_endpoint_policy,
                quota=quota_enforcer,
                audit=audit_sink,
                memory_source_admission=MemorySourceAdmissionService(
                    source_hmac=audit_keyring.memory_source_ref,
                    job_repository_builder=repository_builder,
                ),
            )
            active_handlers = {
                "private_run": private_run_handler,
                "automation_run": private_run_handler,
                "retention_purge": RetentionPurgeJobHandler(
                    session_factory,
                    audit=retention_audit_sink,
                    quota=quota_enforcer,
                    job_repository_builder=repository_builder,
                ),
                "mcp_discovery": McpToolDiscoveryJobHandler(
                    session_factory,
                    endpoint_policy=mcp_endpoint_policy,
                    http_client_factory=mcp_http_client_factory,
                    discovery_timeout_seconds=(mcp_security.discovery_timeout_seconds),
                    job_repository_builder=repository_builder,
                ),
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
    except WorkerConfigurationUnavailable as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        loop.close()


if __name__ == "__main__":
    main()
