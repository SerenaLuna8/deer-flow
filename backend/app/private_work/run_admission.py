from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.instance_identity import (
    normalize_channel_instance_id,
    persisted_channel_instance_id,
)
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
    strip_private_client_fields,
)
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkRunExecutionProfileUnsupported,
    PrivateWorkRunModelSelectionLocked,
    PrivateWorkRunModelUnavailable,
    PrivateWorkUnavailable,
)
from app.private_work.execution_profile import (
    RUN_EXECUTION_PROFILE_KWARG,
    RunExecutionProfileUnsupported,
    RunModelSelectionLocked,
    RunSelectedModelUnavailable,
    parse_persisted_run_execution_profile,
)
from app.private_work.human_input_response import HumanInputResponsePromotion
from app.private_work.inbound_dedupe import (
    PrivateRunInboundDelivery,
    ProjectInboundDeliveryRepository,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import PrivateRunConflict, PrivateRunCreate, PrivateRunRecord, PrivateRunRepository
from app.private_work.sandbox_files import (
    RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG,
    CurrentUploadSnapshotInvalid,
    required_current_upload_snapshot_from_run_kwargs,
)
from app.private_work.snapshot_repository import (
    RunAssetSnapshot,
    RunMcpGrantSnapshot,
    RunModelSnapshotAdmissionPort,
    RunRuntimePolicyAdmissionPort,
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from app.reliability.jobs import (
    AdmittedJobRecord,
    JobIdempotencyConflict,
    JobScope,
    PrivateRunJobRepository,
)
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.model_refs import ModelRefResolver
from app.shared_assets.models import AssetKind, AssetSelection, ResolvedRunAssetClosure
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.agents.memory.snip import MEMORY_ARCHIVE_RECEIPT_KEY
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.persistence.channel_connections import (
    ChannelConnectionRow,
    ChannelConversationRow,
    ProjectChannelInstanceRow,
)
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.trace_context import generate_trace_id, normalize_trace_id


@dataclass(frozen=True, slots=True)
class PersistedRunSnapshot:
    assets: tuple[RunAssetSnapshot, ...]
    mcp_grants: tuple[RunMcpGrantSnapshot, ...]
    catalog_generation: int


@dataclass(frozen=True, slots=True)
class PrivateRunInboundAuthority:
    """Exact persisted connection coordinates resolved for one inbound run."""

    connection_id: str
    provider: str
    external_account_id: str
    workspace_id: str | None
    external_conversation_id: str
    external_topic_id: str | None
    channel_instance_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "connection_id",
            "provider",
            "external_account_id",
            "external_conversation_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise TypeError(f"{name} must be a non-empty string")
        object.__setattr__(
            self,
            "channel_instance_id",
            normalize_channel_instance_id(
                self.provider,
                self.channel_instance_id,
            ),
        )
        for name in (
            "workspace_id",
            "external_topic_id",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")


@dataclass(frozen=True, slots=True)
class PrivateRunAdmissionServerContext:
    """Typed authority supplied only by an authenticated server caller."""

    non_interactive: bool = False
    inbound_authority: PrivateRunInboundAuthority | None = None
    inbound_delivery: PrivateRunInboundDelivery | None = None
    origin_trace_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.non_interactive) is not bool:
            raise TypeError("non_interactive must be a boolean")
        if self.inbound_authority is not None and type(self.inbound_authority) is not PrivateRunInboundAuthority:
            raise TypeError("inbound_authority must be PrivateRunInboundAuthority")
        if self.inbound_delivery is not None and type(self.inbound_delivery) is not PrivateRunInboundDelivery:
            raise TypeError(
                "inbound_delivery must be PrivateRunInboundDelivery",
            )
        if (self.inbound_authority is None) != (self.inbound_delivery is None):
            raise TypeError(
                "inbound_authority and inbound_delivery must be supplied together",
            )
        if self.origin_trace_id is not None:
            normalized = normalize_trace_id(self.origin_trace_id)
            if normalized is None:
                raise ValueError("origin_trace_id is invalid")
            object.__setattr__(self, "origin_trace_id", normalized)


@dataclass(frozen=True, slots=True)
class AdmittedPrivateRun:
    run: PrivateRunRecord
    job: AdmittedJobRecord
    snapshot: PersistedRunSnapshot
    opaque_runtime_scope: PrivateResourceScope
    inbound_delivery_replay: bool = False

    @property
    def thread_id(self) -> str:
        return self.run.thread_id


class PrivateRunAdmissionQuotaPort(Protocol):
    async def reserve_concurrent_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
    ) -> None: ...


class PrivateRunAdmissionAuditPort(Protocol):
    async def run_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None: ...


class PrivateRunHumanInputResponsePromoter(Protocol):
    async def promote(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        kwargs: Mapping[str, object],
    ) -> HumanInputResponsePromotion: ...


def _matches_server_promoted_human_input_retry(
    persisted_kwargs: Mapping[str, object],
    retry_kwargs: Mapping[str, object],
) -> bool:
    """Ignore only the visibility bit added by a prior trusted admission."""

    persisted_input = persisted_kwargs.get("input")
    retry_input = retry_kwargs.get("input")
    if not isinstance(persisted_input, Mapping) or not isinstance(retry_input, Mapping):
        return False
    persisted_messages = persisted_input.get("messages")
    retry_messages = retry_input.get("messages")
    if not isinstance(persisted_messages, list) or len(persisted_messages) != 1:
        return False
    if not isinstance(retry_messages, list) or len(retry_messages) != 1:
        return False
    persisted_message = persisted_messages[0]
    retry_message = retry_messages[0]
    if not isinstance(persisted_message, Mapping) or not isinstance(retry_message, Mapping):
        return False
    persisted_additional = persisted_message.get("additional_kwargs")
    retry_additional = retry_message.get("additional_kwargs")
    if not isinstance(persisted_additional, Mapping) or not isinstance(retry_additional, Mapping):
        return False
    if set(persisted_additional) != {"hide_from_ui", "human_input_response"}:
        return False
    if persisted_additional.get("hide_from_ui") is not True or set(retry_additional) != {"human_input_response"}:
        return False
    if persisted_additional.get("human_input_response") != retry_additional.get("human_input_response"):
        return False

    normalized = copy.deepcopy(dict(persisted_kwargs))
    normalized_input = normalized.get("input")
    if not isinstance(normalized_input, dict):
        return False
    normalized_messages = normalized_input.get("messages")
    if not isinstance(normalized_messages, list) or len(normalized_messages) != 1:
        return False
    normalized_message = normalized_messages[0]
    if not isinstance(normalized_message, dict):
        return False
    normalized_additional = normalized_message.get("additional_kwargs")
    if not isinstance(normalized_additional, dict):
        return False
    normalized_additional.pop("hide_from_ui")
    return normalized == retry_kwargs


def _strip_client_memory_archive_receipt(
    payload: object,
    *,
    command: bool,
) -> object:
    """Drop only graph-state receipt slots, preserving message/tool payloads."""

    cloned = copy.deepcopy(payload)
    if not isinstance(cloned, dict):
        return cloned
    cloned.pop(MEMORY_ARCHIVE_RECEIPT_KEY, None)
    if command:
        update = cloned.get("update")
        if isinstance(update, Mapping):
            clean_update = dict(update)
            clean_update.pop(MEMORY_ARCHIVE_RECEIPT_KEY, None)
            cloned["update"] = clean_update
    return cloned


class _NoopPrivateRunAdmissionQuota:
    async def reserve_concurrent_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
    ) -> None:
        del session, context, run


class _NoopPrivateRunAdmissionAudit:
    async def run_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None:
        del session, context, run, job


class PrivateRunAdmissionService:
    """Admit one project-private run under the frozen database lock order."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resolver: ProjectAssetResolver | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
        snapshots: RunSnapshotRepository | None = None,
        model_ref_resolver: ModelRefResolver | None = None,
        model_catalog: RunModelSnapshotAdmissionPort | None = None,
        runtime_policy: RunRuntimePolicyAdmissionPort | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
        quota: PrivateRunAdmissionQuotaPort | None = None,
        audit: PrivateRunAdmissionAuditPort | None = None,
        human_input_response_promoter: PrivateRunHumanInputResponsePromoter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._snapshots = snapshots or RunSnapshotRepository(
            session_factory,
            model_ref_resolver=model_ref_resolver,
            model_catalog=model_catalog,
            runtime_policy=runtime_policy,
            endpoint_policy=endpoint_policy,
        )
        self._quota = quota or _NoopPrivateRunAdmissionQuota()
        self._audit = audit or _NoopPrivateRunAdmissionAudit()
        self._human_input_response_promoter = human_input_response_promoter

    async def _persisted_snapshot(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
    ) -> PersistedRunSnapshot:
        assets = await self._snapshots.list_assets_in_session(
            session,
            context,
            run_id,
            lock=True,
        )
        grants = await self._snapshots.list_mcp_grants_in_session(
            session,
            context,
            run_id,
            lock=True,
        )
        if not assets or assets[0].asset_kind != AssetKind.AGENT.value:
            raise RunSnapshotAssetStale
        generations = {asset.catalog_generation for asset in assets}
        if len(generations) != 1:
            raise RunSnapshotAssetStale
        return PersistedRunSnapshot(
            assets=assets,
            mcp_grants=grants,
            catalog_generation=generations.pop(),
        )

    @staticmethod
    async def _require_inbound_authority(
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        server_context: PrivateRunAdmissionServerContext | None,
    ) -> tuple[uuid.UUID, str] | None:
        authority = server_context.inbound_authority if server_context is not None else None
        if authority is None:
            return None

        persisted_instance_id = persisted_channel_instance_id(
            authority.provider,
            authority.channel_instance_id,
        )
        if persisted_instance_id is not None:
            try:
                exact_instance_id = uuid.UUID(persisted_instance_id)
            except (TypeError, ValueError):
                raise PrivateWorkNotFound(context.request_id) from None
            instance = (
                await session.execute(
                    select(ProjectChannelInstanceRow.id)
                    .where(
                        ProjectChannelInstanceRow.id == exact_instance_id,
                        ProjectChannelInstanceRow.project_id == context.project_id,
                        ProjectChannelInstanceRow.provider == authority.provider,
                        ProjectChannelInstanceRow.desired_status == "enabled",
                        ProjectChannelInstanceRow.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if instance is None:
                raise PrivateWorkNotFound(context.request_id)

        connection_metadata = (
            await session.execute(
                select(ChannelConnectionRow.metadata_json)
                .where(
                    ChannelConnectionRow.id == authority.connection_id,
                    ChannelConnectionRow.project_id == context.project_id,
                    ChannelConnectionRow.owner_user_id == str(context.user_id),
                    ChannelConnectionRow.provider == authority.provider,
                    ChannelConnectionRow.channel_instance_id == (exact_instance_id if persisted_instance_id is not None else None),
                    ChannelConnectionRow.external_account_id == authority.external_account_id,
                    ChannelConnectionRow.workspace_id == (authority.workspace_id or ""),
                    ChannelConnectionRow.status == "connected",
                    ChannelConnectionRow.frozen_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not isinstance(connection_metadata, Mapping):
            raise PrivateWorkNotFound(context.request_id)
        agent_scope = connection_metadata.get("agent_scope")
        if agent_scope not in {"project", "system"}:
            raise PrivateWorkNotFound(context.request_id)
        try:
            agent_asset_id = uuid.UUID(str(connection_metadata["agent_asset_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PrivateWorkNotFound(context.request_id) from None

        conversation = (
            await session.execute(
                select(ChannelConversationRow.id)
                .where(
                    ChannelConversationRow.project_id == context.project_id,
                    ChannelConversationRow.owner_user_id == str(context.user_id),
                    ChannelConversationRow.connection_id == authority.connection_id,
                    ChannelConversationRow.provider == authority.provider,
                    ChannelConversationRow.external_conversation_id == authority.external_conversation_id,
                    ChannelConversationRow.external_topic_id == (authority.external_topic_id or ""),
                    ChannelConversationRow.thread_id == thread_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise PrivateWorkNotFound(context.request_id)
        return agent_asset_id, agent_scope

    @staticmethod
    def _is_same_request(
        run: PrivateRunRecord,
        *,
        thread_id: str,
        request: PrivateRunCreate,
    ) -> bool:
        persisted_kwargs = dict(run.kwargs)
        persisted_profile = persisted_kwargs.pop(
            RUN_EXECUTION_PROFILE_KWARG,
            None,
        )
        try:
            required_current_upload_snapshot_from_run_kwargs(run.kwargs)
        except CurrentUploadSnapshotInvalid:
            return False
        persisted_kwargs.pop(RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG, None)
        if persisted_profile is None:
            profile_match = request.execution_profile.as_dict() == {
                "model_name": None,
                "thinking_enabled": None,
                "reasoning_effort": None,
            }
        else:
            try:
                persisted_requested, _ = parse_persisted_run_execution_profile(
                    persisted_profile,
                )
                profile_match = persisted_requested == request.execution_profile
            except RunExecutionProfileUnsupported:
                profile_match = False
        kwargs_match = persisted_kwargs == request.kwargs or _matches_server_promoted_human_input_retry(
            persisted_kwargs,
            request.kwargs,
        )
        return run.thread_id == thread_id and run.multitask_strategy == request.multitask_strategy and run.metadata == request.metadata and kwargs_match and profile_match

    @staticmethod
    def _server_kwargs(
        kwargs: dict[str, object],
        server_context: PrivateRunAdmissionServerContext | None,
    ) -> dict[str, object]:
        if server_context is None:
            return kwargs
        if type(server_context) is not PrivateRunAdmissionServerContext:
            raise TypeError("PrivateRunAdmissionServerContext is required")
        if not server_context.non_interactive:
            return kwargs
        result = dict(kwargs)
        config_value = result.get("config")
        config = dict(config_value) if isinstance(config_value, dict) else {}
        context_value = config.get("context")
        runtime_context = dict(context_value) if isinstance(context_value, dict) else {}
        runtime_context["non_interactive"] = True
        config["context"] = runtime_context
        result["config"] = config
        return result

    async def admit(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        request: PrivateRunCreate,
        *,
        server_context: PrivateRunAdmissionServerContext | None = None,
    ) -> AdmittedPrivateRun:
        context = require_issued_private_work_context(context)
        if type(request) is not PrivateRunCreate or not isinstance(thread_id, str) or not thread_id or request.multitask_strategy != "reject":
            raise PrivateWorkConflict(context.request_id)
        safe_kwargs = strip_private_client_fields(request.kwargs)
        # LangGraph state and Command payloads may legitimately contain keys
        # such as ``role``, ``user_id``, or ``project_id`` as message/tool
        # data.  They are never consulted as application authority; the
        # Worker injects its immutable private scope separately.  Preserve
        # their exact shape while continuing to recursively sanitize runtime
        # config/context and metadata.
        for graph_payload in ("input", "command"):
            if graph_payload in request.kwargs:
                safe_kwargs[graph_payload] = _strip_client_memory_archive_receipt(
                    request.kwargs[graph_payload],
                    command=graph_payload == "command",
                )
        server_request = replace(
            request,
            assistant_id=None,
            status="pending",
            multitask_strategy="reject",
            metadata=strip_private_client_fields(request.metadata),
            kwargs=self._server_kwargs(safe_kwargs, server_context),
            model_name=None,
            follow_up_to_run_id=None,
            origin_trace_id=(server_context.origin_trace_id if server_context is not None and server_context.origin_trace_id is not None else normalize_trace_id(context.request_id) or generate_trace_id()),
        )
        try:
            async with self._session_factory() as session, session.begin():
                # The revalidator issues the project then membership FOR UPDATE
                # statements.  Every lock below is subordinate to that scope.
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                inbound_agent = await self._require_inbound_authority(
                    session,
                    context,
                    thread_id,
                    server_context,
                )
                thread = await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    lock=True,
                )
                if thread is None:
                    raise PrivateWorkNotFound(context.request_id)
                if inbound_agent is not None and (thread.agent_asset_id != inbound_agent[0] or thread.agent_scope != inbound_agent[1]):
                    raise PrivateWorkNotFound(context.request_id)

                runs = PrivateRunRepository(session)
                jobs = PrivateRunJobRepository(session)
                inbound_deliveries = ProjectInboundDeliveryRepository(session)
                job_scope = JobScope(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                )
                inbound_authority = server_context.inbound_authority if server_context is not None else None
                inbound_delivery = server_context.inbound_delivery if server_context is not None else None
                if inbound_authority is not None and inbound_delivery is not None:
                    replay = await inbound_deliveries.get(
                        scope=context.resource_scope,
                        connection_id=inbound_authority.connection_id,
                        provider=inbound_authority.provider,
                        external_conversation_id=(inbound_authority.external_conversation_id),
                        external_topic_id=(inbound_authority.external_topic_id),
                        delivery=inbound_delivery,
                        lock=True,
                    )
                    if replay is not None:
                        replay_run = await runs.get(
                            scope=context.resource_scope,
                            run_id=replay.run_id,
                            lock=True,
                        )
                        if replay_run is None or replay_run.job_id is None:
                            raise PrivateWorkConflict(context.request_id)
                        replay_job = await jobs.get(
                            scope=job_scope,
                            run_id=replay_run.run_id,
                            job_id=replay_run.job_id,
                            lock=True,
                        )
                        if replay_job is None or replay_run.origin_trace_id != replay_job.origin_trace_id:
                            raise PrivateWorkConflict(context.request_id)
                        return AdmittedPrivateRun(
                            run=replay_run,
                            snapshot=await self._persisted_snapshot(
                                session,
                                context,
                                replay_run.run_id,
                            ),
                            opaque_runtime_scope=context.resource_scope,
                            job=replay_job,
                            inbound_delivery_replay=True,
                        )
                existing = await runs.get(
                    scope=context.resource_scope,
                    run_id=server_request.run_id,
                )
                if existing is not None:
                    if (
                        not self._is_same_request(
                            existing,
                            thread_id=thread_id,
                            request=server_request,
                        )
                        or existing.job_id is None
                    ):
                        raise PrivateWorkConflict(context.request_id)
                    existing_job = await jobs.get(
                        scope=job_scope,
                        run_id=existing.run_id,
                        job_id=existing.job_id,
                        lock=True,
                    )
                    locked_existing = await runs.get(
                        scope=context.resource_scope,
                        run_id=server_request.run_id,
                        lock=True,
                    )
                    if (
                        existing_job is None
                        or locked_existing is None
                        or locked_existing.job_id != existing_job.job_id
                        or locked_existing.origin_trace_id != existing_job.origin_trace_id
                        or not self._is_same_request(
                            locked_existing,
                            thread_id=thread_id,
                            request=server_request,
                        )
                    ):
                        raise PrivateWorkConflict(context.request_id)
                    if inbound_authority is not None and inbound_delivery is not None:
                        await inbound_deliveries.bind(
                            scope=context.resource_scope,
                            connection_id=inbound_authority.connection_id,
                            provider=inbound_authority.provider,
                            external_conversation_id=(inbound_authority.external_conversation_id),
                            external_topic_id=(inbound_authority.external_topic_id),
                            thread_id=thread_id,
                            delivery=inbound_delivery,
                            run_id=locked_existing.run_id,
                        )
                    return AdmittedPrivateRun(
                        run=locked_existing,
                        snapshot=await self._persisted_snapshot(
                            session,
                            context,
                            locked_existing.run_id,
                        ),
                        opaque_runtime_scope=context.resource_scope,
                        job=existing_job,
                    )
                if await runs.has_conflicting_active_run(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                ):
                    raise PrivateWorkConflict(context.request_id)

                # Keep this after the Thread lock and active-Run rejection.
                # Worker checkpoint writes take the same Thread lock, so the
                # open-request read and this admission are one serialized
                # decision rather than a client-owned visibility assertion.
                continuation_source_run_id: str | None = None
                if self._human_input_response_promoter is not None:
                    promotion = await self._human_input_response_promoter.promote(
                        session,
                        context,
                        thread_id,
                        server_request.kwargs,
                    )
                    if type(promotion) is not HumanInputResponsePromotion:
                        raise PrivateWorkConflict(context.request_id)
                    if promotion.continuation_verified and promotion.continuation_run_id is None:
                        raise PrivateWorkConflict(context.request_id)
                    if promotion.continuation_run_id is not None and not promotion.continuation_verified:
                        raise PrivateWorkConflict(context.request_id)
                    try:
                        server_request = replace(
                            server_request,
                            kwargs=promotion.kwargs,
                            follow_up_to_run_id=promotion.continuation_run_id,
                        )
                    except ValueError:
                        raise PrivateWorkConflict(context.request_id) from None
                    continuation_source_run_id = promotion.continuation_run_id

                resolved = await self._resolver.resolve_run_asset_closure_in_session(
                    session,
                    current,
                    AssetSelection(AssetKind.AGENT, thread.agent_asset_id),
                )
                if type(resolved) is not ResolvedRunAssetClosure or resolved.lead_agent.scope.value != thread.agent_scope:
                    raise RunSnapshotAssetStale

                run = await self._snapshots.create_run_with_snapshot_in_session(
                    session,
                    context,
                    thread_id,
                    server_request,
                    resolved,
                    continuation_source_run_id=continuation_source_run_id,
                )
                job = await jobs.enqueue(
                    scope=job_scope,
                    run_id=run.run_id,
                    origin_trace_id=run.origin_trace_id,
                )
                run = await runs.attach_job(
                    scope=context.resource_scope,
                    run_id=run.run_id,
                    job_id=job.job_id,
                )
                if inbound_authority is not None and inbound_delivery is not None:
                    await inbound_deliveries.bind(
                        scope=context.resource_scope,
                        connection_id=inbound_authority.connection_id,
                        provider=inbound_authority.provider,
                        external_conversation_id=(inbound_authority.external_conversation_id),
                        external_topic_id=(inbound_authority.external_topic_id),
                        thread_id=thread_id,
                        delivery=inbound_delivery,
                        run_id=run.run_id,
                    )
                await self._quota.reserve_concurrent_run(session, context, run)
                await self._audit.run_admitted(session, context, run, job)
                snapshot = await self._persisted_snapshot(
                    session,
                    context,
                    run.run_id,
                )
                if snapshot.catalog_generation != resolved.lead_agent.catalog_generation:
                    raise RunSnapshotAssetStale
                return AdmittedPrivateRun(
                    run=run,
                    snapshot=snapshot,
                    opaque_runtime_scope=context.resource_scope,
                    job=job,
                )
        except (RunSnapshotAssetStale, AssetResolutionUnavailable):
            raise PrivateWorkAssetStale(context.request_id) from None
        except RunModelSelectionLocked:
            raise PrivateWorkRunModelSelectionLocked(context.request_id) from None
        except RunSelectedModelUnavailable:
            raise PrivateWorkRunModelUnavailable(context.request_id) from None
        except RunExecutionProfileUnsupported:
            raise PrivateWorkRunExecutionProfileUnsupported(context.request_id) from None
        except AssetForbidden:
            raise PrivateWorkForbidden(context.request_id) from None
        except AssetValidationFailed:
            raise PrivateWorkConflict(context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except (JobIdempotencyConflict, PrivateRunConflict):
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(context.request_id) from None
        except IntegrityError:
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
