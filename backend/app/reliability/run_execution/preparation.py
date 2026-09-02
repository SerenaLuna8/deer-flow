"""Private Run preparation: frozen policy, materialized assets, authorities, checkpointer, RunContext."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, RemoveMessage
from langchain_core.messages.utils import convert_to_messages
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context_evidence_observer import (
    PrivateRunContextEvidenceObserver,
)
from app.private_work.execution_approval_audit import (
    HostExecutionApprovalAuditPort,
)
from app.private_work.execution_approval_policy import (
    HostExecutionProviderPolicySnapshot,
)
from app.private_work.execution_approval_worker import (
    WorkerHostExecutionApprovalPort,
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
from app.private_work.private_agent_runtime import PrivateAgentRuntime
from app.private_work.run_admission import (
    AdmittedPrivateRun,
    _strip_client_memory_archive_receipt,
)
from app.private_work.sandbox_files import (
    CurrentUploadSnapshotEntry,
    CurrentUploadSnapshotInvalid,
    PrivateFileRunScope,
    PrivateRunFileAuthority,
    PrivateSandboxFileProjection,
    required_current_upload_snapshot_from_run_kwargs,
)
from app.private_work.snapshot_repository import agent_model_snapshot_purpose
from app.private_work.thread_repository import PrivateThreadRepository
from app.reliability.run_execution.boundary import PrivateRunExecutionBoundary
from app.reliability.run_execution.contracts import PrivateRunExecution
from app.reliability.run_execution.errors import (
    PermanentExecutionError,
    TransientExecutionError,
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
)
from app.reliability.run_execution.tool_call_control_policy import (
    ResolvedRunToolCallControlPolicy,
    resolve_run_tool_call_control_policy,
)
from app.reliability.run_execution.vision_dispatch import (
    PrivateRunVisionDispatchAuthority,
)
from app.shared_assets.models import AssetKind
from app.system_runtime_settings.models import auxiliary_model_snapshot_ref
from app.system_settings.execution_payload import model_execution_provenance
from app.system_settings.model_refs import resolve_model_ref
from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_CONTEXT_KEY,
    SnipArchiveContext,
)
from deerflow.config.model_config import ModelConfig
from deerflow.config.summarization_config import (
    resolve_effective_compaction_policy,
)
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.private_work.memory_document_repository import (
    DEFAULT_MEMORY_NAMESPACE,
)
from deerflow.runtime import RunContext
from deerflow.runtime.context_evidence import ContextRebaseReason, ContextSubject
from deerflow.runtime.host_execution_approval import (
    HOST_EXECUTION_MAX_CHANNEL_USER_ID_LENGTH,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.runs.execution_contracts import RunAgentResourceOwnership
from deerflow.token_budget_usage import (
    TokenBudgetUsageRecorder,
    TokenBudgetUsageSnapshot,
)


def _context_compaction_threshold_tokens(
    app_config: object,
    *,
    context_window_tokens: int | None = None,
) -> int | None:
    summarization = getattr(app_config, "summarization", None)
    if getattr(summarization, "enabled", False) is not True:
        return None
    threshold = getattr(summarization, "trigger_tokens", None)
    if isinstance(threshold, int) and not isinstance(threshold, bool) and threshold > 0:
        keep = getattr(getattr(summarization, "keep", None), "value", None)
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
            raise ValueError("Frozen summarization keep policy is invalid")
        return resolve_effective_compaction_policy(
            trigger_tokens=threshold,
            keep_tokens=keep,
            context_window_tokens=context_window_tokens,
        ).trigger_tokens
    return None


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


def _persisted_context_rebase_reason(
    kwargs: Mapping[str, object],
) -> ContextRebaseReason | None:
    """Read only the Gateway-issued closed history-replacement reason."""

    config = kwargs.get("config")
    if not isinstance(config, Mapping):
        return None
    context = config.get("context")
    if not isinstance(context, Mapping):
        return None
    value = context.get("context_rebase_reason")
    if value is None:
        return None
    try:
        reason = ContextRebaseReason(value)
    except (TypeError, ValueError):
        raise ValueError("persisted Context rebase reason is invalid") from None
    if reason not in {
        ContextRebaseReason.REGENERATION,
        ContextRebaseReason.MESSAGE_EDIT,
    }:
        raise ValueError("persisted Context rebase reason is not Run-admissible")
    return reason


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


def required_current_upload_snapshot(
    run_kwargs: object,
) -> tuple[CurrentUploadSnapshotEntry, ...]:
    try:
        return required_current_upload_snapshot_from_run_kwargs(run_kwargs)
    except CurrentUploadSnapshotInvalid:
        raise PermanentExecutionError(
            "RUN_CURRENT_UPLOAD_STALE",
        ) from None


def runner_config(
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


def graph_input(execution: PrivateRunExecution) -> object:
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


async def load_memory_archive_context(
    execution: PrivateRunExecution,
    runtime_app_config: Any,
    *,
    session_factory: async_sessionmaker[AsyncSession],
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
        async with session_factory() as session, session.begin():
            preference = await AccountPersonalizationRepository(
                session,
            ).read_memory(execution.context.user_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise TransientExecutionError(
            "EXECUTION_AUTHORITY_UNAVAILABLE",
        ) from None

    enabled = bool(runtime_app_config.memory.enabled and preference.memory_enabled)
    summary_model = None
    if enabled:
        model_name = runtime_app_config.summarization.model_name
        if model_name is None:
            models = getattr(runtime_app_config, "models", None)
            if not isinstance(models, list) or not models:
                raise PermanentExecutionError("RUN_ASSET_STALE")
            model_name = models[0].name
        model = runtime_app_config.get_model_config(model_name)
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


@dataclass(frozen=True, slots=True)
class RunPreparationDependencies:
    """Executor-owned collaborators that preparation reads but never owns."""

    session_factory: async_sessionmaker[AsyncSession]
    app_config: Any
    store: Any
    event_store: Any
    project_checkpointer: ProjectScopedCheckpointer
    model_materializer: SystemModelMaterializationPort | None
    runtime_policy_materializer: SystemRuntimePolicyMaterializationPort | None
    quota: PrivateRunAgentQuotaPort
    file_finalization_audit: PrivateFileFinalizationAuditPort | None
    execution_approval_audit: HostExecutionApprovalAuditPort
    host_execution_domain: HostExecutionDomainSnapshot | None


@dataclass(frozen=True, slots=True)
class FrozenRunPolicy:
    """Admitted model, policy, upload, and budget facts frozen before any resource is acquired."""

    exact_model_name: str
    current_upload_snapshot: tuple[CurrentUploadSnapshotEntry, ...]
    runtime_app_config: Any
    tool_call_control_policy: ResolvedRunToolCallControlPolicy | None
    vision_model: ModelConfig | None
    delegate_model_names: dict[uuid.UUID, str]
    token_budget_usage_recorder: TokenBudgetUsageRecorder | None


@dataclass(frozen=True, slots=True)
class MaterializedRunAuthorities:
    """Run-scoped ports built on top of the materialized private runtime."""

    host_execution_approval_port: WorkerHostExecutionApprovalPort | None
    file_authority: PrivateRunFileAuthority | None


@dataclass(frozen=True, slots=True)
class BoundRunCheckpointer:
    """Project-scoped checkpointer bound to the boundary and Context Evidence observer."""

    checkpointer: Any
    context_evidence_observer: PrivateRunContextEvidenceObserver | None


async def freeze_run_policy(
    execution: PrivateRunExecution,
    deps: RunPreparationDependencies,
) -> FrozenRunPolicy:
    """Freeze upload snapshot, runtime policy, lead/delegate/auxiliary models, and token budget.

    Raises ``PermanentExecutionError`` with the exact inline codes
    (``RUN_CURRENT_UPLOAD_STALE``, ``RUN_ASSET_STALE``, ``RUN_POLICY_STALE``)
    and never acquires a releasable resource.
    """
    current_upload_snapshot = required_current_upload_snapshot(execution.run.kwargs)
    exact_model_name = execution.run.model_name
    if exact_model_name is None:
        raise PermanentExecutionError("RUN_ASSET_STALE")
    runtime_app_config = deps.app_config
    runtime_policy = None
    tool_call_control_policy: ResolvedRunToolCallControlPolicy | None = None
    vision_model: ModelConfig | None = None
    delegate_model_names: dict[uuid.UUID, str] = {}
    token_budget_usage_recorder: TokenBudgetUsageRecorder | None = None
    if deps.runtime_policy_materializer is not None:
        try:
            materialized_runtime_policy = await deps.runtime_policy_materializer.materialize_run_snapshot_envelope(
                project_id=execution.context.project_id,
                owner_user_id=str(execution.context.user_id),
                run_id=execution.run.run_id,
            )
            runtime_policy = materialized_runtime_policy.value
            tool_call_control_policy = resolve_run_tool_call_control_policy(
                materialized_runtime_policy,
                execution.run.kwargs,
            )
            runtime_app_config = deps.app_config.with_runtime_policy(
                tool_call_control_policy.app_config_policy,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PermanentExecutionError(
                "RUN_POLICY_STALE",
            ) from None
    if deps.model_materializer is not None:
        title_bound_name: str | None = None
        try:
            lead_model = await deps.model_materializer.materialize_snapshot(
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
                delegated_model = await deps.model_materializer.materialize_snapshot(
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
                        auxiliary_model = await deps.model_materializer.materialize_snapshot(
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

    return FrozenRunPolicy(
        exact_model_name=exact_model_name,
        current_upload_snapshot=current_upload_snapshot,
        runtime_app_config=runtime_app_config,
        tool_call_control_policy=tool_call_control_policy,
        vision_model=vision_model,
        delegate_model_names=delegate_model_names,
        token_budget_usage_recorder=token_budget_usage_recorder,
    )


async def materialize_private_runtime(
    execution: PrivateRunExecution,
    admitted: AdmittedPrivateRun,
    policy: FrozenRunPolicy,
    *,
    asset_runtime: PrivateAssetRuntime,
    boundary: PrivateRunExecutionBoundary,
) -> PrivateAgentRuntime:
    """Materialize the admitted private runtime and return it without further checks.

    The caller assigns the result before anything else can raise so its
    ``finally`` block releases exactly what was acquired.
    """
    materialize_kwargs: dict[str, object] = {
        "authorization_boundary": boundary,
        "runtime_kind": execution.runtime_kind,
    }
    if policy.delegate_model_names:
        materialize_kwargs["delegate_model_names"] = policy.delegate_model_names
    return await asset_runtime.materialize(
        execution.context,
        admitted,
        **materialize_kwargs,
    )


def build_run_authorities(
    execution: PrivateRunExecution,
    policy: FrozenRunPolicy,
    private_runtime: PrivateAgentRuntime,
    *,
    claim: JobClaim,
    boundary: PrivateRunExecutionBoundary,
    deps: RunPreparationDependencies,
) -> MaterializedRunAuthorities:
    """Verify the materialized model, then build the Host Execution port and File Authority."""
    resolved_runtime_model = resolve_model_ref(
        policy.runtime_app_config,
        private_runtime.model_ref,
    )
    if getattr(resolved_runtime_model, "name", None) != policy.exact_model_name:
        raise PermanentExecutionError("RUN_ASSET_STALE")

    skills_config = getattr(policy.runtime_app_config, "skills", None)
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
            deps.session_factory,
            context=execution.context,
            claim=claim,
            thread_id=execution.run.thread_id,
            request_ttl_seconds=(policy.runtime_app_config.sandbox.host_execution_approval.request_ttl_seconds),
            provider_policy=(
                HostExecutionProviderPolicySnapshot.from_app_config(
                    policy.runtime_app_config,
                )
            ),
            execution_domain=deps.host_execution_domain,
            continuation_approval_id=(continuation_approval_id if isinstance(continuation_approval_id, str) else None),
            audit=deps.execution_approval_audit,
            retry_safety_boundary=boundary,
        )
        if execution.runtime_kind == "chat"
        else None
    )
    file_authority: PrivateRunFileAuthority | None = None
    if execution.runtime_kind in {"chat", "skill_builder"}:
        file_authority = PrivateRunFileAuthority(
            PrivateFileRunScope(
                execution.context,
                thread_id=execution.run.thread_id,
                run_id=execution.run.run_id,
                authorization_boundary=boundary,
            ),
            PrivateSandboxFileProjection(deps.session_factory),
            PrivateFileFinalizer(
                deps.session_factory,
                quota=deps.quota,
                audit=deps.file_finalization_audit,
                output_delivery_port=host_execution_approval_port,
            ),
            run_skill_tree=run_skill_tree,
            skill_container_path=(skill_container_path if run_skill_tree is not None else None),
            current_upload_snapshot=policy.current_upload_snapshot,
            output_delivery_port=host_execution_approval_port,
        )
    return MaterializedRunAuthorities(
        host_execution_approval_port=host_execution_approval_port,
        file_authority=file_authority,
    )


def bind_run_checkpointer(
    execution: PrivateRunExecution,
    policy: FrozenRunPolicy,
    *,
    boundary: PrivateRunExecutionBoundary,
    deps: RunPreparationDependencies,
) -> BoundRunCheckpointer:
    """Bind the project-scoped checkpointer to the boundary and, for chat Runs, Context Evidence."""
    checkpointer = deps.project_checkpointer.for_context(
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
    context_evidence_observer: PrivateRunContextEvidenceObserver | None = None
    if execution.runtime_kind == "chat":
        context_model = policy.runtime_app_config.get_model_config(
            policy.exact_model_name,
        )
        if not isinstance(context_model, ModelConfig):
            raise PermanentExecutionError("RUN_ASSET_STALE")
        try:
            model_identity_digest = model_execution_provenance(
                context_model,
            ).payload_checksum
        except ValueError:
            raise PermanentExecutionError("RUN_ASSET_STALE") from None
        subagent_model_context: dict[
            str,
            tuple[str, int, int | None],
        ] = {}
        for frozen_model in policy.runtime_app_config.models:
            try:
                frozen_digest = model_execution_provenance(
                    frozen_model,
                ).payload_checksum
            except ValueError:
                raise PermanentExecutionError("RUN_ASSET_STALE") from None
            subagent_model_context[frozen_model.name] = (
                frozen_digest,
                frozen_model.max_input_tokens,
                _context_compaction_threshold_tokens(
                    policy.runtime_app_config,
                    context_window_tokens=frozen_model.max_input_tokens,
                ),
            )
        configurable = execution.config.get("configurable")
        source_checkpoint_id = configurable.get("checkpoint_id") if isinstance(configurable, Mapping) and isinstance(configurable.get("checkpoint_id"), str) else None
        try:
            context_rebase_reason = _persisted_context_rebase_reason(
                execution.run.kwargs,
            )
        except ValueError:
            raise PermanentExecutionError("RUN_ASSET_STALE") from None
        context_evidence_observer = PrivateRunContextEvidenceObserver(
            deps.session_factory,
            context=execution.context,
            boundary=boundary,
            thread_id=execution.run.thread_id,
            run_id=execution.run.run_id,
            subject=ContextSubject.lead_thread(
                thread_id=execution.run.thread_id,
            ),
            model_identity_digest=model_identity_digest,
            context_window_tokens=context_model.max_input_tokens,
            compaction_enabled=bool(
                policy.runtime_app_config.summarization.enabled,
            ),
            compaction_threshold_tokens=(
                _context_compaction_threshold_tokens(
                    policy.runtime_app_config,
                    context_window_tokens=context_model.max_input_tokens,
                )
            ),
            source_checkpoint_id=source_checkpoint_id,
            rebase_reason=context_rebase_reason,
            subagent_model_context=subagent_model_context,
        )
        set_context_evidence_observer = getattr(
            checkpointer,
            "set_context_evidence_observer",
            None,
        )
        if not callable(set_context_evidence_observer):
            raise PermanentExecutionError("RUN_ASSET_STALE")
        set_context_evidence_observer(context_evidence_observer)
    return BoundRunCheckpointer(
        checkpointer=checkpointer,
        context_evidence_observer=context_evidence_observer,
    )


def build_run_context(
    execution: PrivateRunExecution,
    policy: FrozenRunPolicy,
    *,
    claim: JobClaim,
    boundary: PrivateRunExecutionBoundary,
    checkpointer: Any,
    context_evidence_observer: PrivateRunContextEvidenceObserver | None,
    file_authority: PrivateRunFileAuthority | None,
    memory_archive_context: SnipArchiveContext,
    private_runtime: PrivateAgentRuntime,
    host_execution_approval_port: WorkerHostExecutionApprovalPort | None,
    vision_dispatch_authority: PrivateRunVisionDispatchAuthority | None,
    resource_ownership: RunAgentResourceOwnership,
    deps: RunPreparationDependencies,
) -> RunContext:
    """Build the memory authority, channel identity, and the Worker ``RunContext``."""
    memory_authority = (
        PrivateRunMemoryAuthority(
            deps.session_factory,
            context=execution.context,
            claim=claim,
            thread_id=execution.run.thread_id,
            namespace=DEFAULT_PRIVATE_MEMORY_NAMESPACE,
            memory_config=policy.runtime_app_config.memory,
            audit=deps.file_finalization_audit,
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
        store=deps.store,
        event_store=LeaseAuthorizedRunEventStore(
            deps.event_store,
            boundary,
            scope=execution.context.resource_scope,
        ),
        run_events_config=None,
        thread_store=(
            _PrivateRunThreadMetadataStore(
                deps.session_factory,
                scope=execution.context.resource_scope,
                boundary=boundary,
            )
            if execution.runtime_kind == "chat"
            else None
        ),
        app_config=policy.runtime_app_config,
        private_scope=execution.context.resource_scope,
        authorization_boundary=boundary,
        file_authority=file_authority,
        memory_authority=memory_authority,
        memory_archive_context=memory_archive_context,
        guardrail_attribution=_private_guardrail_attribution(
            execution.context,
            execution.run,
        ),
        private_agent_runtime=private_runtime,
        host_execution_approval_port=(host_execution_approval_port),
        channel_user_id=channel_user_id,
        vision_dispatch_authority=vision_dispatch_authority,
        token_budget_usage_recorder=policy.token_budget_usage_recorder,
        resource_ownership=resource_ownership,
        tool_call_control_policy=(policy.tool_call_control_policy.graph_profile if policy.tool_call_control_policy is not None else None),
        context_evidence_observer=context_evidence_observer,
        max_concurrent_subagents=(policy.tool_call_control_policy.max_concurrent_subagents if policy.tool_call_control_policy is not None else None),
        max_total_subagents=(policy.tool_call_control_policy.max_total_subagents if policy.tool_call_control_policy is not None else None),
    )
    return run_context


__all__ = [
    "BoundRunCheckpointer",
    "FrozenRunPolicy",
    "MaterializedRunAuthorities",
    "RunPreparationDependencies",
    "bind_run_checkpointer",
    "build_run_authorities",
    "build_run_context",
    "freeze_run_policy",
    "graph_input",
    "load_memory_archive_context",
    "materialize_private_runtime",
    "required_current_upload_snapshot",
    "runner_config",
]
