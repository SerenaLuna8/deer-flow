"""Project-scoped chat controls that never restore the legacy global API."""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import BaseMessage
from langgraph.types import Overwrite
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import deerflow.utils.llm_text as llm_text
from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.authorization import PrivateRequestAuthorizationBoundary
from app.private_work.checkpoint_lineage import (
    CheckpointLineageError,
    find_settled_checkpoint_before_message,
)
from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    bind_transaction_checkpoint_state,
    checkpoint_config,
    snapshot_checkpoint_id,
)
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkCompactionDisabled,
    PrivateWorkConflict,
    PrivateWorkContextUsageUnsupported,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkRunExecutionProfileUnsupported,
    PrivateWorkRunModelSelectionLocked,
    PrivateWorkRunModelUnavailable,
    PrivateWorkThreadBusy,
    PrivateWorkUnavailable,
)
from app.private_work.execution_profile import (
    RequestedRunExecutionProfile,
    RunExecutionProfileUnsupported,
    RunModelSelectionLocked,
    selected_run_model_ref,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import PrivateRunRecord, PrivateRunRepository
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.private_work.thread_repository import PrivateThreadRecord, PrivateThreadRepository
from app.private_work.thread_service import PrivateThreadService
from app.projects.capabilities import Capability
from app.reliability.run_execution.tool_call_control_policy import (
    resolve_run_tool_call_control_policy,
)
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import (
    AssetKind,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedRunAssetFact,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.system_runtime_settings import (
    SystemRuntimePolicyMaterializer,
    project_memory_compaction_app_config_policy,
)
from app.system_runtime_settings.errors import SystemRuntimePolicyUnavailable
from app.system_settings import (
    SystemModelMaterializationUnavailable,
    SystemModelMaterializer,
)
from app.system_settings.execution_payload import model_execution_provenance
from app.system_settings.model_refs import ConfiguredModelRefResolver
from deerflow.agents.memory.snip import SnipArchiveContext
from deerflow.agents.middlewares.provider_request_usage import (
    ProviderRequestUsageUnsupported,
    provider_request_closure_identity,
    provider_request_runtime_policy_compatibility_identity,
    provider_request_runtime_policy_identity,
)
from deerflow.agents.provider_request_contract import (
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.config.model_config import ModelConfig
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.models import ModelRuntimeProfile
from deerflow.persistence.private_work.memory_document_repository import (
    DEFAULT_MEMORY_NAMESPACE,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.system_settings import RunModelConfigSnapshotRow
from deerflow.runtime.context_compaction import (
    ContextCompactionDisabled,
    ContextCompactionFailed,
    ContextUsageUnsupported,
    ThreadCompactionResult,
    ThreadContextUsage,
    commit_thread_compaction,
    has_complete_turns,
    measure_thread_context_usage,
    prepare_thread_compaction,
)
from deerflow.runtime.events.store import RunEventStore
from deerflow.runtime.goal import DEFAULT_MAX_GOAL_CONTINUATIONS, build_goal_state
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY, get_original_user_content_text, message_to_text
from deerflow.utils.oneshot_llm import run_oneshot_llm

logger = logging.getLogger(__name__)

_HISTORY_SCAN_LIMIT = 200
_SUGGESTION_MESSAGE_LIMIT = 6
_SUGGESTION_MESSAGE_CHARS = 4000
_SUGGESTION_TOTAL_CHARS = 12000
_CONTEXT_USAGE_AUTHORITY_RETRY_LIMIT = 3


def _context_usage_active_run_predicate():
    """Keep Gauge marker and full-read Run authority exactly aligned."""

    return or_(
        RunRow.status.in_(("pending", "running")),
        RunRow.finalization_status == "finalizing",
    )


@dataclass(frozen=True, slots=True)
class ContextUsageAuthorityMarker:
    """Cheap cache identity for the exact Thread's Gauge authority."""

    cache_marker: str


@dataclass(frozen=True, slots=True)
class _ContextUsageAuthority:
    """One authoritative source for the Gauge's Lead model and runtime policy."""

    run_id: str | None
    lead_model_ref: str
    closure_identity: str | None = None
    profile_authority_identity: str | None = None
    profile_closure_identity: str | None = None
    asset_facts: tuple[ResolvedRunAssetFact, ...] | None = None
    profile_asset_facts: tuple[ResolvedRunAssetFact, ...] | None = None
    profile_run_kwargs: Mapping[str, object] | None = None
    profile_proof_attempted: bool = False
    profile_runtime_policy_identity: str | None = None
    profile_runtime_policy_compatibility_identity: str | None = None
    lead_model_payload_checksum: str | None = None
    profile_lead_model_payload_checksum: str | None = None
    resolved_lead_model_name: str | None = None
    profile_lead_model_name: str | None = None
    profile_title_model_name: str | None = None


class ProjectChatControlService:
    """Authority boundary for Goal, Compact, Branch, Regenerate, and Follow-up."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        project_scoped_checkpointer: ProjectScopedCheckpointer,
        thread_service: PrivateThreadService,
        run_event_store: RunEventStore,
        *,
        resolver: ProjectAssetResolver | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
        model_materializer: SystemModelMaterializer | None = None,
        runtime_policy_materializer: SystemRuntimePolicyMaterializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._project_scoped_checkpointer = project_scoped_checkpointer
        self._thread_service = thread_service
        self._run_event_store = run_event_store
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._snapshots = RunSnapshotRepository(
            session_factory,
            endpoint_policy=endpoint_policy,
        )
        self._revalidator = PrivateWorkRevalidator()
        self._model_materializer = model_materializer
        self._runtime_policy_materializer = runtime_policy_materializer

    async def get_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig | None = None,
    ) -> dict[str, Any] | None:
        context = require_issued_private_work_context(context)
        snapshot = await self._state(
            context,
            app_config or get_app_config(),
            as_node="goal",
        ).aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(snapshot) is None:
            raise PrivateWorkNotFound(context.request_id)
        goal = (snapshot.values or {}).get("goal")
        return copy.deepcopy(goal) if isinstance(goal, dict) else None

    async def set_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        objective: str,
        max_continuations: int = DEFAULT_MAX_GOAL_CONTINUATIONS,
        app_config: AppConfig | None = None,
    ) -> dict[str, Any]:
        context = require_issued_private_work_context(context)
        try:
            goal = build_goal_state(
                objective,
                max_continuations=max_continuations,
            )
        except (TypeError, ValueError):
            raise PrivateWorkInvalid(context.request_id) from None
        await self._write_goal(
            context,
            thread_id,
            goal,
            app_config=app_config or get_app_config(),
        )
        return goal

    async def clear_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig | None = None,
    ) -> None:
        context = require_issued_private_work_context(context)
        await self._write_goal(
            context,
            thread_id,
            None,
            app_config=app_config or get_app_config(),
        )

    async def _write_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        goal: dict[str, Any] | None,
        *,
        app_config: AppConfig,
    ) -> None:
        saver = self._saver(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_thread(
                    session,
                    context,
                    thread_id,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    reject_incomplete_run=True,
                )
                state = bind_transaction_checkpoint_state(
                    saver,
                    session,
                    app_config,
                    as_node="goal",
                )
                snapshot = await state.aget(checkpoint_config(thread_id))
                parent_checkpoint_id = snapshot_checkpoint_id(snapshot)
                if parent_checkpoint_id is None:
                    raise PrivateWorkNotFound(context.request_id)
                await state.aupdate(
                    snapshot.config,
                    {"goal": Overwrite(copy.deepcopy(goal))},
                    as_node="goal",
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def compact(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        force: bool,
        keep: tuple[str, int | float] | None,
        app_config: AppConfig,
    ) -> ThreadCompactionResult:
        """Prepare outside locks, then compare-and-swap under a second lock."""

        context = require_issued_private_work_context(context)
        authorization_boundary = PrivateRequestAuthorizationBoundary(
            lambda: self._validate_control_authority(
                context,
                thread_id,
                reject_incomplete_run=True,
            ),
            request_id=context.request_id,
        )
        saver = self._saver(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_thread(
                    session,
                    context,
                    thread_id,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    reject_incomplete_run=True,
                )
                locked_state = bind_transaction_checkpoint_state(
                    saver,
                    session,
                    app_config,
                    as_node="manual_compaction",
                )
                source = await locked_state.aget(checkpoint_config(thread_id))
                source_checkpoint_id = snapshot_checkpoint_id(source)
                if source_checkpoint_id is None:
                    raise PrivateWorkNotFound(context.request_id)
                preference = await AccountPersonalizationRepository(
                    session,
                ).read_memory(context.user_id)

            runtime_config = await self._materialize_compaction_config(
                context,
                thread_id,
                app_config,
            )
            archive_context = self._compaction_archive_context(
                context,
                runtime_config,
                preference_version=preference.version,
                preference_enabled=preference.memory_enabled,
                source_checkpoint_id=source_checkpoint_id,
            )
            prepared = await prepare_thread_compaction(
                self._state(
                    context,
                    runtime_config,
                    as_node="manual_compaction",
                ),
                thread_id,
                keep=keep,
                force=force,
                user_id=str(context.user_id),
                app_config=runtime_config,
                snapshot=source,
                authorization_boundary=authorization_boundary,
                memory_archive_context=archive_context,
            )
            if not prepared.result.compacted:
                return prepared.result

            async with self._session_factory() as session, session.begin():
                await self._lock_thread(
                    session,
                    context,
                    thread_id,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    reject_incomplete_run=True,
                )
                locked_state = bind_transaction_checkpoint_state(
                    saver,
                    session,
                    runtime_config,
                    as_node="manual_compaction",
                )
                current = await locked_state.aget(checkpoint_config(thread_id))
                if snapshot_checkpoint_id(current) is None:
                    raise PrivateWorkNotFound(context.request_id)
                if snapshot_checkpoint_id(current) != prepared.source_checkpoint_id:
                    raise PrivateWorkConflict(context.request_id)
                return await commit_thread_compaction(
                    locked_state,
                    prepared,
                )
        except AuthorizationRevoked:
            raise authorization_boundary.private_error() from None
        except ContextCompactionDisabled:
            raise PrivateWorkCompactionDisabled(context.request_id) from None
        except ContextCompactionFailed:
            raise PrivateWorkUnavailable(context.request_id) from None
        except LookupError:
            raise PrivateWorkNotFound(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            logger.exception(
                "Project context compaction failed: request_id=%s",
                context.request_id,
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def context_usage(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig,
        selected_model_name: str | None = None,
    ) -> ThreadContextUsage:
        """Measure the authorized materialized Thread head without writing it."""

        context = require_issued_private_work_context(context)
        try:
            authority = await self._resolve_context_usage_authority(
                context,
                thread_id,
                selected_model_name=selected_model_name,
            )
            for _attempt in range(_CONTEXT_USAGE_AUTHORITY_RETRY_LIMIT):
                (
                    runtime_config,
                    context_model_name,
                    current_lead_model,
                ) = await self._materialize_context_usage_config(
                    context,
                    app_config,
                    authority=authority,
                    selected_model_name=selected_model_name,
                )
                snapshot = await self._state(
                    context,
                    runtime_config,
                    as_node="context_usage",
                ).aget(checkpoint_config(thread_id))
                if snapshot_checkpoint_id(snapshot) is None:
                    raise PrivateWorkNotFound(context.request_id)
                profile_authority_identity = None
                if authority.run_id is None:
                    values = getattr(snapshot, "values", None)
                    profile = values.get(PROVIDER_REQUEST_PROFILE_STATE_KEY) if isinstance(values, Mapping) else None
                    value = profile.get("authority_identity") if isinstance(profile, Mapping) else None
                    if isinstance(value, str) and value:
                        profile_authority_identity = value
                current_authority = await self._resolve_context_usage_authority(
                    context,
                    thread_id,
                    selected_model_name=selected_model_name,
                    profile_authority_identity=profile_authority_identity,
                )
                if self._same_context_usage_current_authority(
                    current_authority,
                    authority,
                ):
                    authority = current_authority
                    idle_profile = None
                    if authority.run_id is None:
                        if (
                            getattr(
                                authority,
                                "profile_authority_identity",
                                None,
                            )
                            is not None
                        ):
                            authority = await self._prove_idle_provider_profile(
                                context,
                                authority=authority,
                                runtime_config=runtime_config,
                                current_lead_model=current_lead_model,
                            )
                        idle_profile = self._idle_provider_request_profile(
                            snapshot,
                            runtime_config=runtime_config,
                            authority=authority,
                        )
                    return measure_thread_context_usage(
                        snapshot,
                        app_config=runtime_config,
                        context_model_name=context_model_name,
                        provider_request_profile=idle_profile,
                        expected_authority_identity=authority.run_id,
                        require_provider_request_profile=True,
                    )
                authority = current_authority
            raise PrivateWorkUnavailable(context.request_id)
        except PrivateWorkError:
            raise
        except ContextUsageUnsupported:
            raise PrivateWorkContextUsageUnsupported(context.request_id) from None
        except (ContextCompactionFailed, ValueError):
            raise PrivateWorkUnavailable(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            logger.exception(
                "Project context usage measurement failed: request_id=%s",
                context.request_id,
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def context_usage_authority_marker(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> ContextUsageAuthorityMarker:
        """Project only Run identity; never materialize Gauge inputs."""

        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=False,
                )
                thread = await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    lock=False,
                )
                if thread is None:
                    raise PrivateWorkNotFound(context.request_id)

                private_run_scope = (
                    RunRow.project_id == context.project_id,
                    RunRow.owner_user_id == str(context.user_id),
                    RunRow.thread_id == thread_id,
                )
                active_run_id = (
                    select(RunRow.run_id)
                    .where(
                        *private_run_scope,
                        _context_usage_active_run_predicate(),
                    )
                    .order_by(RunRow.created_at.asc(), RunRow.run_id.asc())
                    .limit(1)
                    .scalar_subquery()
                )
                latest_run_id = (
                    select(RunRow.run_id)
                    .where(
                        *private_run_scope,
                        RunRow.status != "deleted",
                    )
                    .order_by(RunRow.created_at.desc(), RunRow.run_id.desc())
                    .limit(1)
                    .scalar_subquery()
                )
                active, latest = (
                    await session.execute(
                        select(
                            active_run_id.label("active_run_id"),
                            latest_run_id.label("latest_run_id"),
                        )
                    )
                ).one()
                if active is not None:
                    if not isinstance(active, str) or not active:
                        raise PrivateWorkUnavailable(context.request_id)
                    return ContextUsageAuthorityMarker(
                        cache_marker=f"active:{active}",
                    )
                if latest is not None and (not isinstance(latest, str) or not latest):
                    raise PrivateWorkUnavailable(context.request_id)
                return ContextUsageAuthorityMarker(
                    cache_marker=f"idle:{latest or 'none'}",
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    @staticmethod
    def _same_context_usage_current_authority(
        left: _ContextUsageAuthority,
        right: _ContextUsageAuthority,
    ) -> bool:
        """Compare the current request closure, excluding frozen proof fields."""

        return (
            left.run_id == right.run_id
            and left.lead_model_ref == right.lead_model_ref
            and getattr(left, "closure_identity", None) == getattr(right, "closure_identity", None)
            and getattr(left, "asset_facts", None) == getattr(right, "asset_facts", None)
        )

    @staticmethod
    def _idle_provider_request_profile(
        snapshot: object,
        *,
        runtime_config: AppConfig,
        authority: _ContextUsageAuthority,
    ) -> Mapping[str, object]:
        """Reuse only a policy-identical profile for the exact current assets."""

        values = getattr(snapshot, "values", None)
        profile = values.get(PROVIDER_REQUEST_PROFILE_STATE_KEY) if isinstance(values, Mapping) else None
        try:
            policy_identity = provider_request_runtime_policy_identity(
                runtime_config,
            )
            compatibility_identity = provider_request_runtime_policy_compatibility_identity(
                runtime_config,
            )
        except ProviderRequestUsageUnsupported:
            raise ContextUsageUnsupported("Idle Gauge runtime policy identity is unavailable.") from None
        if not isinstance(profile, Mapping):
            raise ContextUsageUnsupported(
                "Idle Gauge cannot prove that the frozen provider profile still matches the next Run.",
            )
        if getattr(authority, "profile_proof_attempted", False):
            policy_matches = (
                authority.profile_runtime_policy_identity is not None
                and authority.profile_runtime_policy_compatibility_identity is not None
                and profile.get("runtime_policy_identity") == authority.profile_runtime_policy_identity
                and compatibility_identity == authority.profile_runtime_policy_compatibility_identity
            )
            model_matches = (
                authority.lead_model_payload_checksum is not None
                and authority.profile_lead_model_payload_checksum is not None
                and authority.lead_model_payload_checksum == authority.profile_lead_model_payload_checksum
                and authority.resolved_lead_model_name is not None
                and authority.resolved_lead_model_name == authority.profile_lead_model_name
                and profile.get("model_name") == authority.resolved_lead_model_name
            )
        else:
            policy_matches = profile.get("runtime_policy_identity") == policy_identity
            model_matches = profile.get("model_name") == authority.lead_model_ref
        if (
            authority.closure_identity is None
            or authority.profile_authority_identity is None
            or authority.profile_closure_identity is None
            or authority.asset_facts is None
            or authority.profile_asset_facts is None
            or profile.get("authority_identity") != authority.profile_authority_identity
            or not model_matches
            or profile.get("closure_identity") != authority.profile_closure_identity
            or authority.asset_facts != authority.profile_asset_facts
            or not policy_matches
            or profile.get("workload_profile") != "interactive"
            or profile.get("mcp_closure_present") is not False
        ):
            raise ContextUsageUnsupported("Idle Gauge cannot prove that the frozen provider profile still matches the next Run.")
        return profile

    async def _materialize_context_usage_config(
        self,
        context: PrivateWorkContext,
        app_config: AppConfig,
        *,
        authority: _ContextUsageAuthority,
        selected_model_name: str | None,
    ) -> tuple[AppConfig, str, ModelConfig]:
        """Bind Gauge policy and models to the same authority as the next call."""

        model_materializer = self._model_materializer
        if model_materializer is None:
            raise PrivateWorkUnavailable(context.request_id)

        runtime_config = app_config
        try:
            if authority.run_id is not None:
                policy_materializer = self._runtime_policy_materializer
                if policy_materializer is None:
                    raise SystemRuntimePolicyUnavailable
                frozen_policy = await policy_materializer.materialize_run_snapshot_envelope(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    run_id=authority.run_id,
                )
                runtime_config = app_config.with_runtime_policy(
                    project_memory_compaction_app_config_policy(
                        frozen_policy.value,
                    )
                )
                lead_model = await model_materializer.materialize_snapshot(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    run_id=authority.run_id,
                    purpose="lead",
                )
            else:
                lead_model = await model_materializer.materialize_active(
                    authority.lead_model_ref,
                )

            if lead_model.name != authority.lead_model_ref and authority.lead_model_ref != "default":
                raise SystemModelMaterializationUnavailable

            runtime_models = [lead_model]
            summary_model_ref = runtime_config.summarization.model_name
            if summary_model_ref is not None and summary_model_ref != lead_model.name:
                if authority.run_id is not None:
                    summary_model = await model_materializer.materialize_snapshot(
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        run_id=authority.run_id,
                        purpose="summarization",
                    )
                else:
                    summary_model = await model_materializer.materialize_active(
                        summary_model_ref,
                    )
                if summary_model.name != summary_model_ref and summary_model_ref != "default":
                    raise SystemModelMaterializationUnavailable
                if summary_model.name == lead_model.name:
                    if summary_model != lead_model:
                        raise SystemModelMaterializationUnavailable
                else:
                    runtime_models.append(summary_model)

            # A Run with title generation enabled and no explicit title model
            # freezes the catalog default, then binds that concrete name into
            # its runtime AppConfig.  Reproduce the same binding for an idle
            # Gauge; otherwise its policy fingerprint can never match the
            # immutable provider profile written by the next Run.
            title_bound_name: str | None = None
            title_config = getattr(runtime_config, "title", None)
            if authority.run_id is None and bool(getattr(title_config, "enabled", False)) and getattr(title_config, "model_name", None) is None:
                title_model = await model_materializer.materialize_active(None)
                existing = next(
                    (model for model in runtime_models if model.name == title_model.name),
                    None,
                )
                if existing is not None and existing != title_model:
                    raise SystemModelMaterializationUnavailable
                if existing is None:
                    runtime_models.append(title_model)
                title_bound_name = title_model.name

            runtime_config = runtime_config.with_runtime_models(
                tuple(runtime_models),
            )
            if title_bound_name is not None:
                runtime_config = runtime_config.model_copy(
                    update={
                        "title": runtime_config.title.model_copy(
                            update={"model_name": title_bound_name},
                        ),
                    },
                )
            return (
                runtime_config,
                lead_model.name,
                lead_model,
            )
        except SystemRuntimePolicyUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except SystemModelMaterializationUnavailable:
            if authority.run_id is None and selected_model_name is not None:
                raise PrivateWorkRunModelUnavailable(context.request_id) from None
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _prove_idle_provider_profile(
        self,
        context: PrivateWorkContext,
        *,
        authority: _ContextUsageAuthority,
        runtime_config: AppConfig,
        current_lead_model: ModelConfig,
    ) -> _ContextUsageAuthority:
        """Prove an idle checkpoint profile against its immutable source Run."""

        run_id = authority.profile_authority_identity
        run_kwargs = authority.profile_run_kwargs
        if run_id is None or run_kwargs is None:
            return replace(authority, profile_proof_attempted=True)
        policy_materializer = self._runtime_policy_materializer
        if policy_materializer is None:
            raise PrivateWorkUnavailable(context.request_id)
        try:
            frozen_policy = await policy_materializer.materialize_run_snapshot_envelope(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                run_id=run_id,
            )
            control_policy = resolve_run_tool_call_control_policy(
                frozen_policy,
                run_kwargs,
            )
            if control_policy.workload_profile.name != "interactive":
                return replace(authority, profile_proof_attempted=True)
            frozen_runtime_config = runtime_config.with_runtime_policy(
                control_policy.app_config_policy,
            )
            frozen_title = frozen_runtime_config.title
            if frozen_title.enabled and frozen_title.model_name is None and authority.profile_title_model_name is not None:
                frozen_runtime_config = frozen_runtime_config.model_copy(
                    update={
                        "title": frozen_title.model_copy(
                            update={
                                "model_name": authority.profile_title_model_name,
                            },
                        ),
                    },
                )
            current_provenance = model_execution_provenance(current_lead_model)
            return replace(
                authority,
                profile_proof_attempted=True,
                profile_runtime_policy_identity=(
                    provider_request_runtime_policy_identity(
                        frozen_runtime_config,
                    )
                ),
                profile_runtime_policy_compatibility_identity=(
                    provider_request_runtime_policy_compatibility_identity(
                        frozen_runtime_config,
                    )
                ),
                lead_model_payload_checksum=current_provenance.payload_checksum,
                resolved_lead_model_name=current_lead_model.name,
            )
        except SystemRuntimePolicyUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except (ProviderRequestUsageUnsupported, TypeError, ValueError):
            return replace(authority, profile_proof_attempted=True)

    async def _resolve_context_usage_authority(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        selected_model_name: str | None,
        profile_authority_identity: str | None = None,
    ) -> _ContextUsageAuthority:
        """Resolve active-Run snapshots or the next composer's selected model."""

        try:
            async with self._session_factory() as session, session.begin():
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                thread = await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    lock=True,
                )
                if thread is None:
                    raise PrivateWorkNotFound(context.request_id)

                active_run = (
                    await session.execute(
                        select(RunRow.run_id, RunRow.model_name)
                        .where(
                            RunRow.project_id == context.project_id,
                            RunRow.owner_user_id == str(context.user_id),
                            RunRow.thread_id == thread_id,
                            _context_usage_active_run_predicate(),
                        )
                        .order_by(RunRow.created_at.asc(), RunRow.run_id.asc())
                        .limit(1)
                    )
                ).one_or_none()
                if active_run is not None:
                    run_id, model_name = active_run
                    if not isinstance(model_name, str) or not model_name:
                        raise PrivateWorkUnavailable(context.request_id)
                    return _ContextUsageAuthority(
                        run_id=run_id,
                        lead_model_ref=model_name,
                    )

                selection = AssetSelection(
                    AssetKind.AGENT,
                    thread.agent_asset_id,
                )
                lead_agent = await self._resolver.resolve_project_asset_snapshot_in_session(
                    session,
                    current,
                    selection,
                )
                current_facts = await self._resolver.resolve_run_asset_facts_in_session(
                    session,
                    current,
                    selection,
                )
                if (
                    type(lead_agent) is not ResolvedAgentSnapshot
                    or lead_agent.scope.value != thread.agent_scope
                    or not self._valid_context_usage_asset_facts(current_facts)
                    or current_facts[0].scope != lead_agent.scope
                    or current_facts[0].asset_id != lead_agent.asset_id
                    or current_facts[0].version_id != lead_agent.version_id
                    or current_facts[0].checksum != lead_agent.checksum
                ):
                    raise PrivateWorkAssetStale(context.request_id)
                lead_model_ref = selected_run_model_ref(
                    lead_agent.payload.model_ref,
                    RequestedRunExecutionProfile(
                        model_name=selected_model_name,
                    ),
                )
                authority = _ContextUsageAuthority(
                    run_id=None,
                    lead_model_ref=lead_model_ref,
                    closure_identity=provider_request_closure_identity(
                        agent_facts=((str(lead_agent.version_id), lead_agent.checksum),),
                        catalog_generation=current_facts[0].catalog_generation,
                    ),
                    asset_facts=current_facts,
                )
                if profile_authority_identity is None:
                    return authority

                profile_run = (
                    await session.execute(
                        select(RunRow.run_id, RunRow.kwargs_json)
                        .where(
                            RunRow.project_id == context.project_id,
                            RunRow.owner_user_id == str(context.user_id),
                            RunRow.thread_id == thread_id,
                            RunRow.run_id == profile_authority_identity,
                            RunRow.status != "deleted",
                        )
                        .limit(1)
                    )
                ).one_or_none()
                if profile_run is None:
                    return authority
                profile_run_id, profile_run_kwargs = profile_run
                if not isinstance(profile_run_kwargs, Mapping):
                    return authority
                profile_model_rows = (
                    await session.execute(
                        select(
                            RunModelConfigSnapshotRow.purpose,
                            RunModelConfigSnapshotRow.model_config_id,
                            RunModelConfigSnapshotRow.payload_checksum,
                        ).where(
                            RunModelConfigSnapshotRow.project_id == context.project_id,
                            RunModelConfigSnapshotRow.owner_user_id == str(context.user_id),
                            RunModelConfigSnapshotRow.thread_id == thread_id,
                            RunModelConfigSnapshotRow.run_id == profile_run_id,
                            RunModelConfigSnapshotRow.purpose.in_(("lead", "title")),
                        )
                    )
                ).all()
                profile_model_facts = {
                    purpose: (str(model_config_id), payload_checksum) for purpose, model_config_id, payload_checksum in profile_model_rows if purpose in {"lead", "title"} and isinstance(payload_checksum, str) and len(payload_checksum) == 64
                }
                profile_lead_model = profile_model_facts.get("lead")
                if profile_lead_model is None or len(profile_model_facts) != len(profile_model_rows):
                    return authority
                frozen_facts = await self._snapshots.list_asset_facts_in_session(
                    session,
                    context,
                    thread_id,
                    profile_run_id,
                )
                if not self._valid_context_usage_asset_facts(frozen_facts):
                    return authority
                frozen_lead = frozen_facts[0]
                return _ContextUsageAuthority(
                    run_id=None,
                    lead_model_ref=lead_model_ref,
                    closure_identity=authority.closure_identity,
                    profile_authority_identity=profile_run_id,
                    profile_closure_identity=provider_request_closure_identity(
                        agent_facts=(
                            (
                                str(frozen_lead.version_id),
                                frozen_lead.checksum,
                            ),
                        ),
                        catalog_generation=frozen_lead.catalog_generation,
                    ),
                    asset_facts=authority.asset_facts,
                    profile_asset_facts=frozen_facts,
                    profile_run_kwargs=dict(profile_run_kwargs),
                    profile_lead_model_name=profile_lead_model[0],
                    profile_lead_model_payload_checksum=profile_lead_model[1],
                    profile_title_model_name=(profile_model_facts["title"][0] if "title" in profile_model_facts else None),
                )
        except PrivateWorkError:
            raise
        except RunModelSelectionLocked:
            raise PrivateWorkRunModelSelectionLocked(context.request_id) from None
        except RunExecutionProfileUnsupported:
            raise PrivateWorkRunExecutionProfileUnsupported(context.request_id) from None
        except TypeError:
            raise PrivateWorkRunExecutionProfileUnsupported(context.request_id) from None
        except (
            AssetForbidden,
            AssetValidationFailed,
            AssetResolutionUnavailable,
            RunSnapshotAssetStale,
        ):
            raise PrivateWorkAssetStale(context.request_id) from None
        except (AssetStorageUnavailable, DBAPIError):
            raise PrivateWorkUnavailable(context.request_id) from None

    @staticmethod
    def _valid_context_usage_asset_facts(
        facts: tuple[ResolvedRunAssetFact, ...],
    ) -> bool:
        if not facts:
            return False
        lead = facts[0]
        generation = lead.catalog_generation
        return lead.kind is AssetKind.AGENT and lead.dependency_order == 0 and all(type(fact) is ResolvedRunAssetFact and fact.dependency_order == order and fact.catalog_generation == generation for order, fact in enumerate(facts))

    async def _materialize_compaction_config(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        app_config: AppConfig,
    ) -> AppConfig:
        """Bind manual compaction to one current, exact model definition.

        A dedicated summarization model wins when the current database policy
        selects one. Otherwise the thread's current Agent model is the
        authoritative fallback. Production composition always supplies the
        system materializer; the unmaterialized branch exists only for
        isolated service tests that inject an exact ``AppConfig`` directly.
        """

        model_ref = app_config.summarization.model_name
        if model_ref is None:
            resolved = await self._resolve_agent_authority(context, thread_id)
            model_ref = resolved.payload.model_ref
        if self._model_materializer is None:
            return app_config
        try:
            runtime_model = await self._model_materializer.materialize_active(
                model_ref,
            )
        except SystemModelMaterializationUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        if runtime_model.name != model_ref and model_ref != "default":
            raise PrivateWorkUnavailable(context.request_id)
        return app_config.with_runtime_models((runtime_model,))

    def _compaction_archive_context(
        self,
        context: PrivateWorkContext,
        app_config: AppConfig,
        *,
        preference_version: int,
        preference_enabled: bool,
        source_checkpoint_id: str,
    ) -> SnipArchiveContext:
        requested_enabled = bool(app_config.memory.enabled and preference_enabled)
        effective_enabled = requested_enabled
        summary_model = None
        if requested_enabled:
            model_name = app_config.summarization.model_name
            model = app_config.get_model_config(model_name) if model_name is not None else (app_config.models[0] if app_config.models else None)
            try:
                summary_model = model_execution_provenance(model)
            except ValueError:
                if self._model_materializer is not None:
                    raise PrivateWorkUnavailable(context.request_id)
                # Isolated service tests may inject an unmaterialized AppConfig.
                # They retain Thread compaction but cannot author durable
                # history without an exact PostgreSQL model-version identity.
                effective_enabled = False
        return SnipArchiveContext(
            enabled=effective_enabled,
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            namespace=DEFAULT_MEMORY_NAMESPACE,
            preference_version=preference_version,
            summary_model=summary_model,
            source_checkpoint_id=source_checkpoint_id,
        )

    async def lock_and_verify_dream_archive_ready(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig,
    ) -> bool:
        """Lock one Thread and verify its current head has no complete turns.

        The checkpoint read uses the caller-owned SQL transaction, so any
        carried archive receipt is repaired before this method returns. The
        caller must keep the transaction open through Dream admission; every
        production checkpoint writer also locks the Thread row and therefore
        cannot race a new head between this proof and admission.
        """

        context = require_issued_private_work_context(context)
        if not session.in_transaction():
            raise PrivateWorkUnavailable(context.request_id)
        await self._lock_thread(
            session,
            context,
            thread_id,
            Capability.PRIVATE_WORK_CREATE,
            Capability.SHARED_ASSETS_EXECUTE,
            reject_incomplete_run=True,
        )
        state = bind_transaction_checkpoint_state(
            self._saver(context),
            session,
            app_config,
            as_node="dream_archive_barrier",
        )
        snapshot = await state.aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(snapshot) is None:
            raise PrivateWorkNotFound(context.request_id)
        messages = (snapshot.values or {}).get("messages")
        if messages is None:
            return True
        if not isinstance(messages, list):
            raise PrivateWorkUnavailable(context.request_id)
        return not has_complete_turns(messages)

    async def branch(
        self,
        context: PrivateWorkContext,
        source_thread_id: str,
        *,
        message_id: str,
        message_ids: list[str],
        title: str | None,
        app_config: AppConfig | None = None,
    ) -> tuple[PrivateThreadRecord, str]:
        context = require_issued_private_work_context(context)
        resolved_app_config = app_config or get_app_config()
        source = await self._thread_service.get(context, source_thread_id)
        if source is None:
            raise PrivateWorkNotFound(context.request_id)
        target_ids = {message_id, *message_ids}
        selected = None
        state = self._state(
            context,
            resolved_app_config,
            as_node="branch",
        )
        for snapshot in await state.ahistory(
            checkpoint_config(source_thread_id),
            limit=_HISTORY_SCAN_LIMIT,
        ):
            if self._matches_branch_target(
                self._messages(snapshot),
                target_ids,
            ):
                selected = snapshot
                break
        checkpoint_id = snapshot_checkpoint_id(selected)
        if checkpoint_id is None:
            raise PrivateWorkConflict(context.request_id)
        selected_messages = self._messages(selected)
        target_indices = [index for index, message in enumerate(selected_messages) if self._message_id(message) in target_ids]
        first_target_index = min(target_indices) if target_indices else -1
        branch_human = next(
            (message for message in reversed(selected_messages[:first_target_index]) if self._is_visible_human(message)),
            None,
        )
        branch_human_id = self._message_id(branch_human)
        if branch_human_id is None:
            raise PrivateWorkConflict(context.request_id)
        replay_base = await self._find_checkpoint_before_message(
            state,
            source_thread_id,
            branch_human_id,
            context.request_id,
            head=selected,
        )
        replay_base_checkpoint_id = snapshot_checkpoint_id(replay_base)
        if replay_base_checkpoint_id is None:
            raise PrivateWorkConflict(context.request_id)
        target_thread_id = str(uuid.uuid4())
        record = await self._thread_service.branch(
            context,
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            checkpoint_id=checkpoint_id,
            replay_base_checkpoint_id=replay_base_checkpoint_id,
            expected_source_version=source.version,
            app_config=resolved_app_config,
            display_name=title or source.display_name,
        )
        return record, checkpoint_id

    async def prepare_regenerate(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        message_id: str,
        app_config: AppConfig | None = None,
    ) -> dict[str, Any]:
        context = require_issued_private_work_context(context)
        await self._validate_control_authority(
            context,
            thread_id,
            reject_incomplete_run=True,
        )
        state = self._state(
            context,
            app_config or get_app_config(),
            as_node="regenerate",
        )
        latest = await state.aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(latest) is None:
            raise PrivateWorkNotFound(context.request_id)
        messages = self._messages(latest)
        target_index = next(
            (index for index, message in enumerate(messages) if self._message_id(message) == message_id),
            None,
        )
        if target_index is None:
            raise PrivateWorkNotFound(context.request_id)
        target = messages[target_index]
        if not self._is_visible_ai(target):
            raise PrivateWorkConflict(context.request_id)
        latest_ai = next((message for message in reversed(messages) if self._is_visible_ai(message)), None)
        if self._message_id(latest_ai) != message_id:
            raise PrivateWorkConflict(context.request_id)
        human = next(
            (message for message in reversed(messages[:target_index]) if self._is_visible_human(message)),
            None,
        )
        human_id = self._message_id(human)
        if human is None or human_id is None:
            raise PrivateWorkConflict(context.request_id)
        base = await self._find_checkpoint_before_message(
            state,
            thread_id,
            human_id,
            context.request_id,
            head=latest,
        )
        target_run_id = await self._find_target_run_id(
            context,
            thread_id,
            message_id,
        )
        checkpoint = self._checkpoint_response(base, context.request_id)
        replay_human = self._clean_human_message(human)
        replay_human["id"] = self._replay_input_message_id(base, human_id)
        regenerate_input: dict[str, Any] = {"messages": [replay_human]}
        latest_title = self._channel_values(latest).get("title")
        if isinstance(latest_title, str) and latest_title:
            regenerate_input["title"] = latest_title
        return {
            "input": regenerate_input,
            "checkpoint": checkpoint,
            "metadata": {
                "regenerate_from_message_id": message_id,
                "regenerate_from_run_id": target_run_id,
                "regenerate_checkpoint_id": checkpoint["checkpoint_id"],
            },
            "target_run_id": target_run_id,
        }

    async def prepare_edit_regenerate(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        human_message_id: str,
        replacement_text: str,
        replacement_base_id: str | None = None,
        app_config: AppConfig | None = None,
    ) -> dict[str, Any]:
        """Prepare one strict edit replay without accepting client authority."""

        context = require_issued_private_work_context(context)
        normalized_text = replacement_text.strip()
        if not normalized_text:
            raise PrivateWorkConflict(context.request_id)
        await self._validate_control_authority(
            context,
            thread_id,
            reject_incomplete_run=True,
        )
        state = self._state(
            context,
            app_config or get_app_config(),
            as_node="edit_regenerate",
        )
        latest = await state.aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(latest) is None:
            raise PrivateWorkNotFound(context.request_id)
        latest_values = self._channel_values(latest)
        goal = latest_values.get("goal")
        if isinstance(goal, Mapping) and goal.get("status") == "active":
            raise PrivateWorkConflict(context.request_id)

        source_human, source_ai, source_message_ids = self._latest_editable_turn(
            self._messages(latest),
            human_message_id,
            context.request_id,
        )
        source_text = get_original_user_content_text(
            self._message_content(source_human),
            self._additional_kwargs(source_human),
        ).strip()
        if source_text == normalized_text:
            raise PrivateWorkConflict(context.request_id)
        source_human_id = self._message_id(source_human)
        source_ai_id = self._message_id(source_ai)
        if source_human_id is None or source_ai_id is None:
            raise PrivateWorkConflict(context.request_id)

        base = await self._find_checkpoint_before_message(
            state,
            thread_id,
            source_human_id,
            context.request_id,
            head=latest,
        )
        target_run_id = await self._find_target_run_id(
            context,
            thread_id,
            source_ai_id,
        )
        source_run = await self._require_successful_source_run(
            context,
            thread_id,
            target_run_id,
        )
        checkpoint = self._checkpoint_response(base, context.request_id)
        edit_messages, replacement_human_message_id = self._edit_replay_message_plan(
            base,
            source_human,
            replacement_text=normalized_text,
            replacement_base_id=(replacement_base_id or str(uuid.uuid4())),
        )
        edit_version_group_id = source_run.metadata.get("edit_version_group_id")
        if not isinstance(edit_version_group_id, str) or not edit_version_group_id:
            edit_version_group_id = source_human_id

        edit_input: dict[str, Any] = {"messages": edit_messages}
        base_title = self._channel_values(base).get("title")
        latest_title = latest_values.get("title")
        if isinstance(base_title, str) and base_title and isinstance(latest_title, str) and latest_title:
            edit_input["title"] = latest_title

        return {
            "input": edit_input,
            "checkpoint": checkpoint,
            "metadata": {
                "replay_kind": "edit",
                "regenerate_from_message_id": source_ai_id,
                "regenerate_from_run_id": target_run_id,
                "regenerate_checkpoint_id": checkpoint["checkpoint_id"],
                "edit_from_message_id": source_human_id,
                "edit_message_id": replacement_human_message_id,
                "edit_version_group_id": edit_version_group_id,
            },
            "target_run_id": target_run_id,
            "replacement_human_message_id": replacement_human_message_id,
            "source_message_ids": source_message_ids,
        }

    async def suggest(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        n: int,
        app_config: AppConfig,
    ) -> list[str]:
        context = require_issued_private_work_context(context)
        if not app_config.suggestions.enabled:
            return []
        snapshot = await self._state(
            context,
            app_config,
            as_node="suggest",
        ).aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(snapshot) is None:
            raise PrivateWorkNotFound(context.request_id)
        conversation = self._suggestion_conversation(self._messages(snapshot))
        if not conversation:
            return []
        resolved = await self._resolve_agent_authority(context, thread_id)
        runtime_config = app_config
        if self._model_materializer is not None:
            try:
                runtime_model = await self._model_materializer.materialize_active(
                    resolved.payload.model_ref,
                )
            except SystemModelMaterializationUnavailable:
                logger.warning(
                    "Project suggestion model is unavailable: request_id=%s",
                    context.request_id,
                )
                return []
            runtime_config = app_config.with_runtime_models((runtime_model,))
            model_name = runtime_model.name
        else:
            model_name = ConfiguredModelRefResolver(app_config).resolve(
                resolved.payload.model_ref,
            )
        if model_name is None:
            logger.warning(
                "Project suggestion model is unavailable: request_id=%s",
                context.request_id,
            )
            return []
        system_instruction = (
            "You are generating follow-up questions to help the user continue the conversation.\n"
            f"Based on the conversation below, produce EXACTLY {n} short questions the user might ask next.\n"
            "Requirements:\n"
            "- Questions must be relevant to the preceding conversation.\n"
            "- Questions must be written in the same language as the user.\n"
            "- Keep each question concise (ideally <= 20 words / <= 40 Chinese characters).\n"
            "- Do NOT include numbering, markdown, or any extra text.\n"
            "- Output MUST be a JSON array of strings only."
        )
        try:
            raw = await run_oneshot_llm(
                system_instruction=system_instruction,
                user_content=f"Conversation Context:\n{conversation}\n\nGenerate {n} follow-up questions",
                run_name="project_suggest_agent",
                app_config=runtime_config,
                model_name=model_name,
                profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
            )
        except Exception:
            logger.exception(
                "Project suggestion model call failed: request_id=%s",
                context.request_id,
            )
            return []
        suggestions = self._parse_json_string_list(raw) or []
        return [text.replace("\n", " ").strip() for text in suggestions if text.strip()][:n]

    async def _validate_control_authority(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        reject_incomplete_run: bool,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_thread(
                    session,
                    context,
                    thread_id,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    reject_incomplete_run=reject_incomplete_run,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _resolve_agent_authority(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> ResolvedAgentSnapshot:
        try:
            async with self._session_factory() as session, session.begin():
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                thread = await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    lock=True,
                )
                if thread is None:
                    raise PrivateWorkNotFound(context.request_id)
                resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
                    session,
                    current,
                    AssetSelection(AssetKind.AGENT, thread.agent_asset_id),
                )
                if type(resolved) is not ResolvedAgentSnapshot or resolved.scope.value != thread.agent_scope:
                    raise PrivateWorkAssetStale(context.request_id)
                await self._snapshots.validate_agent_closure_in_session(
                    session,
                    context,
                    resolved,
                )
                return resolved
        except PrivateWorkError:
            raise
        except (AssetForbidden, AssetValidationFailed, AssetResolutionUnavailable, RunSnapshotAssetStale):
            raise PrivateWorkAssetStale(context.request_id) from None
        except (AssetStorageUnavailable, DBAPIError):
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _lock_thread(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        *capabilities: Capability,
        reject_incomplete_run: bool,
    ) -> PrivateThreadRecord:
        await self._revalidator.require(
            session,
            context,
            *capabilities,
            lock=True,
        )
        thread = await PrivateThreadRepository(session).get(
            scope=context.resource_scope,
            thread_id=thread_id,
            lock=True,
        )
        if thread is None:
            raise PrivateWorkNotFound(context.request_id)
        if reject_incomplete_run:
            incomplete = (
                await session.execute(
                    select(RunRow.run_id)
                    .where(
                        RunRow.project_id == context.project_id,
                        RunRow.owner_user_id == str(context.user_id),
                        RunRow.thread_id == thread_id,
                        or_(
                            RunRow.status.in_(("pending", "running")),
                            RunRow.finalization_status == "finalizing",
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if incomplete is not None:
                raise PrivateWorkThreadBusy(context.request_id)
        return thread

    async def _find_checkpoint_before_message(
        self,
        state: Any,
        thread_id: str,
        message_id: str,
        request_id: str,
        *,
        head: object,
    ) -> Any:
        del thread_id
        try:
            return await find_settled_checkpoint_before_message(
                state,
                head,
                message_id,
                max_depth=_HISTORY_SCAN_LIMIT * 2,
            )
        except CheckpointLineageError:
            raise PrivateWorkConflict(request_id) from None

    async def _find_target_run_id(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        message_id: str,
    ) -> str:
        try:
            rows = await self._run_event_store.list_messages(
                thread_id,
                limit=_HISTORY_SCAN_LIMIT,
                scope=context.resource_scope,
            )
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None
        for row in reversed(rows):
            if row.get("event_type") not in {"ai_message", "llm.ai.response"}:
                continue
            if self._event_message_id(row) != message_id:
                continue
            run_id = row.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
        raise PrivateWorkConflict(context.request_id)

    async def _require_successful_source_run(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
    ) -> PrivateRunRecord:
        """Revalidate an edit source under the exact private coordinates."""

        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                thread = await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    lock=True,
                )
                if thread is None:
                    raise PrivateWorkNotFound(context.request_id)
                record = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=run_id,
                    lock=True,
                )
                if record is None or record.thread_id != thread_id or record.status != "success":
                    raise PrivateWorkConflict(context.request_id)
                return record
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    @classmethod
    def _suggestion_conversation(cls, messages: list[Any]) -> str:
        parts: list[str] = []
        total = 0
        visible = [message for message in messages if cls._is_visible_human(message) or cls._is_visible_ai(message)][-_SUGGESTION_MESSAGE_LIMIT:]
        for message in visible:
            content = message_to_text(message).strip()[:_SUGGESTION_MESSAGE_CHARS]
            if not content:
                continue
            role = "User" if cls._is_visible_human(message) else "Assistant"
            remaining = _SUGGESTION_TOTAL_CHARS - total
            if remaining <= 0:
                break
            line = f"{role}: {content}"[:remaining]
            parts.append(line)
            total += len(line)
        return "\n".join(parts)

    @staticmethod
    def _parse_json_string_list(text: str) -> list[str] | None:
        candidate = llm_text.strip_markdown_code_fence(llm_text.strip_think_blocks(text))
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(candidate[start : end + 1])
        except Exception:
            return None
        if not isinstance(payload, list):
            return None
        return [item.strip() for item in payload if isinstance(item, str) and item.strip()]

    @staticmethod
    def _checkpoint_config(thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }

    @staticmethod
    def _checkpoint_id(item: object | None) -> str | None:
        if item is None:
            return None
        config = getattr(item, "config", {}) or {}
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        value = configurable.get("checkpoint_id") if isinstance(configurable, Mapping) else None
        if isinstance(value, str) and value:
            return value
        checkpoint = getattr(item, "checkpoint", {}) or {}
        value = checkpoint.get("id") if isinstance(checkpoint, Mapping) else None
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _channel_values(item: object) -> dict[str, Any]:
        snapshot_values = getattr(item, "values", None)
        if isinstance(snapshot_values, Mapping):
            return dict(snapshot_values)
        return {}

    @classmethod
    def _messages(cls, item: object) -> list[Any]:
        messages = cls._channel_values(item).get("messages", [])
        return list(messages) if isinstance(messages, list) else []

    @classmethod
    def _replay_input_message_id(
        cls,
        base: object,
        visible_message_id: str,
    ) -> str:
        """Reuse the pre-injection ID already present in a replay checkpoint.

        Dynamic context replaces the first user message with a hidden reminder
        plus a visible ``<id>__user`` copy. The replay base is the settled input
        checkpoint immediately before that replacement, so it already contains
        the original ``<id>`` message. Reusing that ID lets the messages reducer
        replace the checkpointed input instead of appending the same turn twice.
        """

        suffix = "__user"
        if not visible_message_id.endswith(suffix):
            return visible_message_id
        pre_injection_id = visible_message_id[: -len(suffix)]
        if any(cls._is_visible_human(message) and cls._message_id(message) == pre_injection_id for message in cls._messages(base)):
            return pre_injection_id
        return visible_message_id

    @classmethod
    def _edit_replay_message_plan(
        cls,
        base: object,
        source_human: object,
        *,
        replacement_text: str,
        replacement_base_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        source_message_id = cls._message_id(source_human)
        if source_message_id is None:
            raise ValueError("source human message requires a stable id")
        pre_injection_id = cls._replay_input_message_id(
            base,
            source_message_id,
        )
        replacement = cls._clean_human_message_for_edit(
            source_human,
            replacement_id=replacement_base_id,
            replacement_text=replacement_text,
        )
        if pre_injection_id == source_message_id:
            return [replacement], replacement_base_id
        return (
            [
                {"type": "remove", "id": pre_injection_id},
                replacement,
            ],
            f"{replacement_base_id}__user",
        )

    @staticmethod
    def _message_id(message: object | None) -> str | None:
        if message is None:
            return None
        value = message.get("id") if isinstance(message, Mapping) else getattr(message, "id", None)
        return str(value) if value else None

    @staticmethod
    def _message_type(message: object | None) -> str | None:
        if message is None:
            return None
        if isinstance(message, Mapping):
            value = message.get("type") or message.get("role")
        else:
            value = getattr(message, "type", None)
        if value == "assistant":
            return "ai"
        if value == "user":
            return "human"
        return str(value) if value else None

    @staticmethod
    def _message_name(message: object) -> str | None:
        value = message.get("name") if isinstance(message, Mapping) else getattr(message, "name", None)
        return str(value) if value else None

    @staticmethod
    def _message_content(message: object) -> object:
        return message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)

    @staticmethod
    def _additional_kwargs(message: object) -> dict[str, Any]:
        if isinstance(message, Mapping):
            value = message.get("additional_kwargs")
        else:
            value = getattr(message, "additional_kwargs", None)
        return dict(value) if isinstance(value, Mapping) else {}

    @classmethod
    def _message_tool_calls(cls, message: object) -> list[Any]:
        value = message.get("tool_calls") if isinstance(message, Mapping) else getattr(message, "tool_calls", None)
        if not isinstance(value, list):
            value = cls._additional_kwargs(message).get("tool_calls")
        return list(value) if isinstance(value, list) else []

    @classmethod
    def _hidden(cls, message: object) -> bool:
        return cls._message_type(message) == "remove" or cls._message_name(message) == "summary" or cls._additional_kwargs(message).get("hide_from_ui") is True

    @classmethod
    def _is_visible_human(cls, message: object) -> bool:
        return cls._message_type(message) == "human" and not cls._hidden(message)

    @classmethod
    def _is_visible_ai(cls, message: object) -> bool:
        return cls._message_type(message) == "ai" and not cls._hidden(message)

    @classmethod
    def _latest_editable_turn(
        cls,
        messages: list[Any],
        human_message_id: str,
        request_id: str,
    ) -> tuple[Any, Any, list[str]]:
        latest_human_index = next(
            (index for index in range(len(messages) - 1, -1, -1) if cls._is_visible_human(messages[index])),
            None,
        )
        if latest_human_index is None or cls._message_id(messages[latest_human_index]) != human_message_id:
            raise PrivateWorkConflict(request_id)
        source_human = messages[latest_human_index]
        last_ai_index: int | None = None
        for index, message in enumerate(
            messages[latest_human_index + 1 :],
            start=latest_human_index + 1,
        ):
            if cls._is_visible_human(message):
                break
            if cls._is_visible_ai(message):
                last_ai_index = index
        if last_ai_index is None:
            raise PrivateWorkConflict(request_id)
        source_ai = messages[last_ai_index]
        if not message_to_text(source_ai).strip() or cls._message_tool_calls(source_ai):
            raise PrivateWorkConflict(request_id)
        source_message_ids = [message_id for message in messages[latest_human_index : last_ai_index + 1] if (message_id := cls._message_id(message)) is not None]
        return source_human, source_ai, source_message_ids

    @classmethod
    def _matches_branch_target(
        cls,
        messages: list[Any],
        target_ids: set[str],
    ) -> bool:
        if not target_ids:
            return False
        indices = {message_id: index for index, message in enumerate(messages) if (message_id := cls._message_id(message)) is not None}
        if not target_ids.issubset(indices):
            return False
        if any(not cls._is_visible_ai(messages[indices[message_id]]) for message_id in target_ids):
            return False
        target_end = max(indices[message_id] for message_id in target_ids)
        return not any(cls._is_visible_human(message) or cls._is_visible_ai(message) for message in messages[target_end + 1 :])

    @classmethod
    def _clean_human_message(cls, message: object) -> dict[str, Any]:
        additional_kwargs = cls._additional_kwargs(message)
        content = get_original_user_content_text(
            cls._message_content(message),
            additional_kwargs,
        )
        additional_kwargs.pop(ORIGINAL_USER_CONTENT_KEY, None)
        additional_kwargs.pop("hide_from_ui", None)
        clean: dict[str, Any] = {
            "type": "human",
            "content": [{"type": "text", "text": content}],
            "additional_kwargs": additional_kwargs,
        }
        if message_id := cls._message_id(message):
            clean["id"] = message_id
        if name := cls._message_name(message):
            clean["name"] = name
        return clean

    @classmethod
    def _clean_human_message_for_edit(
        cls,
        message: object,
        *,
        replacement_id: str,
        replacement_text: str,
    ) -> dict[str, Any]:
        source_kwargs = cls._additional_kwargs(message)
        additional_kwargs = {key: copy.deepcopy(source_kwargs[key]) for key in ("files", "referenced_message_contexts") if key in source_kwargs}
        clean: dict[str, Any] = {
            "type": "human",
            "id": replacement_id,
            "content": [{"type": "text", "text": replacement_text}],
            "additional_kwargs": additional_kwargs,
        }
        if name := cls._message_name(message):
            clean["name"] = name
        return clean

    @classmethod
    def _event_message_id(cls, row: dict[str, Any]) -> str | None:
        content = row.get("content")
        if isinstance(content, (BaseMessage, Mapping)):
            return cls._message_id(content)
        return None

    @classmethod
    def _checkpoint_response(
        cls,
        item: object,
        request_id: str,
    ) -> dict[str, Any]:
        config = getattr(item, "config", {}) or {}
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        checkpoint_id = configurable.get("checkpoint_id") if isinstance(configurable, Mapping) else None
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise PrivateWorkConflict(request_id)
        return {
            "checkpoint_ns": str(configurable.get("checkpoint_ns") or ""),
            "checkpoint_id": checkpoint_id,
            "checkpoint_map": configurable.get("checkpoint_map"),
        }

    def _saver(self, context: PrivateWorkContext) -> Any:
        return self._project_scoped_checkpointer.for_context(context)

    def _state(
        self,
        context: PrivateWorkContext,
        app_config: AppConfig,
        *,
        as_node: str,
    ) -> Any:
        return bind_scoped_checkpoint_state(
            self._project_scoped_checkpointer,
            context,
            app_config,
            as_node=as_node,
        )


__all__ = ["ProjectChatControlService"]
