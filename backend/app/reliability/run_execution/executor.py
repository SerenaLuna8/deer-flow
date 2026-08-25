"""Production private Run Agent execution adapter."""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import BaseMessage, RemoveMessage
from langchain_core.messages.utils import convert_to_messages
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkMcpQuotaExceeded
from app.private_work.execution_approval import (
    HostExecutionProviderPolicySnapshot,
    WorkerHostExecutionApprovalPort,
)
from app.private_work.execution_approval_audit import (
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_profile import (
    RUN_EXECUTION_PROFILE_KWARG,
    RunExecutionProfileUnsupported,
    effective_run_execution_profile_from_kwargs,
)
from app.private_work.file_finalizer import (
    PrivateFileFinalizationAuditPort,
    PrivateFileFinalizer,
)
from app.private_work.memory_authority import (
    DEFAULT_PRIVATE_MEMORY_NAMESPACE,
    PrivateRunMemoryAuthority,
)
from app.private_work.run_admission import (
    AdmittedPrivateRun,
    _strip_client_memory_archive_receipt,
)
from app.private_work.run_repository import PrivateRunUsageSnapshot
from app.private_work.sandbox_files import (
    CurrentUploadSnapshotEntry,
    CurrentUploadSnapshotInvalid,
    CurrentUploadSnapshotStale,
    PrivateFileRunScope,
    PrivateRunFileAuthority,
    PrivateSandboxFileProjection,
    required_current_upload_snapshot_from_run_kwargs,
)
from app.private_work.snapshot_repository import agent_model_snapshot_purpose
from app.private_work.thread_repository import PrivateThreadRepository
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
from app.reliability.run_execution.ports import (
    NoopPrivateRunAgentQuota as _NoopPrivateRunAgentQuota,
)
from app.reliability.run_execution.ports import (
    PrivateRunAgentQuotaPort,
    SystemModelMaterializationPort,
    SystemRuntimePolicyMaterializationPort,
)
from app.reliability.run_execution.projections import (
    private_guardrail_attribution as _private_guardrail_attribution,
)
from app.reliability.run_execution.stream_authority import (
    LeaseAuthorizedRunEventStore,
    LeaseAuthorizedStreamBridge,
)
from app.reliability.run_execution.tool_call_control_policy import (
    ResolvedRunToolCallControlPolicy,
    resolve_run_tool_call_control_policy,
)
from app.reliability.run_execution.vision_dispatch import (
    PrivateRunVisionDispatchAuthority,
)
from app.shared_assets.models import AssetKind
from app.shared_assets.skill_builder_activity_stream import (
    SkillBuilderActivityEmitter,
    SkillBuilderActivityStreamBridge,
    SkillDesignActivityLimitExceeded,
)
from app.shared_assets.skill_builder_agent_runtime import (
    SkillBuilderAgentFactory,
    WorkerSkillBuilderAuthoringCatalog,
)
from app.shared_assets.skill_design_service import SkillDesignService
from app.system_runtime_settings.models import auxiliary_model_snapshot_ref
from app.system_settings.execution_payload import model_execution_provenance
from app.system_settings.model_refs import resolve_model_ref
from app.worker.service import JobLeaseAuthority
from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_CONTEXT_KEY,
    SnipArchiveContext,
)
from deerflow.config.app_config import (
    pop_current_app_config,
    push_current_app_config,
)
from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.config.model_config import ModelConfig
from deerflow.error_codes import (
    PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
    MemoryAuthorityUnavailable,
    PublicRunError,
    PublicRunErrorCode,
)
from deerflow.mcp.http_security import make_secure_mcp_http_client_factory
from deerflow.mcp_definition_policy import NetworkMcpEndpointPolicy
from deerflow.models.factory import AgentModelSettingsUnsupported
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.private_work.memory_document_repository import (
    DEFAULT_MEMORY_NAMESPACE,
)
from deerflow.runtime import (
    DisconnectMode,
    RunContext,
    RunManager,
    RunRecord,
    run_agent,
)
from deerflow.runtime.checkpoint_mode import CheckpointModeMismatchError
from deerflow.runtime.events.models import STREAM_TERMINAL_ERROR_CODES
from deerflow.runtime.host_execution_approval import (
    HOST_EXECUTION_MAX_CHANNEL_USER_ID_LENGTH,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.runs.execution_contracts import (
    RunAgentOutcome,
    RunAgentResourceOwnership,
    RunAgentUsageSnapshot,
)
from deerflow.runtime.user_context import (
    reset_current_user,
    reset_runtime_storage_user_id,
    set_current_user,
    set_runtime_storage_user_id,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.token_budget_usage import (
    TokenBudgetUsageRecorder,
    TokenBudgetUsageSnapshot,
)
from deerflow.trace_context import normalize_trace_id, request_trace_context

logger = logging.getLogger("app.reliability.execution")


def _persisted_channel_user_id(
    kwargs: Mapping[str, object],
) -> str | None:
    """Read the Gateway-persisted, server-owned channel identity."""

    config = kwargs.get("config")
    if not isinstance(config, Mapping):
        return None
    context = config.get("context")
    if not isinstance(context, Mapping):
        return None
    value = context.get("channel_user_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > HOST_EXECUTION_MAX_CHANNEL_USER_ID_LENGTH:
        raise ValueError("persisted channel identity is invalid")
    return value


class _PrivateRunThreadMetadataStore:
    """Persist the first completed title inside the exact private scope."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        scope: PrivateResourceScope,
        boundary: PrivateRunExecutionBoundary,
    ) -> None:
        self._factory = session_factory
        self._scope = scope
        self._boundary = boundary

    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
    ) -> None:
        await self._boundary.before_checkpoint_write()
        async with self._factory() as session, session.begin():
            await PrivateThreadRepository(
                session,
            ).set_automatic_display_name(
                scope=self._scope,
                thread_id=thread_id,
                display_name=display_name,
            )

    async def update_status(self, thread_id: str, status: str) -> None:
        # Private Run admission/settlement owns status. The harness invokes this
        # compatibility hook after title sync, so it intentionally stays a no-op.
        del thread_id, status


class RunAgentPrivateExecutor:
    """Production adapter that invokes ``run_agent`` only inside the Worker."""

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

    @staticmethod
    def _default_agent_factory():
        from deerflow.agents.lead_agent.agent import make_lead_agent

        return make_lead_agent

    @staticmethod
    def _usage_snapshot(
        record: RunRecord,
        recorder: TokenBudgetUsageRecorder | None = None,
    ) -> PrivateRunUsageSnapshot:
        return PrivateRunUsageSnapshot(
            total_input_tokens=record.total_input_tokens,
            total_output_tokens=record.total_output_tokens,
            total_tokens=record.total_tokens,
            llm_call_count=record.llm_call_count,
            lead_agent_tokens=record.lead_agent_tokens,
            subagent_tokens=record.subagent_tokens,
            middleware_tokens=record.middleware_tokens,
            token_usage_by_model=record.token_usage_by_model,
            token_budget_usage=(recorder.snapshot() if recorder is not None else None),
        )

    @staticmethod
    def _outcome_usage_snapshot(
        usage: RunAgentUsageSnapshot,
        recorder: TokenBudgetUsageRecorder | None = None,
    ) -> PrivateRunUsageSnapshot:
        return PrivateRunUsageSnapshot(
            total_input_tokens=usage.total_input_tokens,
            total_output_tokens=usage.total_output_tokens,
            total_tokens=usage.total_tokens,
            llm_call_count=usage.llm_call_count,
            lead_agent_tokens=usage.lead_agent_tokens,
            subagent_tokens=usage.subagent_tokens,
            middleware_tokens=usage.middleware_tokens,
            token_usage_by_model={model_name: dict(counters) for model_name, counters in usage.token_usage_by_model.items()},
            token_budget_usage=(usage.token_budget_usage if usage.token_budget_usage is not None else (recorder.snapshot() if recorder is not None else None)),
        )

    @staticmethod
    def _terminal_failure_result(
        public_error_code: str,
        *,
        attempt_usage: PrivateRunUsageSnapshot,
    ) -> AgentExecutionResult:
        return AgentExecutionResult.failed(
            public_error_code,
            retryable=False,
            attempt_usage=attempt_usage,
        )

    @classmethod
    def _output_limit_error(
        cls,
        record: RunRecord | None,
        *,
        lease_lost: bool,
        recorder: TokenBudgetUsageRecorder | None = None,
    ) -> PermanentExecutionError:
        return PermanentExecutionError(
            PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value,
            attempt_usage=(cls._usage_snapshot(record, recorder) if record is not None and not lease_lost else None),
        )

    async def _memory_archive_context(
        self,
        execution: PrivateRunExecution,
        app_config: Any,
    ) -> SnipArchiveContext:
        if execution.runtime_kind == "skill_builder":
            return SnipArchiveContext(
                enabled=False,
                project_id=execution.context.project_id,
                owner_user_id=str(execution.context.user_id),
                namespace=DEFAULT_MEMORY_NAMESPACE,
                preference_version=1,
                summary_model=None,
            )
        try:
            async with self._factory() as session, session.begin():
                preference = await AccountPersonalizationRepository(
                    session,
                ).read_memory(execution.context.user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TransientExecutionError(
                "EXECUTION_AUTHORITY_UNAVAILABLE",
            ) from None

        enabled = bool(app_config.memory.enabled and preference.memory_enabled)
        summary_model = None
        if enabled:
            model_name = app_config.summarization.model_name
            if model_name is None:
                models = getattr(app_config, "models", None)
                if not isinstance(models, list) or not models:
                    raise PermanentExecutionError("RUN_ASSET_STALE")
                model_name = models[0].name
            model = app_config.get_model_config(model_name)
            try:
                summary_model = model_execution_provenance(model)
            except ValueError:
                raise PermanentExecutionError("RUN_ASSET_STALE")
        return SnipArchiveContext(
            enabled=enabled,
            project_id=execution.context.project_id,
            owner_user_id=str(execution.context.user_id),
            namespace=DEFAULT_MEMORY_NAMESPACE,
            preference_version=preference.version,
            summary_model=summary_model,
        )

    @staticmethod
    def _required_current_upload_snapshot(
        run_kwargs: object,
    ) -> tuple[CurrentUploadSnapshotEntry, ...]:
        try:
            return required_current_upload_snapshot_from_run_kwargs(run_kwargs)
        except CurrentUploadSnapshotInvalid:
            raise PermanentExecutionError(
                "RUN_CURRENT_UPLOAD_STALE",
            ) from None

    @staticmethod
    def _runner_config(
        execution: PrivateRunExecution,
        archive_context: SnipArchiveContext,
    ) -> dict[str, Any]:
        config = copy.deepcopy(execution.config)
        raw_profile_present = RUN_EXECUTION_PROFILE_KWARG in execution.run.kwargs
        try:
            effective_profile = effective_run_execution_profile_from_kwargs(
                execution.run.kwargs,
            )
        except RunExecutionProfileUnsupported:
            raise PermanentExecutionError(
                "RUN_EXECUTION_PROFILE_STALE",
            ) from None
        if raw_profile_present and effective_profile is None:
            raise PermanentExecutionError("RUN_EXECUTION_PROFILE_STALE")
        raw_context = config.get("context")
        runtime_context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
        runtime_context[MEMORY_ARCHIVE_CONTEXT_KEY] = archive_context
        if getattr(execution, "runtime_kind", "chat") == "skill_builder":
            # Skill Builder has no host-execution approval UI or dedicated
            # continuation admission path. Mark its individual Agent turn as
            # non-interactive so Local approval-required bash is omitted and
            # delegated bash remains fail-closed. Isolated providers retain
            # their normal bash tools because they do not require approval.
            runtime_context["non_interactive"] = True
        raw_configurable = config.get("configurable")
        configurable = dict(raw_configurable) if isinstance(raw_configurable, Mapping) else {}
        if effective_profile is not None:
            persisted_model_name = getattr(execution.run, "model_name", None)
            if persisted_model_name is not None and persisted_model_name != effective_profile.model_name:
                raise PermanentExecutionError(
                    "RUN_EXECUTION_PROFILE_STALE",
                )
            for key, value in (
                ("thinking_enabled", effective_profile.thinking_enabled),
                ("reasoning_effort", effective_profile.reasoning_effort),
            ):
                runtime_context[key] = value
                configurable[key] = value
        config["context"] = runtime_context
        config["configurable"] = configurable
        return config

    @staticmethod
    def _graph_input(execution: PrivateRunExecution) -> object:
        if execution.resume_from_checkpoint:
            return None
        if execution.command is not None:
            if not isinstance(execution.command, Mapping):
                raise TransientExecutionError("INVALID_RUN_PAYLOAD")
            try:
                clean_command = _strip_client_memory_archive_receipt(
                    execution.command,
                    command=True,
                )
                if not isinstance(clean_command, Mapping):
                    raise TypeError
                return Command(**dict(clean_command))
            except (TypeError, ValueError):
                raise TransientExecutionError("INVALID_RUN_PAYLOAD") from None
        if not isinstance(execution.graph_input, Mapping):
            if execution.graph_input is None:
                return {}
            raise TransientExecutionError("INVALID_RUN_PAYLOAD")
        graph_input = _strip_client_memory_archive_receipt(
            execution.graph_input,
            command=False,
        )
        if not isinstance(graph_input, dict):
            raise TransientExecutionError("INVALID_RUN_PAYLOAD")
        messages = graph_input.get("messages")
        if isinstance(messages, list):
            converted: list[object] = []
            for message in messages:
                if isinstance(message, BaseMessage):
                    converted.append(message)
                    continue
                if not isinstance(message, Mapping):
                    converted.append(message)
                    continue
                if message.get("type") == "remove":
                    message_id = message.get("id")
                    if set(message) != {"type", "id"} or not isinstance(message_id, str) or not message_id:
                        raise TransientExecutionError("INVALID_RUN_PAYLOAD")
                    converted.append(RemoveMessage(id=message_id))
                    continue
                try:
                    converted.extend(convert_to_messages([dict(message)]))
                except (TypeError, ValueError, NotImplementedError):
                    raise TransientExecutionError("INVALID_RUN_PAYLOAD") from None
            graph_input["messages"] = converted
        return graph_input

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
        resource_ownership = RunAgentResourceOwnership()
        runtime_config_pushed = False
        token_budget_usage_recorder: TokenBudgetUsageRecorder | None = None
        try:
            current_upload_snapshot = self._required_current_upload_snapshot(
                execution.run.kwargs,
            )
            exact_model_name = execution.run.model_name
            if exact_model_name is None:
                raise PermanentExecutionError("RUN_ASSET_STALE")
            runtime_app_config = self._app_config
            runtime_policy = None
            tool_call_control_policy: ResolvedRunToolCallControlPolicy | None = None
            vision_model: ModelConfig | None = None
            delegate_model_names: dict[uuid.UUID, str] = {}
            if self._runtime_policy_materializer is not None:
                try:
                    materialized_runtime_policy = await self._runtime_policy_materializer.materialize_run_snapshot_envelope(
                        project_id=execution.context.project_id,
                        owner_user_id=str(execution.context.user_id),
                        run_id=execution.run.run_id,
                    )
                    runtime_policy = materialized_runtime_policy.value
                    tool_call_control_policy = resolve_run_tool_call_control_policy(
                        materialized_runtime_policy,
                        execution.run.kwargs,
                    )
                    runtime_app_config = self._app_config.with_runtime_policy(
                        tool_call_control_policy.app_config_policy,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise PermanentExecutionError(
                        "RUN_POLICY_STALE",
                    ) from None
            if self._model_materializer is not None:
                title_bound_name: str | None = None
                try:
                    lead_model = await self._model_materializer.materialize_snapshot(
                        project_id=execution.context.project_id,
                        owner_user_id=str(execution.context.user_id),
                        run_id=execution.run.run_id,
                        purpose="lead",
                    )
                    runtime_models: dict[str, ModelConfig] = {
                        lead_model.name: lead_model,
                    }
                    delegated_agent_definitions: set[uuid.UUID] = set()
                    for asset in execution.snapshot.assets:
                        if asset.kind is not AssetKind.AGENT or asset.dependency_order == 0:
                            continue
                        if asset.version_id in delegated_agent_definitions:
                            raise PermanentExecutionError("RUN_ASSET_STALE")
                        delegated_agent_definitions.add(asset.version_id)
                        delegated_model = await self._model_materializer.materialize_snapshot(
                            project_id=execution.context.project_id,
                            owner_user_id=str(execution.context.user_id),
                            run_id=execution.run.run_id,
                            purpose=agent_model_snapshot_purpose(
                                asset.version_id,
                            ),
                        )
                        existing = runtime_models.get(delegated_model.name)
                        if existing is not None and existing != delegated_model:
                            raise PermanentExecutionError("RUN_ASSET_STALE")
                        runtime_models[delegated_model.name] = delegated_model
                        delegate_model_names[asset.version_id] = delegated_model.name
                    if runtime_policy is not None:
                        auxiliary_model_refs: list[tuple[str, str | None]] = [
                            ("title", runtime_app_config.title.model_name),
                            (
                                "summarization",
                                runtime_app_config.summarization.model_name,
                            ),
                            ("memory", runtime_app_config.memory.model_name),
                        ]
                        if execution.runtime_kind in {"chat", "skill_builder"} and not lead_model.supports_vision:
                            auxiliary_model_refs.append(
                                (
                                    "vision",
                                    runtime_app_config.vision_bridge.model_name,
                                )
                            )
                        for purpose, model_ref in auxiliary_model_refs:
                            snapshot_ref = auxiliary_model_snapshot_ref(
                                purpose,
                                model_ref,
                                title_enabled=runtime_app_config.title.enabled,
                            )
                            if snapshot_ref is None:
                                continue
                            try:
                                auxiliary_model = await self._model_materializer.materialize_snapshot(
                                    project_id=execution.context.project_id,
                                    owner_user_id=str(execution.context.user_id),
                                    run_id=execution.run.run_id,
                                    purpose=purpose,
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                if purpose == "title" and model_ref is None:
                                    continue
                                raise PermanentExecutionError(
                                    "RUN_ASSET_STALE",
                                ) from None
                            if model_ref is not None and auxiliary_model.name != model_ref:
                                raise PermanentExecutionError(
                                    "RUN_ASSET_STALE",
                                )
                            if purpose == "vision" and not auxiliary_model.supports_vision:
                                raise PermanentExecutionError(
                                    "RUN_ASSET_STALE",
                                )
                            existing = runtime_models.get(auxiliary_model.name)
                            if existing is not None and existing != auxiliary_model:
                                raise PermanentExecutionError(
                                    "RUN_ASSET_STALE",
                                )
                            runtime_models[auxiliary_model.name] = auxiliary_model
                            if purpose == "vision":
                                vision_model = auxiliary_model
                            if purpose == "title" and model_ref is None:
                                title_bound_name = auxiliary_model.name
                except asyncio.CancelledError:
                    raise
                except PermanentExecutionError:
                    raise
                except Exception:
                    raise PermanentExecutionError(
                        "RUN_ASSET_STALE",
                    ) from None
                if lead_model.name != exact_model_name:
                    raise PermanentExecutionError("RUN_ASSET_STALE")
                runtime_app_config = runtime_app_config.with_runtime_models(
                    tuple(runtime_models.values()),
                )
                if title_bound_name is not None:
                    runtime_app_config = runtime_app_config.model_copy(
                        update={
                            "title": runtime_app_config.title.model_copy(
                                update={"model_name": title_bound_name},
                            ),
                        },
                    )
            elif runtime_app_config.get_model_config(exact_model_name) is None:
                # Compatibility path for isolated unit tests. Production
                # composition always injects the PostgreSQL materializer.
                raise PermanentExecutionError("RUN_ASSET_STALE")

            if runtime_app_config.token_budget.enabled:
                baseline = execution.token_budget_usage or TokenBudgetUsageSnapshot.zero(
                    execution.run.run_id,
                )
                if baseline.run_id != execution.run.run_id:
                    raise PermanentExecutionError("RUN_ASSET_STALE")
                token_budget_usage_recorder = TokenBudgetUsageRecorder(baseline)

            archive_context = await self._memory_archive_context(
                execution,
                runtime_app_config,
            )
            push_current_app_config(runtime_app_config)
            runtime_config_pushed = True
            materialize_kwargs: dict[str, object] = {
                "authorization_boundary": boundary,
                "runtime_kind": execution.runtime_kind,
            }
            if delegate_model_names:
                materialize_kwargs["delegate_model_names"] = delegate_model_names
            private_runtime = await self._asset_runtime.materialize(
                execution.context,
                admitted,
                **materialize_kwargs,
            )
            resolved_runtime_model = resolve_model_ref(
                runtime_app_config,
                private_runtime.model_ref,
            )
            if getattr(resolved_runtime_model, "name", None) != exact_model_name:
                raise PermanentExecutionError("RUN_ASSET_STALE")

            skills_config = getattr(runtime_app_config, "skills", None)
            skill_container_path = getattr(
                skills_config,
                "container_path",
                None,
            )
            run_skill_tree = private_runtime.borrow_materialized_skill_tree()
            if run_skill_tree is not None and not isinstance(
                skill_container_path,
                str,
            ):
                raise PermanentExecutionError("RUN_ASSET_STALE")
            continuation_approval_id = execution.run.kwargs.get(
                "host_execution_approval_id",
            )
            host_execution_approval_port = (
                WorkerHostExecutionApprovalPort(
                    self._factory,
                    context=execution.context,
                    claim=claim,
                    thread_id=execution.run.thread_id,
                    request_ttl_seconds=(runtime_app_config.sandbox.host_execution_approval.request_ttl_seconds),
                    provider_policy=(
                        HostExecutionProviderPolicySnapshot.from_app_config(
                            runtime_app_config,
                        )
                    ),
                    execution_domain=self._host_execution_domain,
                    continuation_approval_id=(continuation_approval_id if isinstance(continuation_approval_id, str) else None),
                    audit=self._execution_approval_audit,
                    retry_safety_boundary=boundary,
                )
                if execution.runtime_kind == "chat"
                else None
            )
            if execution.runtime_kind in {"chat", "skill_builder"}:
                file_authority = PrivateRunFileAuthority(
                    PrivateFileRunScope(
                        execution.context,
                        thread_id=execution.run.thread_id,
                        run_id=execution.run.run_id,
                        authorization_boundary=boundary,
                    ),
                    PrivateSandboxFileProjection(self._factory),
                    PrivateFileFinalizer(
                        self._factory,
                        quota=self._quota,
                        audit=self._file_finalization_audit,
                        output_delivery_port=host_execution_approval_port,
                    ),
                    run_skill_tree=run_skill_tree,
                    skill_container_path=(skill_container_path if run_skill_tree is not None else None),
                    current_upload_snapshot=current_upload_snapshot,
                    output_delivery_port=host_execution_approval_port,
                )
            run_manager = RunManager()
            record = await run_manager.register_persisted(
                run_id=execution.run.run_id,
                thread_id=execution.run.thread_id,
                assistant_id=execution.run.assistant_id,
                on_disconnect=DisconnectMode.continue_,
                metadata=execution.run.metadata,
                kwargs=execution.run.kwargs,
                multitask_strategy=execution.run.multitask_strategy,
                model_name=exact_model_name,
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
                if vision_model is not None
                else None
            )

            checkpointer = self._project_checkpointer.for_context(
                execution.context,
                thread_kind=execution.runtime_kind,
            )
            set_boundary = getattr(
                checkpointer,
                "set_authorization_boundary",
                None,
            )
            if callable(set_boundary):
                set_boundary(boundary)
            memory_authority = (
                PrivateRunMemoryAuthority(
                    self._factory,
                    context=execution.context,
                    claim=claim,
                    thread_id=execution.run.thread_id,
                    namespace=DEFAULT_PRIVATE_MEMORY_NAMESPACE,
                    memory_config=runtime_app_config.memory,
                    audit=self._file_finalization_audit,
                )
                if execution.runtime_kind == "chat"
                else None
            )
            try:
                channel_user_id = _persisted_channel_user_id(
                    execution.run.kwargs,
                )
            except ValueError:
                raise PermanentExecutionError("RUN_ASSET_STALE") from None
            run_context = RunContext(
                checkpointer=checkpointer,
                store=self._store,
                event_store=LeaseAuthorizedRunEventStore(
                    self._event_store,
                    boundary,
                    scope=execution.context.resource_scope,
                ),
                run_events_config=None,
                thread_store=(
                    _PrivateRunThreadMetadataStore(
                        self._factory,
                        scope=execution.context.resource_scope,
                        boundary=boundary,
                    )
                    if execution.runtime_kind == "chat"
                    else None
                ),
                app_config=runtime_app_config,
                private_scope=execution.context.resource_scope,
                authorization_boundary=boundary,
                file_authority=file_authority,
                memory_authority=memory_authority,
                memory_archive_context=archive_context,
                guardrail_attribution=_private_guardrail_attribution(
                    execution.context,
                    execution.run,
                ),
                private_agent_runtime=private_runtime,
                host_execution_approval_port=(host_execution_approval_port),
                channel_user_id=channel_user_id,
                vision_dispatch_authority=vision_dispatch_authority,
                token_budget_usage_recorder=token_budget_usage_recorder,
                resource_ownership=resource_ownership,
                tool_call_control_policy=(tool_call_control_policy.graph_profile if tool_call_control_policy is not None else None),
                max_concurrent_subagents=(tool_call_control_policy.max_concurrent_subagents if tool_call_control_policy is not None else None),
                max_total_subagents=(tool_call_control_policy.max_total_subagents if tool_call_control_policy is not None else None),
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
                    terminal_status=lambda: str(record.status),
                    terminal_error_code=lambda: record.error if record.error in STREAM_TERMINAL_ERROR_CODES else None,
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
                    agent_factory = SkillBuilderAgentFactory(
                        catalog=WorkerSkillBuilderAuthoringCatalog(
                            self._factory,
                            execution.context,
                        ),
                        draft_sink=SkillDesignService(
                            self._factory,
                        ).terminal_sink(
                            execution.context,
                            claim,
                        ),
                    )
                outcome = await self._runner(
                    stream_bridge,
                    run_manager,
                    record,
                    ctx=run_context,
                    agent_factory=agent_factory,
                    graph_input=self._graph_input(execution),
                    config=self._runner_config(
                        execution,
                        archive_context,
                    ),
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
            attempt_usage = self._outcome_usage_snapshot(
                outcome.usage,
                token_budget_usage_recorder,
            )
            if boundary.cancel_requested or boundary.authorization_revoked:
                return AgentExecutionResult.cancelled(
                    attempt_usage=attempt_usage,
                )
            if outcome.status == "succeeded":
                return AgentExecutionResult.succeeded(
                    attempt_usage=attempt_usage,
                    suspended_approval_id=outcome.suspended_approval_id,
                )
            if outcome.status == "cancelled":
                return AgentExecutionResult.cancelled(
                    attempt_usage=attempt_usage,
                )
            error_code = outcome.public_error_code
            if error_code is None:
                raise RuntimeError("failed Run Agent outcome has no error code")
            if error_code in STREAM_TERMINAL_ERROR_CODES:
                return self._terminal_failure_result(
                    error_code,
                    attempt_usage=attempt_usage,
                )
            if boundary.ambiguous_side_effect:
                raise AmbiguousExternalSideEffect(
                    attempt_usage=attempt_usage,
                )
            return self._terminal_failure_result(
                error_code,
                attempt_usage=attempt_usage,
            )
        except asyncio.CancelledError:
            raise
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
                attempt_usage=(self._usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        except TransientExecutionError as error:
            if error.attempt_usage is None and record is not None and not boundary.lease_lost:
                raise TransientExecutionError(
                    error.public_error_code,
                    attempt_usage=self._usage_snapshot(
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
                attempt_usage=(self._usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        except MemoryAuthorityUnavailable:
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(self._usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        except PublicRunError as error:
            if error.code is PublicRunErrorCode.MODEL_OUTPUT_LIMIT:
                raise self._output_limit_error(
                    record,
                    lease_lost=boundary.lease_lost,
                    recorder=token_budget_usage_recorder,
                ) from error
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(self._usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
            ) from None
        except AuthorizationRevoked:
            if boundary.lease_lost:
                raise TransientExecutionError(
                    "EXECUTION_AUTHORITY_UNAVAILABLE",
                ) from None
            return AgentExecutionResult.cancelled(
                attempt_usage=(self._usage_snapshot(record, token_budget_usage_recorder) if record is not None else None),
            )
        except Exception:
            if boundary.ambiguous_side_effect:
                raise AmbiguousExternalSideEffect(
                    attempt_usage=(self._usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
                ) from None
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(self._usage_snapshot(record, token_budget_usage_recorder) if record is not None and not boundary.lease_lost else None),
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
