"""Production private Run Agent execution adapter."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context_evidence_observer import (
    PrivateRunContextEvidenceObserver,
)
from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkMcpQuotaExceeded
from app.private_work.execution_approval_audit import (
    NoopHostExecutionApprovalAudit,
)
from app.private_work.file_finalizer import PrivateFileFinalizationAuditPort
from app.private_work.run_admission import AdmittedPrivateRun
from app.private_work.sandbox_files import CurrentUploadSnapshotStale
from app.reliability.jobs import (
    AdmittedJobRecord,
    automation_run_idempotency_key,
    private_run_idempotency_key,
)
from app.reliability.run_execution.boundary import PrivateRunExecutionBoundary
from app.reliability.run_execution.contracts import (
    AgentExecutionResult,
    PrivateRunExecution,
)
from app.reliability.run_execution.errors import (
    AmbiguousExternalSideEffect,
    PermanentExecutionError,
    TransientExecutionError,
)
from app.reliability.run_execution.outcome_mapping import (
    map_run_agent_outcome,
    outcome_usage_snapshot,
    output_limit_error,
    terminal_failure_result,
    usage_snapshot,
)
from app.reliability.run_execution.ports import (
    NoopPrivateRunAgentQuota as _NoopPrivateRunAgentQuota,
)
from app.reliability.run_execution.ports import (
    PrivateRunAgentQuotaPort,
    SystemModelMaterializationPort,
    SystemRuntimePolicyMaterializationPort,
)
from app.reliability.run_execution.preparation import (
    RunPreparationDependencies,
    bind_run_checkpointer,
    build_run_authorities,
    build_run_context,
    freeze_run_policy,
    graph_input,
    load_memory_archive_context,
    materialize_private_runtime,
    required_current_upload_snapshot,
    runner_config,
)
from app.reliability.run_execution.preparation import (
    _context_compaction_threshold_tokens as _context_compaction_threshold_tokens,
)
from app.reliability.run_execution.stream_authority import (
    LeaseAuthorizedStreamBridge,
)
from app.reliability.run_execution.vision_dispatch import (
    PrivateRunVisionDispatchAuthority,
)
from app.shared_assets.skill_builder_activity_stream import (
    SkillBuilderActivityEmitter,
    SkillBuilderActivityStreamBridge,
    SkillDesignActivityLimitExceeded,
)
from app.shared_assets.skill_builder_agent_runtime import (
    SkillBuilderAgentFactory,
    WorkerSkillBuilderAuthoringCatalog,
)
from app.shared_assets.skill_builder_draft_sink import SkillDesignDraftSink
from app.worker.service import JobLeaseAuthority
from deerflow.agents.memory.snip import SnipArchiveContext
from deerflow.config.app_config import (
    pop_current_app_config,
    push_current_app_config,
)
from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.error_codes import (
    PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
    ContextProviderCallAmbiguousError,
    MemoryAuthorityUnavailable,
    PublicRunError,
    PublicRunErrorCode,
)
from deerflow.mcp.http_security import make_secure_mcp_http_client_factory
from deerflow.mcp_definition_policy import NetworkMcpEndpointPolicy
from deerflow.models.factory import AgentModelSettingsUnsupported
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.runtime import (
    DisconnectMode,
    RunManager,
    RunRecord,
    run_agent,
)
from deerflow.runtime.checkpoint_mode import CheckpointModeMismatchError
from deerflow.runtime.events.models import (
    STREAM_TERMINAL_ERROR_CODES,
    stream_terminal_status_for_run_settlement,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot
from deerflow.runtime.runs.execution_contracts import (
    RunAgentOutcome,
    RunAgentResourceOwnership,
)
from deerflow.runtime.user_context import (
    reset_current_user,
    reset_runtime_storage_user_id,
    set_current_user,
    set_runtime_storage_user_id,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.token_budget_usage import TokenBudgetUsageRecorder
from deerflow.trace_context import normalize_trace_id, request_trace_context

logger = logging.getLogger("app.reliability.execution")


class RunAgentPrivateExecutor:
    """Production adapter that invokes ``run_agent`` only inside the Worker."""

    _usage_snapshot = staticmethod(usage_snapshot)
    _outcome_usage_snapshot = staticmethod(outcome_usage_snapshot)
    _terminal_failure_result = staticmethod(terminal_failure_result)
    _output_limit_error = staticmethod(output_limit_error)
    _required_current_upload_snapshot = staticmethod(required_current_upload_snapshot)
    _runner_config = staticmethod(runner_config)
    _graph_input = staticmethod(graph_input)

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        app_config: Any,
        bridge: Any,
        project_checkpointer: ProjectScopedCheckpointer,
        store: Any,
        event_store: Any,
        asset_runtime: PrivateAssetRuntime | None = None,
        model_materializer: SystemModelMaterializationPort | None = None,
        runtime_policy_materializer: SystemRuntimePolicyMaterializationPort | None = None,
        agent_factory: Any | None = None,
        runner=run_agent,
        quota: PrivateRunAgentQuotaPort | None = None,
        audit: PrivateFileFinalizationAuditPort | None = None,
        host_execution_domain: HostExecutionDomainSnapshot | None = None,
        skill_builder_activity_emitter_factory: Any = (SkillBuilderActivityEmitter.create),
        knowledge_module: Any | None = None,
    ) -> None:
        self._factory = session_factory
        self._app_config = app_config
        self._bridge = bridge
        self._project_checkpointer = project_checkpointer
        self._store = store
        self._event_store = event_store
        self._model_materializer = model_materializer
        self._runtime_policy_materializer = runtime_policy_materializer
        if asset_runtime is None:
            raw_mcp_security = getattr(app_config, "mcp_security", None)
            if isinstance(raw_mcp_security, McpSecurityConfig):
                mcp_security = raw_mcp_security
            elif isinstance(raw_mcp_security, Mapping):
                mcp_security = McpSecurityConfig.model_validate(raw_mcp_security)
            else:
                mcp_security = McpSecurityConfig()
            self._asset_runtime = PrivateAssetRuntime(
                session_factory,
                endpoint_policy=NetworkMcpEndpointPolicy(
                    mcp_security.project_remote_allowed_networks,
                ),
                http_client_factory=make_secure_mcp_http_client_factory(
                    proxy_url=mcp_security.egress_proxy_url,
                    timeout_seconds=max(
                        mcp_security.discovery_timeout_seconds,
                        mcp_security.tool_call_timeout_seconds,
                    ),
                ),
                discovery_timeout_seconds=mcp_security.discovery_timeout_seconds,
                tool_call_timeout_seconds=mcp_security.tool_call_timeout_seconds,
                run_session_reuse=mcp_security.run_session_reuse,
            )
        else:
            self._asset_runtime = asset_runtime
        self._agent_factory = agent_factory or self._default_agent_factory()
        self._runner = runner
        # Worker-lifecycle KnowledgeModule; present only when the feature is
        # enabled. Ordinary chat Runs get the project-bound knowledge_search
        # tool; Skill Builder Runs never do.
        self._knowledge_module = knowledge_module
        if not callable(skill_builder_activity_emitter_factory):
            raise TypeError("skill_builder_activity_emitter_factory must be callable")
        self._skill_builder_activity_emitter_factory = skill_builder_activity_emitter_factory
        self._quota = quota or _NoopPrivateRunAgentQuota()
        self._file_finalization_audit = audit
        if (
            host_execution_domain is not None
            and type(
                host_execution_domain,
            )
            is not HostExecutionDomainSnapshot
        ):
            raise TypeError(
                "host_execution_domain must be a HostExecutionDomainSnapshot",
            )
        self._host_execution_domain = host_execution_domain
        self._execution_approval_audit = (
            audit
            if callable(
                getattr(audit, "host_execution_approval_requested", None),
            )
            else NoopHostExecutionApprovalAudit()
        )
        self._preparation = RunPreparationDependencies(
            session_factory=session_factory,
            app_config=app_config,
            store=store,
            event_store=event_store,
            project_checkpointer=project_checkpointer,
            model_materializer=model_materializer,
            runtime_policy_materializer=runtime_policy_materializer,
            quota=self._quota,
            file_finalization_audit=audit,
            execution_approval_audit=self._execution_approval_audit,
            host_execution_domain=self._host_execution_domain,
        )

    @staticmethod
    def _default_agent_factory():
        from deerflow.agents.lead_agent.agent import make_lead_agent

        return make_lead_agent

    async def _memory_archive_context(
        self,
        execution: PrivateRunExecution,
        app_config: Any,
    ) -> SnipArchiveContext:
        """Thin compatibility seam over the preparation owner; tests patch this on the instance."""
        return await load_memory_archive_context(
            execution,
            app_config,
            session_factory=self._factory,
        )

    def _resolve_agent_factory(
        self,
        execution: PrivateRunExecution,
        claim: JobClaim,
        base_factory: Any,
    ) -> Any:
        """Skill Builder Runs use the authoring graph and never receive the
        knowledge tool; chat Runs gain it exactly when the module is enabled."""
        if execution.runtime_kind == "skill_builder":
            return SkillBuilderAgentFactory(
                catalog=WorkerSkillBuilderAuthoringCatalog(
                    self._factory,
                    execution.context,
                ),
                draft_sink=SkillDesignDraftSink(
                    self._factory,
                    execution.context,
                    claim,
                ),
            )
        if self._knowledge_module is None:
            return base_factory
        from app.knowledge.authority import PrivateWorkKnowledgeAuthority
        from app.knowledge.run_tool import create_knowledge_lead_agent_factory
        from app.projects.capabilities import Capability

        return create_knowledge_lead_agent_factory(
            module=self._knowledge_module,
            project_id=execution.context.project_id,
            owner_user_id=execution.context.user_id,
            authority=PrivateWorkKnowledgeAuthority(
                execution.context,
                Capability.SHARED_ASSETS_EXECUTE,
            ),
            base_factory=base_factory,
        )

    @staticmethod
    def _admitted(
        execution: PrivateRunExecution,
        claim: JobClaim,
    ) -> AdmittedPrivateRun:
        return AdmittedPrivateRun(
            run=execution.run,
            job=AdmittedJobRecord(
                job_id=claim.job_id,
                job_type=claim.job_type,
                project_id=execution.run.project_id,
                owner_user_id=execution.run.owner_user_id,
                run_id=execution.run.run_id,
                idempotency_key=(automation_run_idempotency_key(claim.occurrence_id) if claim.job_type == "automation_run" and claim.occurrence_id is not None else private_run_idempotency_key(execution.run.run_id)),
                status="running",
                origin_trace_id=execution.run.origin_trace_id,
                execution_domain_affinity=claim.execution_domain_affinity,
            ),
            snapshot=execution.snapshot,
            opaque_runtime_scope=execution.context.resource_scope,
        )

    async def execute(
        self,
        execution: PrivateRunExecution,
        authority: JobLeaseAuthority,
    ) -> AgentExecutionResult:
        run_trace_id = normalize_trace_id(execution.run.origin_trace_id)
        claim_trace_id = normalize_trace_id(authority.claim.origin_trace_id)
        if run_trace_id is None or claim_trace_id is None or run_trace_id != claim_trace_id:
            raise PermanentExecutionError("RUN_TRACE_MISMATCH")
        with request_trace_context(run_trace_id):
            return await self._execute_with_trace(execution, authority)

    async def _execute_with_trace(
        self,
        execution: PrivateRunExecution,
        authority: JobLeaseAuthority,
    ) -> AgentExecutionResult:
        claim = authority.claim
        boundary = PrivateRunExecutionBoundary(
            self._factory,
            context=execution.context,
            claim=claim,
            expected_worker_id=authority.expected_worker_id,
            quota=self._quota,
            runtime_kind=execution.runtime_kind,
        )
        admitted = self._admitted(execution, claim)
        private_runtime = None
        file_authority = None
        record: RunRecord | None = None
        context_evidence_observer: PrivateRunContextEvidenceObserver | None = None
        resource_ownership = RunAgentResourceOwnership()
        runtime_config_pushed = False
        token_budget_usage_recorder: TokenBudgetUsageRecorder | None = None
        try:
            policy = await freeze_run_policy(execution, self._preparation)
            token_budget_usage_recorder = policy.token_budget_usage_recorder
            archive_context = await self._memory_archive_context(
                execution,
                policy.runtime_app_config,
            )
            push_current_app_config(policy.runtime_app_config)
            runtime_config_pushed = True
            private_runtime = await materialize_private_runtime(
                execution,
                admitted,
                policy,
                asset_runtime=self._asset_runtime,
                boundary=boundary,
            )
            authorities = build_run_authorities(
                execution,
                policy,
                private_runtime,
                claim=claim,
                boundary=boundary,
                deps=self._preparation,
            )
            file_authority = authorities.file_authority
            run_manager = RunManager()
            record = await run_manager.register_persisted(
                run_id=execution.run.run_id,
                thread_id=execution.run.thread_id,
                assistant_id=execution.run.assistant_id,
                on_disconnect=DisconnectMode.continue_,
                metadata=execution.run.metadata,
                kwargs=execution.run.kwargs,
                multitask_strategy=execution.run.multitask_strategy,
                model_name=policy.exact_model_name,
                scope=execution.context.resource_scope,
                created_at=execution.run.created_at.isoformat(),
            )
            boundary.bind_abort_event(record.abort_event)
            authority.bind_cancel_callback(boundary.request_local_cancel)
            if authority.cancel_requested:
                boundary.request_local_cancel()
            vision_dispatch_authority = (
                PrivateRunVisionDispatchAuthority(
                    boundary=boundary,
                )
                if policy.vision_model is not None
                else None
            )
            bound_checkpointer = bind_run_checkpointer(
                execution,
                policy,
                boundary=boundary,
                deps=self._preparation,
            )
            context_evidence_observer = bound_checkpointer.context_evidence_observer
            run_context = build_run_context(
                execution,
                policy,
                claim=claim,
                boundary=boundary,
                checkpointer=bound_checkpointer.checkpointer,
                context_evidence_observer=context_evidence_observer,
                file_authority=file_authority,
                memory_archive_context=archive_context,
                private_runtime=private_runtime,
                host_execution_approval_port=authorities.host_execution_approval_port,
                vision_dispatch_authority=vision_dispatch_authority,
                resource_ownership=resource_ownership,
                deps=self._preparation,
            )
            owner_token = set_current_user(
                SimpleNamespace(id=execution.run.owner_user_id),
            )
            storage_token = set_runtime_storage_user_id(
                execution.run.owner_user_id,
            )
            try:
                agent_factory = self._agent_factory
                stream_bridge: Any = LeaseAuthorizedStreamBridge(
                    self._bridge,
                    boundary,
                    scope=execution.context.resource_scope,
                    thread_id=execution.run.thread_id,
                    terminal_status=lambda: stream_terminal_status_for_run_settlement(
                        record.status,
                    ),
                    terminal_error_code=lambda: record.error if record.error in STREAM_TERMINAL_ERROR_CODES else None,
                    terminal_authority=lambda: record.terminal_authority,
                )
                if execution.runtime_kind == "skill_builder":
                    activity_emitter = await self._skill_builder_activity_emitter_factory(
                        self._factory,
                        execution.context,
                        claim,
                    )
                    stream_bridge = SkillBuilderActivityStreamBridge(
                        stream_bridge,
                        activity_emitter,
                    )
                agent_factory = self._resolve_agent_factory(
                    execution,
                    claim,
                    agent_factory,
                )
                outcome = await self._runner(
                    stream_bridge,
                    run_manager,
                    record,
                    ctx=run_context,
                    agent_factory=agent_factory,
                    graph_input=graph_input(execution),
                    config=runner_config(execution, archive_context),
                    stream_modes=list(execution.stream_mode),
                    stream_subgraphs=execution.stream_subgraphs,
                    interrupt_before=execution.interrupt_before,
                    interrupt_after=execution.interrupt_after,
                )
            finally:
                reset_runtime_storage_user_id(storage_token)
                reset_current_user(owner_token)

            if boundary.lease_lost:
                raise TransientExecutionError(
                    "EXECUTION_AUTHORITY_UNAVAILABLE",
                )
            if type(outcome) is not RunAgentOutcome:
                raise TypeError("Run Agent runner returned an invalid outcome")
            attempt_usage = outcome_usage_snapshot(
                outcome.usage,
                token_budget_usage_recorder,
            )
            if context_evidence_observer is not None and not boundary.authorization_revoked:
                await context_evidence_observer.record_settled()
            return map_run_agent_outcome(
                outcome,
                attempt_usage=attempt_usage,
                authorization_revoked=boundary.authorization_revoked,
                cancel_requested=boundary.cancel_requested,
                ambiguous_side_effect=boundary.ambiguous_side_effect,
            )
        except asyncio.CancelledError:
            raise
        except ContextProviderCallAmbiguousError as error:
            if boundary.lease_lost:
                raise TransientExecutionError(
                    "EXECUTION_AUTHORITY_UNAVAILABLE",
                ) from error
            if boundary.authorization_revoked:
                return AgentExecutionResult.cancelled(
                    attempt_usage=(
                        usage_snapshot(
                            record,
                            token_budget_usage_recorder,
                        )
                        if record is not None
                        else None
                    ),
                )
            if context_evidence_observer is not None:
                try:
                    await context_evidence_observer.record_settled()
                except asyncio.CancelledError:
                    raise
                except AuthorizationRevoked:
                    if boundary.lease_lost:
                        raise TransientExecutionError(
                            "EXECUTION_AUTHORITY_UNAVAILABLE",
                        ) from error
                    return AgentExecutionResult.cancelled(
                        attempt_usage=(
                            usage_snapshot(
                                record,
                                token_budget_usage_recorder,
                            )
                            if record is not None
                            else None
                        ),
                    )
                except Exception as settlement_error:
                    raise TransientExecutionError(
                        PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                        attempt_usage=(
                            usage_snapshot(
                                record,
                                token_budget_usage_recorder,
                            )
                            if record is not None
                            else None
                        ),
                    ) from settlement_error
            if record is None:
                raise RuntimeError(
                    "Context Provider ambiguity has no registered Run",
                ) from error
            return terminal_failure_result(
                PublicRunErrorCode.CONTEXT_PROVIDER_CALL_AMBIGUOUS.value,
                attempt_usage=usage_snapshot(
                    record,
                    token_budget_usage_recorder,
                ),
            )
        except CheckpointModeMismatchError as error:
            raise PermanentExecutionError(
                "CHECKPOINT_MODE_MISMATCH",
            ) from error
        except PrivateWorkAssetStale:
            raise PermanentExecutionError("RUN_ASSET_STALE") from None
        except CurrentUploadSnapshotStale:
            raise PermanentExecutionError(
                "RUN_CURRENT_UPLOAD_STALE",
            ) from None
        except AgentModelSettingsUnsupported:
            raise PermanentExecutionError(
                "RUN_EXECUTION_PROFILE_UNSUPPORTED",
            ) from None
        except SkillDesignActivityLimitExceeded:
            raise PermanentExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        except TransientExecutionError as error:
            if error.attempt_usage is None and record is not None and not boundary.lease_lost:
                raise TransientExecutionError(
                    error.public_error_code,
                    attempt_usage=usage_snapshot(
                        record,
                        token_budget_usage_recorder,
                    ),
                ) from error
            raise
        except PermanentExecutionError:
            # Deterministic admitted-snapshot drift must reach the Job handler
            # unchanged so it settles the Run dead instead of scheduling retry.
            raise
        except AmbiguousExternalSideEffect:
            raise
        except PrivateWorkMcpQuotaExceeded as error:
            raise TransientExecutionError(
                error.code,
                attempt_usage=(usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        except MemoryAuthorityUnavailable:
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        except PublicRunError as error:
            if error.code is PublicRunErrorCode.MODEL_OUTPUT_LIMIT:
                raise output_limit_error(
                    record,
                    lease_lost=boundary.lease_lost,
                    recorder=token_budget_usage_recorder,
                ) from error
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        except AuthorizationRevoked:
            if boundary.lease_lost:
                raise TransientExecutionError(
                    "EXECUTION_AUTHORITY_UNAVAILABLE",
                ) from None
            return AgentExecutionResult.cancelled(
                attempt_usage=(usage_snapshot(record, token_budget_usage_recorder) if record is not None else None),
            )
        except Exception:
            if boundary.ambiguous_side_effect:
                raise AmbiguousExternalSideEffect(
                    attempt_usage=(usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
                ) from None
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        finally:
            if runtime_config_pushed:
                pop_current_app_config()
            mount_outcome = None
            if not resource_ownership.transferred and file_authority is not None:
                try:
                    mount_outcome = await file_authority.release()
                except Exception:
                    logger.warning(
                        "Failed to release private file authority for Run %s",
                        execution.run.run_id,
                        exc_info=True,
                    )
            if not resource_ownership.transferred and private_runtime is not None:
                try:
                    if mount_outcome is None:
                        await private_runtime.aclose()
                    else:
                        await private_runtime.aclose(mount_outcome)
                except Exception:
                    logger.warning(
                        "Failed to clean private runtime for Run %s",
                        execution.run.run_id,
                        exc_info=True,
                    )


__all__ = ["RunAgentPrivateExecutor"]
