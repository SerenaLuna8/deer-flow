"""Command-line entry point for the independent M6 Worker role."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Mapping
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from functools import partial

from app.automations.reconciliation import AutomationReconciler
from app.final_schema import FinalSchemaProbe
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.run_skill_tree_orphan_reaper import (
    RunSkillTreeOrphanReaper,
)
from app.private_work.thread_service import PrivateThreadService
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
from app.worker.memory_dream import MemoryDreamJobHandler
from app.worker.memory_dream_prepare import MemoryDreamPrepareJobHandler
from app.worker.memory_seal import MemorySealJobHandler
from app.worker.retention import RetentionPurgeJobHandler
from app.worker.service import JobHandler, WorkerService
from deerflow.config import get_app_config
from deerflow.config.database_config import (
    DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
)
from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.config.paths import get_paths
from deerflow.config.quota_config import QuotaConfig
from deerflow.config.worker_config import require_supported_worker_release_topology
from deerflow.logging_config import configure_logging
from deerflow.mcp.http_security import make_secure_mcp_http_client_factory
from deerflow.mcp_definition_policy import NetworkMcpEndpointPolicy
from deerflow.persistence import close_engine, get_engine, get_session_factory, init_engine
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.runtime import make_store
from deerflow.runtime.checkpoint_mode import (
    freeze_checkpoint_channel_mode,
    freeze_checkpoint_snapshot_frequency,
)
from deerflow.runtime.checkpointer.async_provider import make_checkpointer
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.events.stream import PostgresStreamBridge
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.sandbox.security import resolve_host_bash_execution_mode
from deerflow.secrets import SecretKey
from deerflow.subagents.lifecycle import (
    SubagentTaskLifecycle,
    subagent_task_lifecycle,
)

WORKER_VERSION = "m6"
logger = logging.getLogger(__name__)


class WorkerConfigurationUnavailable(RuntimeError):
    """Stable, secret-free startup failure for an unusable Worker config."""

    def __init__(self) -> None:
        super().__init__("Worker configuration is unavailable")


def _handlers_for_run_mount_readiness(
    handlers: dict[str, JobHandler],
    *,
    ready: bool,
) -> dict[str, JobHandler]:
    """Remove Agent-graph claims when exact Skill mounts are unprovable."""

    if type(ready) is not bool:
        raise TypeError("run mount provider readiness must be boolean")
    if ready:
        return dict(handlers)
    return {job_type: handler for job_type, handler in handlers.items() if job_type not in {"private_run", "automation_run"}}


async def _run_service_until_subagents_close(
    service: WorkerService,
    stop_event: asyncio.Event,
    lifecycle: SubagentTaskLifecycle,
) -> None:
    """Keep Sub-Agent Tasks inside the Worker's resource lifetime."""

    try:
        await service.run(stop_event)
    finally:
        try:
            # This executes while run_worker still owns its AsyncExitStack, so
            # private owner-loop proxies become quiet before stores/checkpointers
            # and the database engine are released.
            await lifecycle.aclose()
        finally:
            # WorkerService keeps its operator-facing grace period bounded by
            # detaching cancellation-resistant handlers. Once child Tasks are
            # quiet, join those parent handlers through their real finally
            # blocks before the AsyncExitStack can release shared resources.
            await service.join_detached()


async def run_worker(
    *,
    handlers: dict[str, JobHandler] | None = None,
    stop_event: asyncio.Event | None = None,
    subagent_lifecycle: SubagentTaskLifecycle | None = None,
) -> None:
    try:
        SecretKey.from_environment()
        config = await asyncio.to_thread(get_app_config)
        require_supported_worker_release_topology(config.worker)
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
    host_execution_domain: HostExecutionDomainSnapshot | None = None
    if resolve_host_bash_execution_mode(config).value == "local_approval_required":
        try:
            host_execution_domain = await asyncio.to_thread(
                HostExecutionDomainSnapshot.capture,
                config,
            )
        except Exception:
            raise WorkerConfigurationUnavailable() from None
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
        engine = get_engine()
        if engine is None:
            raise WorkerConfigurationUnavailable()
        # Knowledge is startup-only: a missing/disabled settings row keeps
        # the feature module absent, so no Knowledge task loop runs. Project
        # cleanup remains independently composed because historical Knowledge
        # data can outlive a later feature-disable configuration change.
        from app.knowledge.composition import (
            create_knowledge_worker_resources_from_database,
        )

        knowledge_resources = await create_knowledge_worker_resources_from_database(app_config=config)
        knowledge_module = knowledge_resources.feature_module
        if knowledge_module is not None:
            stack.push_async_callback(knowledge_module.aclose)
        sandbox_provider = await asyncio.to_thread(get_sandbox_provider)
        try:
            run_mount_provider_ready = await asyncio.to_thread(
                sandbox_provider.run_readonly_mounts_ready,
            )
        except Exception:
            run_mount_provider_ready = False
        if type(run_mount_provider_ready) is not bool:
            run_mount_provider_ready = False
        logger.info(
            "Run Skill mount provider readiness ready=%s",
            run_mount_provider_ready,
        )
        run_skill_orphan_reaper = RunSkillTreeOrphanReaper(
            engine=engine,
            materialization_root=get_paths().run_skill_materialization_root(),
            provider=sandbox_provider,
            grace_seconds=(config.worker.materialization_orphan_grace_seconds),
        )
        orphan_report = await run_skill_orphan_reaper.reap_startup()
        logger.info(
            "Run Skill orphan startup reconciliation complete scanned=%d deleted=%d preserved_active=%d preserved_grace=%d preserved_lock=%d preserved_unknown=%d",
            orphan_report.scanned,
            orphan_report.deleted,
            orphan_report.preserved_active,
            orphan_report.preserved_grace,
            orphan_report.preserved_lock,
            orphan_report.preserved_unknown,
        )
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
        run_event_notify_enabled = config.worker.stream.run_event_notify_enabled
        run_event_store = DbRunEventStore(
            session_factory,
            run_event_notify_enabled=run_event_notify_enabled,
        )
        terminal_port = PrivateRunJobTerminalPort(
            quota=quota_enforcer,
            audit=audit_sink,
            event_store=run_event_store,
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
            bridge = PostgresStreamBridge(
                session_factory,
                run_event_notify_enabled=run_event_notify_enabled,
            )
            raw_checkpointer = await stack.enter_async_context(
                make_checkpointer(config),
            )
            store = await stack.enter_async_context(make_store(config))
            project_checkpointer = ProjectScopedCheckpointer(
                raw_checkpointer,
                session_factory,
                quota=quota_enforcer,
                run_event_store=run_event_store,
            )
            model_materializer = SystemModelMaterializer(session_factory)
            runtime_policy_materializer = SystemRuntimePolicyMaterializer(
                session_factory,
            )
            executor = RunAgentPrivateExecutor(
                session_factory,
                app_config=config,
                model_materializer=model_materializer,
                runtime_policy_materializer=runtime_policy_materializer,
                bridge=bridge,
                project_checkpointer=project_checkpointer,
                store=store,
                event_store=run_event_store,
                quota=quota_enforcer,
                audit=audit_sink,
                host_execution_domain=host_execution_domain,
                knowledge_module=knowledge_module,
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
                execution_approval_ttl_seconds=(config.sandbox.host_execution_approval.request_ttl_seconds),
            )
            memory_archive_barrier = ProjectChatControlService(
                session_factory,
                project_checkpointer,
                PrivateThreadService(
                    session_factory,
                    project_checkpointer,
                ),
                run_event_store,
                endpoint_policy=mcp_endpoint_policy,
                model_materializer=model_materializer,
            )
            active_handlers = {
                "private_run": private_run_handler,
                "automation_run": private_run_handler,
                "retention_purge": RetentionPurgeJobHandler(
                    session_factory,
                    audit=retention_audit_sink,
                    approval_audit=audit_sink,
                    quota=quota_enforcer,
                    job_repository_builder=repository_builder,
                    mount_owner_reconciler=run_skill_orphan_reaper,
                    retry_initial_seconds=(config.worker.retry_initial_seconds),
                    retry_max_seconds=config.worker.retry_max_seconds,
                    knowledge_purge=knowledge_resources.project_purge,
                ),
                "mcp_discovery": McpToolDiscoveryJobHandler(
                    session_factory,
                    endpoint_policy=mcp_endpoint_policy,
                    http_client_factory=mcp_http_client_factory,
                    discovery_timeout_seconds=(mcp_security.discovery_timeout_seconds),
                    job_repository_builder=repository_builder,
                ),
                "memory_dream": MemoryDreamJobHandler(
                    session_factory,
                    app_config=config,
                    model_materializer=model_materializer,
                    runtime_policy_materializer=runtime_policy_materializer,
                    job_repository_builder=repository_builder,
                    retry_initial_seconds=config.worker.retry_initial_seconds,
                    retry_max_seconds=config.worker.retry_max_seconds,
                    audit=audit_sink,
                ),
                "memory_dream_prepare": MemoryDreamPrepareJobHandler(
                    session_factory,
                    app_config=config,
                    barrier=memory_archive_barrier,
                    job_repository_builder=repository_builder,
                    retry_initial_seconds=config.worker.retry_initial_seconds,
                    retry_max_seconds=config.worker.retry_max_seconds,
                    audit=audit_sink,
                ),
                "memory_seal": MemorySealJobHandler(
                    session_factory,
                    app_config=config,
                    barrier=memory_archive_barrier,
                    job_repository_builder=repository_builder,
                    audit=audit_sink,
                ),
            }
        active_handlers = _handlers_for_run_mount_readiness(
            active_handlers,
            ready=run_mount_provider_ready,
        )
        registry = WorkerRegistry(session_factory, version=WORKER_VERSION)
        service = WorkerService(
            session_factory,
            registry,
            active_handlers,
            config.worker,
            repository_builder=repository_builder,
            after_claim_commit=reconcile_deferred_automation_terminals,
            execution_domain=host_execution_domain,
        )
        effective_stop_event = stop_event or asyncio.Event()
        effective_lifecycle = subagent_lifecycle or subagent_task_lifecycle
        if knowledge_module is None:
            await _run_service_until_subagents_close(
                service,
                effective_stop_event,
                effective_lifecycle,
            )
        else:
            from app.knowledge.worker import run_knowledge_task_worker, run_worker_loops

            await run_worker_loops(
                run_main=partial(
                    _run_service_until_subagents_close,
                    service,
                    effective_stop_event,
                    effective_lifecycle,
                ),
                run_knowledge=partial(
                    run_knowledge_task_worker,
                    knowledge_module,
                    effective_stop_event,
                ),
                stop_event=effective_stop_event,
            )


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
