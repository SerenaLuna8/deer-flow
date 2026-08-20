from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkError,
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
    persisted_run_execution_profile,
    resolve_admitted_run_execution_profile,
    selected_run_model_ref,
)
from app.private_work.memory_injection import (
    MemoryInjectionCandidate,
    assess_memory_injection,
    memory_injection_disabled_reason,
)
from app.private_work.run_repository import (
    PrivateRunConflict,
    PrivateRunCreate,
    PrivateRunRecord,
    PrivateRunRepository,
)
from app.private_work.sandbox_files import (
    RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG,
    CurrentUploadSnapshotInvalid,
    admit_current_upload_snapshot,
    persisted_current_upload_snapshot,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.shared_assets.agent_payload_checksum import (
    agent_payload_checksum_matches,
    persisted_agent_payload_checksum_matches,
)
from app.shared_assets.catalog_state_repository import CatalogStateRepository
from app.shared_assets.credential_closure import (
    LockedMcpCredentialClosure,
    McpCredentialClosureInvalid,
    McpCredentialClosureTarget,
    lock_mcp_credential_closures,
)
from app.shared_assets.model_refs import ExactModelRefResolver, ModelRefResolver
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedRunAssetClosure,
    SkillAssetRef,
)
from app.shared_assets.run_snapshot_codec import encode_run_asset_snapshot
from app.shared_assets.skill_credential_closure import (
    AdmittedSkillCredentialReference,
    LockedSkillCredentialClosure,
    LockedSkillCredentialMaterial,
    SkillCredentialClosureInvalid,
    SkillCredentialClosureTarget,
    lock_admitted_skill_credential_materials,
    lock_skill_credential_closures,
)
from app.system_runtime_settings.models import (
    LockedAgentRuntimePolicy,
    auxiliary_model_snapshot_ref,
)
from app.system_runtime_settings.repository import (
    SystemRuntimePolicyRepositoryInvariant,
)
from app.system_settings.errors import (
    SystemModelConflict,
    SystemModelInvalid,
    SystemModelNotFound,
    SystemModelStorageUnavailable,
)
from app.system_settings.validation import (
    ModelSettingsInvalid,
    validate_model_settings,
)
from deerflow.config.agents_config import AgentModelSettings
from deerflow.mcp_definition_policy import (
    McpDefinitionPolicyError,
    McpEndpointPolicy,
    validate_project_mcp_definition,
)
from deerflow.memory_contract import validate_memory_document_sections
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.model import (
    RunAssetVersionRow,
    RunMcpGrantSnapshotRow,
    RunSkillCredentialSnapshotRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets.agent_model import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
)
from deerflow.persistence.shared_assets.mcp_model import McpServerRow, McpServerVersionRow
from deerflow.persistence.shared_assets.skill_model import SkillRow, SkillVersionRow

_FORBIDDEN_PERSISTED_KEY_PARTS = (
    "secret",
    "envelope",
    "key_id",
    "nonce",
    "ciphertext",
    "storage_locator",
)


def agent_model_snapshot_purpose(version_id: uuid.UUID) -> str:
    """Return the stable Run-model purpose for one delegated Agent version."""

    if not isinstance(version_id, uuid.UUID):
        raise TypeError("Agent version_id must be a UUID")
    return f"agent.{version_id.hex}"


@dataclass(frozen=True, slots=True)
class RunAssetSnapshot:
    asset_kind: str
    dependency_order: int
    asset_scope: str
    asset_id: uuid.UUID
    version_id: uuid.UUID
    payload_checksum: str
    catalog_generation: int
    snapshot_json: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RunMcpGrantSnapshot:
    mcp_version_id: uuid.UUID
    credential_slot_id: uuid.UUID
    credential_grant_id: uuid.UUID
    credential_version_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RunSkillCredentialSnapshot:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    secret_name: str
    source_env_field_name: str
    skill_credential_binding_id: uuid.UUID
    binding_revision: int
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID


class RunSnapshotAssetStale(Exception):
    """Internal stale marker remapped at the request-context boundary."""


class AdmittedRunModelSnapshot(Protocol):
    """Minimum secret-free result required by Run admission."""

    model_ref: str
    provider_adapter: str
    provider_settings: Mapping[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool


class RunModelSnapshotAdmissionPort(Protocol):
    """Persist one exact database-backed model closure in the caller transaction."""

    async def admit_model_snapshot(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        purpose: str,
        model_ref: str,
    ) -> AdmittedRunModelSnapshot: ...


class RunRuntimePolicyAdmissionPort(Protocol):
    """Lock and persist the exact agent runtime policy in the caller transaction."""

    async def lock_agent_runtime_for_admission(
        self,
        session: AsyncSession,
    ) -> LockedAgentRuntimePolicy: ...

    async def admit_run_snapshot(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        locked_policy: LockedAgentRuntimePolicy | None = None,
    ) -> object: ...


def _apply_runtime_recursion_limit(
    request: PrivateRunCreate,
    policy: LockedAgentRuntimePolicy,
) -> PrivateRunCreate:
    kwargs = dict(request.kwargs)
    raw_config = kwargs.get("config")
    config = dict(raw_config) if isinstance(raw_config, Mapping) else {}
    requested = config.get("recursion_limit", 100)
    if type(requested) is not int or requested <= 0:
        requested = 100
    config["recursion_limit"] = min(
        requested,
        policy.value.max_recursion_limit,
    )
    kwargs["config"] = config
    return replace(request, kwargs=kwargs)


def _reject_secret_bearing_keys(value: object, request_id: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_PERSISTED_KEY_PARTS):
                raise PrivateWorkConflict(request_id)
            _reject_secret_bearing_keys(item, request_id)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_bearing_keys(item, request_id)


class RunSnapshotRepository:
    """Atomically persist a private run and its exact, secret-free asset closure."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        model_ref_resolver: ModelRefResolver | None = None,
        model_catalog: RunModelSnapshotAdmissionPort | None = None,
        runtime_policy: RunRuntimePolicyAdmissionPort | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
        personalization_repository_builder=AccountPersonalizationRepository,
        audit=None,
    ) -> None:
        self._session_factory = session_factory
        self._model_ref_resolver = model_ref_resolver or ExactModelRefResolver()
        self._model_catalog = model_catalog
        self._runtime_policy = runtime_policy
        self._endpoint_policy = endpoint_policy
        if not callable(personalization_repository_builder):
            raise TypeError("personalization repository builder must be callable")
        self._personalization_repository_builder = personalization_repository_builder
        if audit is not None and not callable(getattr(audit, "memory_injection_skipped", None)):
            raise TypeError("Run snapshot audit port is invalid")
        self._audit = audit

    async def _skip_memory_injection(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
    ) -> None:
        """Degrade over-budget injection to a skip; never block admission."""

        if self._audit is not None:
            await self._audit.memory_injection_skipped(
                session,
                project_id=context.project_id,
                run_id=run_id,
                request_id=context.request_id,
            )

    async def _admit_memory_context_snapshot(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        run_id: str,
        locked_policy: LockedAgentRuntimePolicy,
        thread_id: str | None = None,
        continuation_source_run_id: str | None = None,
    ) -> None:
        """Freeze one complete document in the caller's Run transaction."""

        if not isinstance(locked_policy, LockedAgentRuntimePolicy):
            raise RunSnapshotAssetStale
        memory = locked_policy.value.memory
        if continuation_source_run_id is not None:
            if not isinstance(thread_id, str) or not thread_id or not isinstance(continuation_source_run_id, str) or not continuation_source_run_id or len(continuation_source_run_id) > 64 or continuation_source_run_id == run_id:
                raise PrivateWorkConflict(context.request_id)
            # Hold the live preference row even though its enabled bit does
            # not change the inherited snapshot.  This preserves the global
            # preference-before-Memory lock order and serializes reset.
            await self._personalization_repository_builder(session).read_memory(
                str(context.user_id),
                for_update=True,
            )
            source_run = (
                await session.execute(
                    select(RunRow.run_id).where(
                        RunRow.project_id == context.project_id,
                        RunRow.owner_user_id == str(context.user_id),
                        RunRow.thread_id == thread_id,
                        RunRow.run_id == continuation_source_run_id,
                        RunRow.status != "deleted",
                    )
                )
            ).scalar_one_or_none()
            if source_run is None:
                raise PrivateWorkConflict(context.request_id)
            source_snapshot = (
                await session.execute(
                    select(RunMemoryContextSnapshotRow)
                    .where(
                        RunMemoryContextSnapshotRow.project_id == context.project_id,
                        RunMemoryContextSnapshotRow.owner_user_id == str(context.user_id),
                        RunMemoryContextSnapshotRow.run_id == continuation_source_run_id,
                        RunMemoryContextSnapshotRow.namespace == "default",
                    )
                    .with_for_update(of=RunMemoryContextSnapshotRow)
                )
            ).scalar_one_or_none()
            # No source row is itself a frozen result.  Falling back to the
            # current document here would mix two logical-task snapshots.
            if source_snapshot is None:
                return
            try:
                assessment = assess_memory_injection(
                    # Continuations intentionally inherit the source Run's
                    # frozen result. Current switches do not rewrite that
                    # logical-task decision.
                    platform_enabled=True,
                    account_enabled=True,
                    max_injection_tokens=memory.max_injection_tokens,
                    candidate=MemoryInjectionCandidate(
                        content=source_snapshot.content,
                        content_digest=source_snapshot.content_digest,
                        sections=source_snapshot.sections,
                    ),
                )
                source_sections = validate_memory_document_sections(
                    source_snapshot.sections,
                )
            except (TypeError, ValueError):
                raise PrivateWorkConflict(context.request_id) from None
            if assessment.status == "skipped_over_budget":
                await self._skip_memory_injection(session, context, run_id)
                return
            session.add(
                RunMemoryContextSnapshotRow(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    run_id=run_id,
                    namespace="default",
                    document_version=int(source_snapshot.document_version),
                    content=source_snapshot.content,
                    content_digest=source_snapshot.content_digest,
                    sections=list(source_sections),
                )
            )
            return
        if (
            memory_injection_disabled_reason(
                platform_enabled=memory.enabled,
                account_enabled=True,
            )
            is not None
        ):
            return
        preference = await self._personalization_repository_builder(session).read_memory(
            str(context.user_id),
            for_update=True,
        )
        if (
            memory_injection_disabled_reason(
                platform_enabled=memory.enabled,
                account_enabled=preference.memory_enabled,
            )
            is not None
        ):
            return
        document = (
            await session.execute(
                select(MemoryDocumentRow)
                .where(
                    MemoryDocumentRow.project_id == context.project_id,
                    MemoryDocumentRow.owner_user_id == str(context.user_id),
                    MemoryDocumentRow.namespace == "default",
                )
                .with_for_update(of=MemoryDocumentRow)
            )
        ).scalar_one_or_none()
        candidate = None
        if document is not None and int(document.version) >= 1:
            candidate = MemoryInjectionCandidate(
                content=document.content,
                content_digest=document.content_digest,
                sections=document.sections,
            )
        try:
            assessment = assess_memory_injection(
                platform_enabled=memory.enabled,
                account_enabled=preference.memory_enabled,
                max_injection_tokens=memory.max_injection_tokens,
                candidate=candidate,
            )
            if assessment.status == "inactive":
                return
            if document is None:
                raise ValueError("Memory document assessment is inconsistent")
            document_sections = validate_memory_document_sections(
                document.sections,
            )
        except (TypeError, ValueError):
            raise PrivateWorkConflict(context.request_id) from None
        if assessment.status == "skipped_over_budget":
            await self._skip_memory_injection(session, context, run_id)
            return
        session.add(
            RunMemoryContextSnapshotRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                run_id=run_id,
                namespace="default",
                document_version=int(document.version),
                content=document.content,
                content_digest=document.content_digest,
                sections=list(document_sections),
            )
        )

    @staticmethod
    def _asset_allowed(
        *,
        asset_scope: str,
        asset_project_id: uuid.UUID | None,
        project_id: uuid.UUID,
    ) -> bool:
        return (asset_scope == AssetScope.SYSTEM.value and asset_project_id is None) or (asset_scope == AssetScope.PROJECT.value and asset_project_id == project_id)

    @staticmethod
    async def _agent(
        session: AsyncSession,
        snapshot: ResolvedAgentSnapshot,
        project_id: uuid.UUID,
    ) -> tuple[AgentRow, AgentVersionRow]:
        row = (
            await session.execute(
                select(AgentRow, AgentVersionRow)
                .join(AgentVersionRow, AgentVersionRow.agent_id == AgentRow.id)
                .where(
                    AgentRow.id == snapshot.asset_id,
                    AgentVersionRow.id == snapshot.version_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise RunSnapshotAssetStale
        asset, version = row
        try:
            persisted_payload = replace(
                snapshot.payload,
                description=version.description,
                agents_instructions=version.agents_instructions,
                soul=version.soul,
                identity=version.identity,
                user_context=version.user_context,
                model_ref=version.model_ref,
                model_settings=AgentModelSettings.model_validate(
                    {} if version.model_settings is None else version.model_settings,
                ),
                tool_groups=tuple(version.tool_groups),
                payload_schema_version=version.payload_schema_version,
            )
        except (AttributeError, TypeError, ValueError):
            raise RunSnapshotAssetStale from None
        runtime_payload = replace(persisted_payload, payload_schema_version=4) if version.payload_schema_version in (1, 2, 3) else persisted_payload
        if (
            asset.scope != snapshot.scope.value
            or asset.status != "active"
            or asset.current_version_id != version.id
            or runtime_payload != snapshot.payload
            or not persisted_agent_payload_checksum_matches(
                persisted_payload,
                version.payload_checksum,
            )
            or not agent_payload_checksum_matches(
                runtime_payload,
                snapshot.checksum,
            )
            or (asset.scope == AssetScope.SYSTEM.value and version.version_number != 1)
            or not RunSnapshotRepository._asset_allowed(
                asset_scope=asset.scope,
                asset_project_id=asset.project_id,
                project_id=project_id,
            )
        ):
            raise RunSnapshotAssetStale
        return asset, version

    @staticmethod
    async def _skills(
        session: AsyncSession,
        version_ids: tuple[uuid.UUID, ...],
        project_id: uuid.UUID,
    ) -> list[tuple[SkillRow, SkillVersionRow]]:
        rows: list[tuple[SkillRow, SkillVersionRow]] = []
        for version_id in version_ids:
            row = (
                await session.execute(
                    select(SkillRow, SkillVersionRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.skill_id == SkillRow.id,
                    )
                    .where(SkillVersionRow.id == version_id)
                    .with_for_update(
                        read=True,
                        of=[SkillRow, SkillVersionRow],
                    )
                )
            ).one_or_none()
            if row is None:
                raise RunSnapshotAssetStale
            asset, version = row
            if (
                not RunSnapshotRepository._asset_allowed(
                    asset_scope=asset.scope,
                    asset_project_id=asset.project_id,
                    project_id=project_id,
                )
                or asset.status != "active"
                or asset.current_version_id != version.id
                or version.revoked_at is not None
                or (asset.scope == AssetScope.SYSTEM.value and version.version_number != 1)
            ):
                raise RunSnapshotAssetStale
            rows.append((asset, version))
        return rows

    @staticmethod
    async def _mcps(
        session: AsyncSession,
        version_ids: tuple[uuid.UUID, ...],
        project_id: uuid.UUID,
        *,
        endpoint_policy: McpEndpointPolicy | None = None,
    ) -> list[tuple[McpServerRow, McpServerVersionRow]]:
        rows: list[tuple[McpServerRow, McpServerVersionRow]] = []
        for version_id in version_ids:
            row = (
                await session.execute(
                    select(McpServerRow, McpServerVersionRow)
                    .join(
                        McpServerVersionRow,
                        McpServerVersionRow.mcp_server_id == McpServerRow.id,
                    )
                    .where(McpServerVersionRow.id == version_id)
                    .with_for_update(
                        read=True,
                        of=[McpServerRow, McpServerVersionRow],
                    )
                )
            ).one_or_none()
            if row is None:
                raise RunSnapshotAssetStale
            asset, version = row
            if (
                not RunSnapshotRepository._asset_allowed(
                    asset_scope=asset.scope,
                    asset_project_id=asset.project_id,
                    project_id=project_id,
                )
                or asset.status != "active"
                or version.workflow_status != "published"
                or (asset.scope == AssetScope.PROJECT.value and version.transport == "stdio")
            ):
                raise RunSnapshotAssetStale
            if asset.scope == AssetScope.PROJECT.value:
                try:
                    validate_project_mcp_definition(
                        transport=version.transport,
                        url=version.url,
                        env=version.non_secret_env,
                        headers=version.non_secret_headers,
                        oauth=version.oauth_metadata,
                        credential_slot_schemas=(),
                        endpoint_policy=endpoint_policy,
                    )
                except (AttributeError, McpDefinitionPolicyError, TypeError):
                    raise RunSnapshotAssetStale from None
            rows.append((asset, version))
        return rows

    @staticmethod
    async def _validate_dependency_order(
        session: AsyncSession,
        snapshot: ResolvedAgentSnapshot,
    ) -> None:
        skill_ref_rows = tuple(
            (
                await session.execute(
                    select(
                        AgentVersionSkillRefRow.skill_asset_scope,
                        AgentVersionSkillRefRow.skill_asset_id,
                    )
                    .where(AgentVersionSkillRefRow.agent_version_id == snapshot.version_id)
                    .order_by(AgentVersionSkillRefRow.sort_order)
                )
            ).all()
        )
        mcp_ids = tuple(
            (
                await session.execute(
                    select(AgentVersionMcpRefRow.mcp_server_version_id)
                    .where(AgentVersionMcpRefRow.agent_version_id == snapshot.version_id)
                    .order_by(
                        AgentVersionMcpRefRow.sort_order,
                        AgentVersionMcpRefRow.mcp_server_version_id,
                    )
                )
            ).scalars()
        )
        persisted_refs = tuple(SkillAssetRef(AssetScope(scope), asset_id) for scope, asset_id in skill_ref_rows)
        if persisted_refs != snapshot.payload.skill_refs or mcp_ids != snapshot.payload.mcp_version_ids or snapshot.dependency_version_ids != (*snapshot.skill_version_ids, *mcp_ids):
            raise RunSnapshotAssetStale

    @staticmethod
    async def _credential_closures(
        session: AsyncSession,
        mcps: list[tuple[McpServerRow, McpServerVersionRow]],
    ) -> dict[uuid.UUID, LockedMcpCredentialClosure]:
        targets = tuple(
            McpCredentialClosureTarget(
                version_id=uuid.UUID(str(version.id)),
                scope=AssetScope(asset.scope),
                project_id=(uuid.UUID(str(asset.project_id)) if asset.scope == AssetScope.PROJECT.value and asset.project_id is not None else None),
            )
            for asset, version in mcps
        )
        try:
            return await lock_mcp_credential_closures(
                session,
                targets,
                load_envelopes=False,
            )
        except McpCredentialClosureInvalid:
            raise RunSnapshotAssetStale from None

    @staticmethod
    def _validate_project_mcp_credential_slots(
        mcps: list[tuple[McpServerRow, McpServerVersionRow]],
        closures: Mapping[uuid.UUID, LockedMcpCredentialClosure],
        *,
        endpoint_policy: McpEndpointPolicy | None,
    ) -> None:
        """Validate the locked credential-slot schemas before admitting work."""

        for asset, version in mcps:
            if asset.scope != AssetScope.PROJECT.value:
                continue
            try:
                closure = closures[uuid.UUID(str(version.id))]
                validate_project_mcp_definition(
                    transport=version.transport,
                    url=version.url,
                    env=version.non_secret_env,
                    headers=version.non_secret_headers,
                    oauth=version.oauth_metadata,
                    credential_slot_schemas=tuple(slot.payload_schema for slot in closure.slots),
                    endpoint_policy=endpoint_policy,
                )
            except (
                AttributeError,
                KeyError,
                McpDefinitionPolicyError,
                TypeError,
                ValueError,
            ):
                raise RunSnapshotAssetStale from None

    async def create_run_with_snapshot(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        request: PrivateRunCreate,
        resolved_agent: ResolvedAgentSnapshot | ResolvedRunAssetClosure,
    ) -> PrivateRunRecord:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                return await self.create_run_with_snapshot_in_session(
                    session,
                    context,
                    thread_id,
                    request,
                    resolved_agent,
                )
        except RunSnapshotAssetStale:
            raise PrivateWorkAssetStale(context.request_id) from None
        except RunModelSelectionLocked:
            raise PrivateWorkRunModelSelectionLocked(context.request_id) from None
        except RunSelectedModelUnavailable:
            raise PrivateWorkRunModelUnavailable(context.request_id) from None
        except RunExecutionProfileUnsupported:
            raise PrivateWorkRunExecutionProfileUnsupported(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(context.request_id) from None
        except IntegrityError:
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def create_run_with_snapshot_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        request: PrivateRunCreate,
        resolved_agent: ResolvedAgentSnapshot | ResolvedRunAssetClosure,
        *,
        continuation_source_run_id: str | None = None,
        runtime_kind: Literal["chat", "skill_builder"] = "chat",
        admit_memory: bool = True,
    ) -> PrivateRunRecord:
        """Write a pending run and exact closure in a caller-owned transaction."""

        context = require_issued_private_work_context(context)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise PrivateWorkConflict(context.request_id)
        if type(request) is not PrivateRunCreate or type(resolved_agent) not in (
            ResolvedAgentSnapshot,
            ResolvedRunAssetClosure,
        ):
            raise PrivateWorkConflict(context.request_id)
        if request.follow_up_to_run_id != continuation_source_run_id:
            raise PrivateWorkConflict(context.request_id)
        if runtime_kind not in {"chat", "skill_builder"} or type(admit_memory) is not bool:
            raise PrivateWorkConflict(context.request_id)
        if runtime_kind == "skill_builder" and (continuation_source_run_id is not None or admit_memory):
            raise PrivateWorkConflict(context.request_id)
        resolved_closure = resolved_agent if type(resolved_agent) is ResolvedRunAssetClosure else None
        lead_agent = resolved_closure.lead_agent if resolved_closure is not None else resolved_agent
        if type(lead_agent) is not ResolvedAgentSnapshot or lead_agent.kind is not AssetKind.AGENT or lead_agent.catalog_generation < 0:
            raise PrivateWorkConflict(context.request_id)
        _reject_secret_bearing_keys(request.metadata, context.request_id)
        _reject_secret_bearing_keys(request.kwargs, context.request_id)
        effective_lead_model_ref = selected_run_model_ref(
            lead_agent.payload.model_ref,
            request.execution_profile,
        )
        exact_model_ref = (
            self._model_ref_resolver.resolve(
                effective_lead_model_ref,
            )
            if self._model_catalog is None
            else None
        )
        if self._model_catalog is None and exact_model_ref is None:
            raise RunSnapshotAssetStale
        if self._model_catalog is None and resolved_closure is not None:
            if any(self._model_ref_resolver.resolve(agent.payload.model_ref) is None for agent in resolved_closure.delegated_agents):
                raise RunSnapshotAssetStale
        if self._model_catalog is None and any(value is not None for value in request.execution_profile.as_dict().values()):
            # Production always supplies the PostgreSQL model catalog. The
            # exact-resolver fallback cannot prove model capabilities and
            # therefore cannot safely admit an explicit execution profile.
            raise RunSnapshotAssetStale
        try:
            current_upload_snapshot = await admit_current_upload_snapshot(
                session,
                scope=context.resource_scope,
                thread_id=thread_id,
                run_kwargs=request.kwargs,
            )
        except CurrentUploadSnapshotInvalid:
            raise RunSnapshotAssetStale from None
        admitted_run_kwargs = dict(request.kwargs)
        admitted_run_kwargs[RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG] = persisted_current_upload_snapshot(
            current_upload_snapshot,
        )
        safe_request = replace(
            request,
            assistant_id=str(lead_agent.asset_id),
            status="pending",
            multitask_strategy="reject",
            model_name=exact_model_ref,
            kwargs=admitted_run_kwargs,
        )
        (
            skills,
            mcps,
            closures,
            skill_credential_closures,
        ) = (
            await self.validate_run_asset_closure_in_session(
                session,
                context,
                resolved_closure,
            )
            if resolved_closure is not None
            else await self.validate_agent_closure_in_session(
                session,
                context,
                lead_agent,
            )
        )
        locked_runtime_policy: LockedAgentRuntimePolicy | None = None
        if self._runtime_policy is not None:
            try:
                locked_runtime_policy = await self._runtime_policy.lock_agent_runtime_for_admission(
                    session,
                )
                safe_request = _apply_runtime_recursion_limit(
                    safe_request,
                    locked_runtime_policy,
                )
            except SystemRuntimePolicyRepositoryInvariant:
                raise RunSnapshotAssetStale from None
        run = await PrivateRunRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            request=safe_request,
        )
        if self._runtime_policy is not None:
            try:
                await self._runtime_policy.admit_run_snapshot(
                    session,
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    locked_policy=locked_runtime_policy,
                )
            except SystemRuntimePolicyRepositoryInvariant:
                raise RunSnapshotAssetStale from None
        if self._model_catalog is not None:
            try:
                model_snapshot = await self._model_catalog.admit_model_snapshot(
                    session,
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    purpose="lead",
                    model_ref=effective_lead_model_ref,
                )
            except SystemModelNotFound:
                if request.execution_profile.model_name is not None:
                    raise RunSelectedModelUnavailable from None
                raise RunSnapshotAssetStale from None
            except (
                SystemModelConflict,
                SystemModelInvalid,
            ):
                raise RunSnapshotAssetStale from None
            except SystemModelStorageUnavailable:
                raise PrivateWorkUnavailable(context.request_id) from None
            exact_model_ref = model_snapshot.model_ref
            sampling_overrides = lead_agent.payload.model_settings.sampling_overrides()
            if sampling_overrides:
                try:
                    validate_model_settings(
                        sampling_overrides,
                        provider_adapter=model_snapshot.provider_adapter,
                    )
                except (AttributeError, ModelSettingsInvalid):
                    raise RunExecutionProfileUnsupported from None
            effective_profile = resolve_admitted_run_execution_profile(
                requested=request.execution_profile,
                model_ref=model_snapshot.model_ref,
                supports_thinking=model_snapshot.supports_thinking,
                supports_reasoning_effort=(model_snapshot.supports_reasoning_effort),
                supports_vision=model_snapshot.supports_vision,
                agent_thinking_enabled=(lead_agent.payload.model_settings.thinking_enabled),
                agent_reasoning_effort=(lead_agent.payload.model_settings.reasoning_effort),
            )
            if resolved_closure is not None:
                try:
                    for delegated_agent in resolved_closure.delegated_agents:
                        await self._model_catalog.admit_model_snapshot(
                            session,
                            project_id=context.project_id,
                            owner_user_id=str(context.user_id),
                            thread_id=thread_id,
                            run_id=run.run_id,
                            purpose=agent_model_snapshot_purpose(delegated_agent.version_id),
                            model_ref=delegated_agent.payload.model_ref,
                        )
                except (
                    SystemModelConflict,
                    SystemModelInvalid,
                    SystemModelNotFound,
                ):
                    raise RunSnapshotAssetStale from None
                except SystemModelStorageUnavailable:
                    raise PrivateWorkUnavailable(context.request_id) from None
            if locked_runtime_policy is not None:
                auxiliary_model_refs: list[tuple[str, str | None]] = [
                    ("title", locked_runtime_policy.value.title.model_name),
                    (
                        "summarization",
                        locked_runtime_policy.value.summarization.model_name,
                    ),
                    ("memory", locked_runtime_policy.value.memory.model_name),
                ]
                if runtime_kind in {"chat", "skill_builder"} and not effective_profile.supports_vision:
                    auxiliary_model_refs.append(
                        (
                            "vision",
                            locked_runtime_policy.value.vision_bridge.model_name,
                        )
                    )
                try:
                    for purpose, model_ref in auxiliary_model_refs:
                        snapshot_ref = auxiliary_model_snapshot_ref(
                            purpose,
                            model_ref,
                            title_enabled=locked_runtime_policy.value.title.enabled,
                        )
                        if snapshot_ref is None:
                            continue
                        try:
                            auxiliary_snapshot = await self._model_catalog.admit_model_snapshot(
                                session,
                                project_id=context.project_id,
                                owner_user_id=str(context.user_id),
                                thread_id=thread_id,
                                run_id=run.run_id,
                                purpose=purpose,
                                model_ref=snapshot_ref,
                            )
                            if purpose == "vision" and not auxiliary_snapshot.supports_vision:
                                raise SystemModelInvalid(context.request_id)
                        except SystemModelNotFound:
                            if purpose == "title" and model_ref is None:
                                continue
                            raise
                except (
                    SystemModelConflict,
                    SystemModelInvalid,
                    SystemModelNotFound,
                ):
                    raise RunSnapshotAssetStale from None
                except SystemModelStorageUnavailable:
                    raise PrivateWorkUnavailable(context.request_id) from None
            admitted_kwargs = dict(run.kwargs)
            admitted_kwargs[RUN_EXECUTION_PROFILE_KWARG] = persisted_run_execution_profile(
                request.execution_profile,
                effective_profile,
            )
            if not await PrivateRunRepository(
                session,
            ).update_admitted_execution_profile(
                scope=context.resource_scope,
                run_id=run.run_id,
                model_name=exact_model_ref,
                kwargs=admitted_kwargs,
            ):
                raise RunSnapshotAssetStale
            refreshed = await PrivateRunRepository(session).get(
                scope=context.resource_scope,
                run_id=run.run_id,
                lock=True,
            )
            if refreshed is None:
                raise RunSnapshotAssetStale
            run = refreshed
        if locked_runtime_policy is not None and admit_memory:
            await self._admit_memory_context_snapshot(
                session,
                context,
                run_id=run.run_id,
                locked_policy=locked_runtime_policy,
                thread_id=thread_id,
                continuation_source_run_id=continuation_source_run_id,
            )
        asset_rows = [
            RunAssetVersionRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                asset_kind=AssetKind.AGENT.value,
                dependency_order=0,
                asset_scope=lead_agent.scope.value,
                asset_id=lead_agent.asset_id,
                version_id=lead_agent.version_id,
                payload_checksum=lead_agent.checksum,
                catalog_generation=lead_agent.catalog_generation,
                snapshot_json=encode_run_asset_snapshot(lead_agent),
            )
        ]
        dependency_order = 1
        if resolved_closure is not None:
            for delegated_agent in resolved_closure.delegated_agents:
                asset_rows.append(
                    RunAssetVersionRow(
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        run_id=run.run_id,
                        asset_kind=AssetKind.AGENT.value,
                        dependency_order=dependency_order,
                        asset_scope=delegated_agent.scope.value,
                        asset_id=delegated_agent.asset_id,
                        version_id=delegated_agent.version_id,
                        payload_checksum=delegated_agent.checksum,
                        catalog_generation=lead_agent.catalog_generation,
                        snapshot_json=encode_run_asset_snapshot(delegated_agent),
                    )
                )
                dependency_order += 1
        skill_snapshots = () if resolved_closure is None else resolved_closure.skills
        mcp_snapshots = () if resolved_closure is None else resolved_closure.mcps
        if len(skill_snapshots) != len(skills) or len(mcp_snapshots) != len(mcps):
            raise RunSnapshotAssetStale
        for (asset, version), skill_snapshot in zip(
            skills,
            skill_snapshots,
            strict=True,
        ):
            asset_rows.append(
                RunAssetVersionRow(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    asset_kind=AssetKind.SKILL.value,
                    dependency_order=dependency_order,
                    asset_scope=asset.scope,
                    asset_id=asset.id,
                    version_id=version.id,
                    payload_checksum=version.payload_checksum,
                    catalog_generation=lead_agent.catalog_generation,
                    snapshot_json=encode_run_asset_snapshot(skill_snapshot),
                )
            )
            dependency_order += 1
        for (asset, version), mcp_snapshot in zip(
            mcps,
            mcp_snapshots,
            strict=True,
        ):
            asset_rows.append(
                RunAssetVersionRow(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    asset_kind=AssetKind.MCP.value,
                    dependency_order=dependency_order,
                    asset_scope=asset.scope,
                    asset_id=asset.id,
                    version_id=version.id,
                    payload_checksum=version.payload_checksum,
                    catalog_generation=lead_agent.catalog_generation,
                    snapshot_json=encode_run_asset_snapshot(mcp_snapshot),
                )
            )
            dependency_order += 1
        session.add_all(asset_rows)
        session.add_all(
            RunMcpGrantSnapshotRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                mcp_version_id=material.grant.mcp_server_version_id,
                credential_slot_id=material.slot.id,
                credential_grant_id=material.grant.id,
                credential_version_id=material.version.id,
            )
            for _asset, version in mcps
            for material in closures[uuid.UUID(str(version.id))].materials
        )
        session.add_all(
            RunSkillCredentialSnapshotRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                skill_id=material.skill_id,
                skill_version_id=material.skill_version_id,
                secret_name=material.env_name,
                source_env_field_name=material.credential_field_name,
                skill_credential_binding_id=material.binding_id,
                binding_revision=material.binding_revision,
                credential_id=material.credential_id,
                credential_version_id=material.credential_version_id,
            )
            for _asset, version in skills
            for material in skill_credential_closures[uuid.UUID(str(version.id))].materials
        )
        await session.flush()
        await PrivateThreadRepository(session).touch_activity(
            scope=context.resource_scope,
            thread_id=thread_id,
            occurred_at=run.created_at,
            thread_kind=runtime_kind,
        )
        return run

    @staticmethod
    def _validate_dependency_snapshots(
        rows: list[tuple[object, object]],
        snapshots: tuple[object, ...],
        *,
        catalog_generation: int,
    ) -> None:
        if len(rows) != len(snapshots):
            raise RunSnapshotAssetStale
        for (asset, version), snapshot in zip(rows, snapshots, strict=True):
            if (
                getattr(asset, "scope", None) != getattr(getattr(snapshot, "scope", None), "value", None)
                or getattr(asset, "id", None) != getattr(snapshot, "asset_id", None)
                or getattr(version, "id", None) != getattr(snapshot, "version_id", None)
                or getattr(version, "payload_checksum", None) != getattr(snapshot, "checksum", None)
                or getattr(snapshot, "catalog_generation", None) != catalog_generation
            ):
                raise RunSnapshotAssetStale

    @staticmethod
    def _validate_main_dependency_boundary(
        closure: ResolvedRunAssetClosure,
        *,
        canonical_main: bool,
    ) -> None:
        skill_ids = tuple(item.version_id for item in closure.skills)
        mcp_ids = tuple(item.version_id for item in closure.mcps)
        main_skill_count = len(closure.main_skill_version_ids)
        main_mcp_count = len(closure.main_mcp_version_ids)
        if skill_ids[:main_skill_count] != closure.main_skill_version_ids or mcp_ids[:main_mcp_count] != closure.main_mcp_version_ids:
            raise RunSnapshotAssetStale
        if not canonical_main:
            if (
                closure.delegated_agents
                or skill_ids != closure.lead_agent.skill_version_ids
                or mcp_ids != closure.lead_agent.payload.mcp_version_ids
                or closure.main_skill_version_ids != skill_ids
                or closure.main_mcp_version_ids != mcp_ids
                or len({item.asset_id for item in closure.skills}) != len(closure.skills)
                or len({item.asset_id for item in closure.mcps}) != len(closure.mcps)
            ):
                raise RunSnapshotAssetStale
            return

        # For each kind and asset_id, the first persisted row belongs to Main's
        # current pool.  Historical rows may only follow that first row and
        # must be referenced by at least one delegated Agent.  This invariant
        # lets Worker reconstruct the Main/delegate boundary without a schema
        # column while dependency_order remains globally continuous.
        main_skill_asset_ids = {item.asset_id for item in closure.skills[:main_skill_count]}
        main_mcp_asset_ids = {item.asset_id for item in closure.mcps[:main_mcp_count]}
        if len(main_skill_asset_ids) != main_skill_count or len(main_mcp_asset_ids) != main_mcp_count:
            raise RunSnapshotAssetStale

        skill_by_version = {item.version_id: item for item in closure.skills}
        mcp_by_version = {item.version_id: item for item in closure.mcps}
        expected_skill_ids = list(closure.main_skill_version_ids)
        expected_mcp_ids = list(closure.main_mcp_version_ids)
        seen_skill_ids = set(expected_skill_ids)
        seen_mcp_ids = set(expected_mcp_ids)
        for agent in closure.delegated_agents:
            for version_id in agent.skill_version_ids:
                item = skill_by_version.get(version_id)
                if item is None:
                    raise RunSnapshotAssetStale
                if version_id not in seen_skill_ids:
                    if item.asset_id not in main_skill_asset_ids:
                        raise RunSnapshotAssetStale
                    expected_skill_ids.append(version_id)
                    seen_skill_ids.add(version_id)
            for version_id in agent.payload.mcp_version_ids:
                item = mcp_by_version.get(version_id)
                if item is None:
                    raise RunSnapshotAssetStale
                if version_id not in seen_mcp_ids:
                    if item.asset_id not in main_mcp_asset_ids:
                        raise RunSnapshotAssetStale
                    expected_mcp_ids.append(version_id)
                    seen_mcp_ids.add(version_id)
        if skill_ids != tuple(expected_skill_ids) or mcp_ids != tuple(expected_mcp_ids):
            raise RunSnapshotAssetStale

    async def validate_run_asset_closure_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        closure: ResolvedRunAssetClosure,
    ) -> tuple[
        list[tuple[SkillRow, SkillVersionRow]],
        list[tuple[McpServerRow, McpServerVersionRow]],
        dict[uuid.UUID, LockedMcpCredentialClosure],
        dict[uuid.UUID, LockedSkillCredentialClosure],
    ]:
        """Lock and validate the complete lead/delegate Run asset closure."""

        context = require_issued_private_work_context(context)
        if not isinstance(session, AsyncSession) or not session.in_transaction() or type(closure) is not ResolvedRunAssetClosure:
            raise RunSnapshotAssetStale
        if closure.lead_agent.catalog_generation > await CatalogStateRepository(session).read_generation():
            raise RunSnapshotAssetStale
        lead_asset, _lead_version = await self._agent(
            session,
            closure.lead_agent,
            context.project_id,
        )
        agents = (closure.lead_agent, *closure.delegated_agents)
        if len({item.asset_id for item in agents}) != len(agents) or any(item.catalog_generation != closure.lead_agent.catalog_generation for item in agents):
            raise RunSnapshotAssetStale
        await self._validate_dependency_order(session, closure.lead_agent)
        for delegated_agent in closure.delegated_agents:
            await self._agent(session, delegated_agent, context.project_id)
            await self._validate_dependency_order(session, delegated_agent)

        self._validate_main_dependency_boundary(
            closure,
            canonical_main=(lead_asset.scope == AssetScope.SYSTEM.value and lead_asset.project_id is None and lead_asset.source_key == "builtin:agent:project-assistant"),
        )
        skills = await self._skills(
            session,
            tuple(item.version_id for item in closure.skills),
            context.project_id,
        )
        self._validate_dependency_snapshots(
            skills,
            closure.skills,
            catalog_generation=closure.lead_agent.catalog_generation,
        )
        try:
            skill_credential_closures = await lock_skill_credential_closures(
                session,
                context.project_id,
                tuple(
                    SkillCredentialClosureTarget(
                        skill_id=uuid.UUID(str(asset.id)),
                        skill_version_id=uuid.UUID(str(version.id)),
                    )
                    for asset, version in skills
                ),
                load_envelopes=False,
                require_required=True,
            )
        except SkillCredentialClosureInvalid:
            raise RunSnapshotAssetStale from None
        mcps = await self._mcps(
            session,
            tuple(item.version_id for item in closure.mcps),
            context.project_id,
            endpoint_policy=self._endpoint_policy,
        )
        self._validate_dependency_snapshots(
            mcps,
            closure.mcps,
            catalog_generation=closure.lead_agent.catalog_generation,
        )
        closures = await self._credential_closures(session, mcps)
        self._validate_project_mcp_credential_slots(
            mcps,
            closures,
            endpoint_policy=self._endpoint_policy,
        )
        return skills, mcps, closures, skill_credential_closures

    async def validate_agent_closure_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        resolved_agent: ResolvedAgentSnapshot,
    ) -> tuple[
        list[tuple[SkillRow, SkillVersionRow]],
        list[tuple[McpServerRow, McpServerVersionRow]],
        dict[uuid.UUID, LockedMcpCredentialClosure],
        dict[uuid.UUID, LockedSkillCredentialClosure],
    ]:
        """Lock and validate an Agent plus its exact credential-grant closure."""

        context = require_issued_private_work_context(context)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise RunSnapshotAssetStale
        if type(resolved_agent) is not ResolvedAgentSnapshot:
            raise RunSnapshotAssetStale
        if resolved_agent.kind is not AssetKind.AGENT or resolved_agent.catalog_generation < 0:
            raise RunSnapshotAssetStale
        if resolved_agent.catalog_generation > await CatalogStateRepository(session).read_generation():
            raise RunSnapshotAssetStale
        project_id = context.project_id
        await self._agent(session, resolved_agent, project_id)
        await self._validate_dependency_order(session, resolved_agent)
        skills = await self._skills(
            session,
            resolved_agent.skill_version_ids,
            project_id,
        )
        try:
            skill_credential_closures = await lock_skill_credential_closures(
                session,
                project_id,
                tuple(
                    SkillCredentialClosureTarget(
                        skill_id=uuid.UUID(str(asset.id)),
                        skill_version_id=uuid.UUID(str(version.id)),
                    )
                    for asset, version in skills
                ),
                load_envelopes=False,
                require_required=True,
            )
        except SkillCredentialClosureInvalid:
            raise RunSnapshotAssetStale from None
        mcps = await self._mcps(
            session,
            resolved_agent.payload.mcp_version_ids,
            project_id,
            endpoint_policy=self._endpoint_policy,
        )
        closures = await self._credential_closures(session, mcps)
        self._validate_project_mcp_credential_slots(
            mcps,
            closures,
            endpoint_policy=self._endpoint_policy,
        )
        return skills, mcps, closures, skill_credential_closures

    async def list_assets(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunAssetSnapshot, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                return await self.list_assets_in_session(session, context, run_id)
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_assets_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunAssetSnapshot, ...]:
        context = require_issued_private_work_context(context)
        statement = (
            select(RunAssetVersionRow)
            .where(
                RunAssetVersionRow.project_id == context.project_id,
                RunAssetVersionRow.owner_user_id == str(context.user_id),
                RunAssetVersionRow.run_id == run_id,
            )
            .order_by(RunAssetVersionRow.dependency_order)
        )
        if lock:
            statement = statement.with_for_update(of=RunAssetVersionRow)
        rows = (await session.execute(statement)).scalars()
        return tuple(
            RunAssetSnapshot(
                asset_kind=row.asset_kind,
                dependency_order=row.dependency_order,
                asset_scope=row.asset_scope,
                asset_id=row.asset_id,
                version_id=row.version_id,
                payload_checksum=row.payload_checksum,
                catalog_generation=row.catalog_generation,
                snapshot_json=dict(row.snapshot_json),
            )
            for row in rows
        )

    async def list_mcp_grants(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunMcpGrantSnapshot, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                return await self.list_mcp_grants_in_session(session, context, run_id)
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_mcp_grants_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunMcpGrantSnapshot, ...]:
        context = require_issued_private_work_context(context)
        statement = (
            select(RunMcpGrantSnapshotRow)
            .where(
                RunMcpGrantSnapshotRow.project_id == context.project_id,
                RunMcpGrantSnapshotRow.owner_user_id == str(context.user_id),
                RunMcpGrantSnapshotRow.run_id == run_id,
            )
            .order_by(
                RunMcpGrantSnapshotRow.mcp_version_id,
                RunMcpGrantSnapshotRow.credential_slot_id,
            )
        )
        if lock:
            statement = statement.with_for_update(of=RunMcpGrantSnapshotRow)
        rows = (await session.execute(statement)).scalars()
        return tuple(
            RunMcpGrantSnapshot(
                mcp_version_id=row.mcp_version_id,
                credential_slot_id=row.credential_slot_id,
                credential_grant_id=row.credential_grant_id,
                credential_version_id=row.credential_version_id,
            )
            for row in rows
        )

    async def current_mcp_grants_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        mcp_assets: tuple[RunAssetSnapshot, ...],
    ) -> tuple[RunMcpGrantSnapshot, ...]:
        """Lock the current exact closure and return only its secret-free IDs."""

        context = require_issued_private_work_context(context)
        if any(asset.asset_kind != AssetKind.MCP.value for asset in mcp_assets):
            raise RunSnapshotAssetStale
        mcps = await self._mcps(
            session,
            tuple(asset.version_id for asset in mcp_assets),
            context.project_id,
            endpoint_policy=self._endpoint_policy,
        )
        by_version = {uuid.UUID(str(version.id)): (asset, version) for asset, version in mcps}
        for persisted in mcp_assets:
            row = by_version.get(persisted.version_id)
            if row is None:
                raise RunSnapshotAssetStale
            asset, version = row
            if asset.id != persisted.asset_id or asset.scope != persisted.asset_scope or version.payload_checksum != persisted.payload_checksum:
                raise RunSnapshotAssetStale
        closures = await self._credential_closures(session, mcps)
        self._validate_project_mcp_credential_slots(
            mcps,
            closures,
            endpoint_policy=self._endpoint_policy,
        )
        current = [
            RunMcpGrantSnapshot(
                mcp_version_id=material.grant.mcp_server_version_id,
                credential_slot_id=material.slot.id,
                credential_grant_id=material.grant.id,
                credential_version_id=material.version.id,
            )
            for _asset, version in mcps
            for material in closures[uuid.UUID(str(version.id))].materials
        ]
        return tuple(
            sorted(
                current,
                key=lambda item: (
                    item.mcp_version_id.int,
                    item.credential_slot_id.int,
                    item.credential_grant_id.int,
                    item.credential_version_id.int,
                ),
            )
        )

    async def list_skill_credentials(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunSkillCredentialSnapshot, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                return await self.list_skill_credentials_in_session(
                    session,
                    context,
                    run_id,
                )
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_skill_credentials_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunSkillCredentialSnapshot, ...]:
        context = require_issued_private_work_context(context)
        statement = (
            select(RunSkillCredentialSnapshotRow)
            .where(
                RunSkillCredentialSnapshotRow.project_id == context.project_id,
                RunSkillCredentialSnapshotRow.owner_user_id == str(context.user_id),
                RunSkillCredentialSnapshotRow.run_id == run_id,
            )
            .order_by(
                RunSkillCredentialSnapshotRow.skill_version_id,
                RunSkillCredentialSnapshotRow.secret_name,
            )
        )
        if lock:
            statement = statement.with_for_update(
                of=RunSkillCredentialSnapshotRow,
            )
        rows = (await session.execute(statement)).scalars()
        return tuple(
            RunSkillCredentialSnapshot(
                skill_id=row.skill_id,
                skill_version_id=row.skill_version_id,
                secret_name=row.secret_name,
                source_env_field_name=row.source_env_field_name,
                skill_credential_binding_id=(row.skill_credential_binding_id),
                binding_revision=row.binding_revision,
                credential_id=row.credential_id,
                credential_version_id=row.credential_version_id,
            )
            for row in rows
        )

    async def current_skill_credentials_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        skill_assets: tuple[RunAssetSnapshot, ...],
    ) -> tuple[RunSkillCredentialSnapshot, ...]:
        """Lock the current Skill Credential closure and return secret-free IDs."""

        context = require_issued_private_work_context(context)
        if any(asset.asset_kind != AssetKind.SKILL.value for asset in skill_assets):
            raise RunSnapshotAssetStale
        skills = await self._skills(
            session,
            tuple(asset.version_id for asset in skill_assets),
            context.project_id,
        )
        by_version = {uuid.UUID(str(version.id)): (asset, version) for asset, version in skills}
        for persisted in skill_assets:
            row = by_version.get(persisted.version_id)
            if row is None:
                raise RunSnapshotAssetStale
            asset, version = row
            if asset.id != persisted.asset_id or asset.scope != persisted.asset_scope or version.payload_checksum != persisted.payload_checksum:
                raise RunSnapshotAssetStale
        try:
            closures = await lock_skill_credential_closures(
                session,
                context.project_id,
                tuple(
                    SkillCredentialClosureTarget(
                        skill_id=uuid.UUID(str(asset.id)),
                        skill_version_id=uuid.UUID(str(version.id)),
                    )
                    for asset, version in skills
                ),
                load_envelopes=False,
                require_required=True,
            )
        except SkillCredentialClosureInvalid:
            raise RunSnapshotAssetStale from None
        current = [
            RunSkillCredentialSnapshot(
                skill_id=material.skill_id,
                skill_version_id=material.skill_version_id,
                secret_name=material.env_name,
                source_env_field_name=material.credential_field_name,
                skill_credential_binding_id=material.binding_id,
                binding_revision=material.binding_revision,
                credential_id=material.credential_id,
                credential_version_id=material.credential_version_id,
            )
            for _asset, version in skills
            for material in closures[uuid.UUID(str(version.id))].materials
        ]
        return tuple(
            sorted(
                current,
                key=lambda item: (
                    item.skill_version_id.int,
                    item.secret_name,
                    item.skill_credential_binding_id.int,
                    item.credential_version_id.int,
                ),
            )
        )

    async def lock_admitted_skill_credentials_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        persisted: tuple[RunSkillCredentialSnapshot, ...],
        *,
        declared_targets: frozenset[tuple[uuid.UUID, str]],
        required_targets: frozenset[tuple[uuid.UUID, str]],
        load_envelopes: bool = False,
    ) -> tuple[LockedSkillCredentialMaterial, ...]:
        """Lock revocable Credential authority without rereading Skill state."""

        context = require_issued_private_work_context(context)
        try:
            return await lock_admitted_skill_credential_materials(
                session,
                context.project_id,
                tuple(
                    AdmittedSkillCredentialReference(
                        skill_id=item.skill_id,
                        skill_version_id=item.skill_version_id,
                        env_name=item.secret_name,
                        credential_field_name=item.source_env_field_name,
                        binding_id=item.skill_credential_binding_id,
                        binding_revision=item.binding_revision,
                        credential_id=item.credential_id,
                        credential_version_id=item.credential_version_id,
                    )
                    for item in persisted
                ),
                declared_targets=declared_targets,
                required_targets=required_targets,
                load_envelopes=load_envelopes,
            )
        except SkillCredentialClosureInvalid:
            raise RunSnapshotAssetStale from None
