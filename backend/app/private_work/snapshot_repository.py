from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from sqlalchemy import func, select
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
    PrivateWorkRunWorkloadProfileUnsupported,
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
from app.private_work.legacy_run_skill_snapshot_writer import (
    LegacyRunSkillSnapshotWriter,
    PreparedLegacyRunSkillSnapshots,
    frozen_run_skill_snapshot_writer,
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
from app.private_work.run_skill_writer_cohort import (
    RunSkillWriterCohortConflict,
    RunSkillWriterCohortUnavailable,
    require_active_run_skill_writer_cohort,
)
from app.private_work.sandbox_files import (
    RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG,
    CurrentUploadSnapshotInvalid,
    admit_current_upload_snapshot,
    persisted_current_upload_snapshot,
)
from app.private_work.snapshot_admission_rules import (  # noqa: F401 - compatibility exports
    _FORBIDDEN_PERSISTED_KEY_PARTS,
    _apply_runtime_recursion_limit,
    _r1_snapshot_schema_version,
    _reject_secret_bearing_keys,
    _RunAssetSnapshotAdmissionEncoder,
    asset_allowed,
    plan_run_asset_rows,
    validate_dependency_snapshots,
    validate_main_dependency_boundary,
    validate_project_mcp_secret_slots,
)
from app.private_work.snapshot_contracts import (  # noqa: F401 - compatibility exports
    AdmittedRunModelSnapshot,
    RunMcpSecretSnapshot,
    RunModelSnapshotAdmissionPort,
    RunRuntimePolicyAdmissionPort,
    RunSkillSecretSnapshot,
    RunSnapshotAssetStale,
    agent_model_snapshot_purpose,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.private_work.workload_profile import (
    EffectiveRunWorkloadProfile,
    RunWorkloadProfileUnsupported,
    effective_run_workload_profile_from_kwargs,
    freeze_admitted_run_workload_profile,
)
from app.shared_assets.agent_payload_checksum import (
    agent_payload_checksum_matches,
    persisted_agent_payload_checksum_matches,
)
from app.shared_assets.catalog_state_repository import CatalogStateRepository
from app.shared_assets.errors import SharedAssetError
from app.shared_assets.mcp_secret_closure import (
    McpSecretClosure,
    lock_mcp_secret_closure,
)
from app.shared_assets.mcp_tool_inventory_repository import (
    McpToolInventoryRepository,
)
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedRunAssetClosure,
    ResolvedRunAssetFact,
    SkillAssetRef,
)
from app.shared_assets.skill_secret_closure import (
    AdmittedSkillSecretReference,
    LockedSkillSecretClosure,
    LockedSkillSecretMaterial,
    SkillSecretClosureInvalid,
    SkillSecretClosureTarget,
    lock_admitted_skill_secret_materials,
    lock_skill_secret_closures,
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
from app.system_settings.model_refs import ExactModelRefResolver, ModelRefResolver
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
    RunMcpSecretSnapshotRow,
    RunSkillSecretSnapshotRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets.agent_model import (
    AgentMcpRefRow,
    AgentRow,
    AgentSkillRefRow,
)
from deerflow.persistence.shared_assets.mcp_model import (
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
)
from deerflow.persistence.shared_assets.skill_model import SkillRow, SkillVersionRow
from deerflow.persistence.system_runtime_settings.model import (
    RunRuntimePolicySnapshotRow,
)


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
        self._run_skill_writer = frozen_run_skill_snapshot_writer()
        if not callable(personalization_repository_builder):
            raise TypeError("personalization repository builder must be callable")
        self._personalization_repository_builder = personalization_repository_builder
        if audit is not None and not callable(getattr(audit, "memory_injection_skipped", None)):
            raise TypeError("Run snapshot audit port is invalid")
        self._audit = audit

    @staticmethod
    async def _continuation_workload_profile(
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        source_run_id: str,
    ) -> EffectiveRunWorkloadProfile:
        source = (
            await session.execute(
                select(
                    RunRow.kwargs_json,
                    RunRuntimePolicySnapshotRow.schema_version,
                )
                .join(
                    RunRuntimePolicySnapshotRow,
                    (RunRuntimePolicySnapshotRow.project_id == RunRow.project_id)
                    & (RunRuntimePolicySnapshotRow.owner_user_id == RunRow.owner_user_id)
                    & (RunRuntimePolicySnapshotRow.thread_id == RunRow.thread_id)
                    & (RunRuntimePolicySnapshotRow.run_id == RunRow.run_id)
                    & (RunRuntimePolicySnapshotRow.section == "agent_runtime"),
                )
                .where(
                    RunRow.project_id == context.project_id,
                    RunRow.owner_user_id == str(context.user_id),
                    RunRow.thread_id == thread_id,
                    RunRow.run_id == source_run_id,
                    RunRow.status != "deleted",
                )
            )
        ).one_or_none()
        if source is None:
            raise RunWorkloadProfileUnsupported
        try:
            return effective_run_workload_profile_from_kwargs(
                source.kwargs_json,
                policy_schema_version=source.schema_version,
            )
        except (AttributeError, TypeError, RunWorkloadProfileUnsupported):
            raise RunWorkloadProfileUnsupported from None

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

    _asset_allowed = staticmethod(asset_allowed)
    _validate_project_mcp_secret_slots = staticmethod(validate_project_mcp_secret_slots)
    _validate_dependency_snapshots = staticmethod(validate_dependency_snapshots)
    _validate_main_dependency_boundary = staticmethod(validate_main_dependency_boundary)

    @staticmethod
    async def _agent(
        session: AsyncSession,
        snapshot: ResolvedAgentSnapshot,
        project_id: uuid.UUID,
    ) -> tuple[AgentRow, AgentRow]:
        asset = (
            await session.execute(
                select(AgentRow).where(
                    AgentRow.id == snapshot.asset_id,
                    AgentRow.definition_id == snapshot.version_id,
                )
            )
        ).scalar_one_or_none()
        if asset is None:
            raise RunSnapshotAssetStale
        try:
            persisted_payload = replace(
                snapshot.payload,
                description=asset.description,
                agents_instructions=asset.agents_instructions,
                soul=asset.soul,
                identity=asset.identity,
                user_context=asset.user_context,
                model_ref=asset.model_ref,
                model_settings=AgentModelSettings.model_validate(
                    {} if asset.model_settings is None else asset.model_settings,
                ),
                tool_groups=tuple(asset.tool_groups),
                payload_schema_version=asset.payload_schema_version,
            )
        except (AttributeError, TypeError, ValueError):
            raise RunSnapshotAssetStale from None
        runtime_payload = persisted_payload
        if (
            asset.scope != snapshot.scope.value
            or asset.status != "active"
            or asset.definition_id != snapshot.version_id
            or runtime_payload != snapshot.payload
            or not persisted_agent_payload_checksum_matches(
                persisted_payload,
                asset.payload_checksum,
            )
            or not agent_payload_checksum_matches(
                runtime_payload,
                snapshot.checksum,
            )
            or not RunSnapshotRepository._asset_allowed(
                asset_scope=asset.scope,
                asset_project_id=asset.project_id,
                project_id=project_id,
            )
        ):
            raise RunSnapshotAssetStale
        return asset, asset

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
                or version.files_sealed is not True
                or type(version.file_count) is not int
                or not 1 <= version.file_count <= 16_384
                or type(version.content_size_bytes) is not int
                or not 0 <= version.content_size_bytes <= 100 * 1024 * 1024
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
                        secret_slot_schemas=(),
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
                        AgentSkillRefRow.skill_asset_scope,
                        AgentSkillRefRow.skill_asset_id,
                    )
                    .where(AgentSkillRefRow.agent_id == snapshot.asset_id)
                    .order_by(AgentSkillRefRow.sort_order)
                )
            ).all()
        )
        mcp_ids = tuple(
            (
                await session.execute(
                    select(AgentMcpRefRow.mcp_server_version_id)
                    .where(AgentMcpRefRow.agent_id == snapshot.asset_id)
                    .order_by(
                        AgentMcpRefRow.sort_order,
                        AgentMcpRefRow.mcp_server_version_id,
                    )
                )
            ).scalars()
        )
        persisted_refs = tuple(SkillAssetRef(AssetScope(scope), asset_id) for scope, asset_id in skill_ref_rows)
        if persisted_refs != snapshot.payload.skill_refs or mcp_ids != snapshot.payload.mcp_version_ids or snapshot.dependency_version_ids != (*snapshot.skill_version_ids, *mcp_ids):
            raise RunSnapshotAssetStale

    @staticmethod
    async def _mcp_secret_closures(
        session: AsyncSession,
        mcps: list[tuple[McpServerRow, McpServerVersionRow]],
        project_id: uuid.UUID,
        request_id: str,
    ) -> dict[uuid.UUID, McpSecretClosure]:
        closures: dict[uuid.UUID, McpSecretClosure] = {}
        for asset, version in mcps:
            slots = tuple(
                (await session.execute(select(McpSecretSlotRow).where(McpSecretSlotRow.mcp_server_version_id == version.id).order_by(McpSecretSlotRow.name, McpSecretSlotRow.id).with_for_update(read=True, of=McpSecretSlotRow)))
                .scalars()
                .all()
            )
            try:
                closures[uuid.UUID(str(version.id))] = await lock_mcp_secret_closure(
                    session,
                    project_id=project_id,
                    mcp_server_id=uuid.UUID(str(asset.id)),
                    mcp_server_version_id=uuid.UUID(str(version.id)),
                    slots=slots,
                    request_id=request_id,
                )
            except SharedAssetError:
                raise RunSnapshotAssetStale from None
        return closures

    @staticmethod
    async def _validate_mcp_discovery_readiness(
        session: AsyncSession,
        mcps: list[tuple[McpServerRow, McpServerVersionRow]],
        closures: Mapping[uuid.UUID, McpSecretClosure],
        *,
        project_id: uuid.UUID,
    ) -> None:
        """Require discovery for the exact admitted Version and secret closure."""

        inventory = McpToolInventoryRepository(session)
        for asset, version in mcps:
            version_id = uuid.UUID(str(version.id))
            try:
                record = await inventory.get(
                    project_id=project_id,
                    mcp_server_id=uuid.UUID(str(asset.id)),
                    mcp_server_version_id=version_id,
                )
                closure = closures[version_id]
            except (KeyError, TypeError, ValueError):
                raise RunSnapshotAssetStale from None
            if (
                record is None
                or record.attempt_status != "ready"
                or record.attempt_payload_checksum != version.payload_checksum
                or record.attempt_secret_digest != closure.digest
                or record.tools_payload_checksum != version.payload_checksum
                or record.tools_secret_digest != closure.digest
                or record.last_success_at is None
            ):
                raise RunSnapshotAssetStale

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
        except RunWorkloadProfileUnsupported:
            raise PrivateWorkRunWorkloadProfileUnsupported(context.request_id) from None
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
        try:
            await require_active_run_skill_writer_cohort(
                session,
                self._run_skill_writer,
            )
        except (
            RunSkillWriterCohortConflict,
            RunSkillWriterCohortUnavailable,
        ):
            raise PrivateWorkUnavailable(context.request_id) from None
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
            skill_secret_closures,
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
        skill_snapshots = () if resolved_closure is None else resolved_closure.skills
        mcp_snapshots = () if resolved_closure is None else resolved_closure.mcps
        if len(skill_snapshots) != len(skills) or len(mcp_snapshots) != len(mcps):
            raise RunSnapshotAssetStale
        prepared_legacy_skills: PreparedLegacyRunSkillSnapshots | None = None
        if self._run_skill_writer.writer_mode == "legacy_v3":
            prepared_legacy_skills = await LegacyRunSkillSnapshotWriter().prepare(
                session,
                request_id=context.request_id,
                locked_skills=tuple(skills),
                snapshots=skill_snapshots,
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
        inherited_workload_profile: EffectiveRunWorkloadProfile | None = None
        if continuation_source_run_id is not None and locked_runtime_policy is not None:
            inherited_workload_profile = await self._continuation_workload_profile(
                session,
                context,
                thread_id=thread_id,
                source_run_id=continuation_source_run_id,
            )
        _, frozen_workload_kwargs = freeze_admitted_run_workload_profile(
            safe_request.kwargs,
            requested=safe_request.workload_profile,
            # Without a runtime-policy service the admission still freezes the
            # baseline v1 workload contract.
            policy_schema_version=(locked_runtime_policy.schema_version if locked_runtime_policy is not None else 1),
            inherited_effective=inherited_workload_profile,
        )
        safe_request = replace(safe_request, kwargs=frozen_workload_kwargs)
        run = await PrivateRunRepository(session).create_for_snapshot_assembly(
            scope=context.resource_scope,
            thread_id=thread_id,
            request=safe_request,
        )
        await session.scalar(
            select(
                func.set_config(
                    "deerflow.run_asset_closure_assembly",
                    run.run_id,
                    True,
                )
            )
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
        planned_rows = plan_run_asset_rows(
            context=context,
            thread_id=thread_id,
            run=run,
            lead_agent=lead_agent,
            resolved_closure=resolved_closure,
            skills=skills,
            skill_snapshots=skill_snapshots,
            mcps=mcps,
            mcp_snapshots=mcp_snapshots,
            prepared_legacy_skills=prepared_legacy_skills,
        )
        asset_rows = planned_rows.asset_rows
        skill_ref_rows = planned_rows.skill_ref_rows
        session.add_all(asset_rows)
        session.add_all(skill_ref_rows)
        session.add_all(
            RunMcpSecretSnapshotRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                mcp_server_id=material.mcp_server_id,
                mcp_server_version_id=material.mcp_server_version_id,
                slot_id=material.slot_id,
                secret_revision=material.revision,
                secret_generation_id=material.generation_id,
                secret_generation_digest=material.generation_digest,
            )
            for _asset, version in mcps
            for material in closures[uuid.UUID(str(version.id))].materials
        )
        session.add_all(
            RunSkillSecretSnapshotRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                skill_id=material.skill_id,
                skill_version_id=material.skill_version_id,
                secret_name=material.secret_name,
                secret_revision=material.secret_revision,
                secret_generation_id=material.secret_generation_id,
                secret_generation_digest=material.secret_generation_digest,
            )
            for _asset, version in skills
            for material in skill_secret_closures[uuid.UUID(str(version.id))].materials
        )
        await session.flush()
        run = await PrivateRunRepository(session).seal_asset_closure(
            scope=context.resource_scope,
            run_id=run.run_id,
        )
        await PrivateThreadRepository(session).touch_activity(
            scope=context.resource_scope,
            thread_id=thread_id,
            occurred_at=run.created_at,
            thread_kind=runtime_kind,
        )
        return run

    async def validate_run_asset_closure_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        closure: ResolvedRunAssetClosure,
    ) -> tuple[
        list[tuple[SkillRow, SkillVersionRow]],
        list[tuple[McpServerRow, McpServerVersionRow]],
        dict[uuid.UUID, McpSecretClosure],
        dict[uuid.UUID, LockedSkillSecretClosure],
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
            skill_secret_closures = await lock_skill_secret_closures(
                session,
                context.project_id,
                tuple(
                    SkillSecretClosureTarget(
                        skill_id=uuid.UUID(str(asset.id)),
                        skill_version_id=uuid.UUID(str(version.id)),
                    )
                    for asset, version in skills
                ),
                load_envelopes=False,
                require_required=True,
            )
        except SkillSecretClosureInvalid:
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
        closures = await self._mcp_secret_closures(
            session,
            mcps,
            context.project_id,
            context.request_id,
        )
        self._validate_project_mcp_secret_slots(
            mcps,
            closures,
            endpoint_policy=self._endpoint_policy,
        )
        await self._validate_mcp_discovery_readiness(
            session,
            mcps,
            closures,
            project_id=context.project_id,
        )
        return skills, mcps, closures, skill_secret_closures

    async def validate_agent_closure_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        resolved_agent: ResolvedAgentSnapshot,
    ) -> tuple[
        list[tuple[SkillRow, SkillVersionRow]],
        list[tuple[McpServerRow, McpServerVersionRow]],
        dict[uuid.UUID, McpSecretClosure],
        dict[uuid.UUID, LockedSkillSecretClosure],
    ]:
        """Lock and validate an Agent plus its exact MCP secret closure."""

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
            skill_secret_closures = await lock_skill_secret_closures(
                session,
                project_id,
                tuple(
                    SkillSecretClosureTarget(
                        skill_id=uuid.UUID(str(asset.id)),
                        skill_version_id=uuid.UUID(str(version.id)),
                    )
                    for asset, version in skills
                ),
                load_envelopes=False,
                require_required=True,
            )
        except SkillSecretClosureInvalid:
            raise RunSnapshotAssetStale from None
        mcps = await self._mcps(
            session,
            resolved_agent.payload.mcp_version_ids,
            project_id,
            endpoint_policy=self._endpoint_policy,
        )
        closures = await self._mcp_secret_closures(
            session,
            mcps,
            context.project_id,
            context.request_id,
        )
        self._validate_project_mcp_secret_slots(
            mcps,
            closures,
            endpoint_policy=self._endpoint_policy,
        )
        await self._validate_mcp_discovery_readiness(
            session,
            mcps,
            closures,
            project_id=context.project_id,
        )
        return skills, mcps, closures, skill_secret_closures

    async def list_asset_facts_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[ResolvedRunAssetFact, ...]:
        """Read ordered frozen asset metadata without loading snapshot JSON."""

        context = require_issued_private_work_context(context)
        conditions = [
            RunAssetVersionRow.project_id == context.project_id,
            RunAssetVersionRow.owner_user_id == str(context.user_id),
            RunAssetVersionRow.thread_id == thread_id,
            RunAssetVersionRow.run_id == run_id,
        ]
        statement = (
            select(
                RunAssetVersionRow.asset_kind,
                RunAssetVersionRow.dependency_order,
                RunAssetVersionRow.asset_scope,
                RunAssetVersionRow.asset_id,
                RunAssetVersionRow.version_id,
                RunAssetVersionRow.payload_checksum,
                RunAssetVersionRow.catalog_generation,
            )
            .where(*conditions)
            .order_by(RunAssetVersionRow.dependency_order)
        )
        if lock:
            statement = statement.with_for_update(of=RunAssetVersionRow)
        rows = (await session.execute(statement)).all()
        return tuple(
            ResolvedRunAssetFact(
                kind=AssetKind(asset_kind),
                dependency_order=dependency_order,
                scope=AssetScope(asset_scope),
                asset_id=asset_id,
                version_id=version_id,
                checksum=payload_checksum,
                catalog_generation=catalog_generation,
            )
            for (
                asset_kind,
                dependency_order,
                asset_scope,
                asset_id,
                version_id,
                payload_checksum,
                catalog_generation,
            ) in rows
        )

    async def list_mcp_secrets(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunMcpSecretSnapshot, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                return await self.list_mcp_secrets_in_session(session, context, run_id)
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_mcp_secrets_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunMcpSecretSnapshot, ...]:
        context = require_issued_private_work_context(context)
        statement = (
            select(RunMcpSecretSnapshotRow)
            .where(
                RunMcpSecretSnapshotRow.project_id == context.project_id,
                RunMcpSecretSnapshotRow.owner_user_id == str(context.user_id),
                RunMcpSecretSnapshotRow.run_id == run_id,
            )
            .order_by(
                RunMcpSecretSnapshotRow.mcp_server_version_id,
                RunMcpSecretSnapshotRow.slot_id,
            )
        )
        if lock:
            statement = statement.with_for_update(of=RunMcpSecretSnapshotRow)
        rows = (await session.execute(statement)).scalars()
        return tuple(
            RunMcpSecretSnapshot(
                mcp_server_id=row.mcp_server_id,
                mcp_server_version_id=row.mcp_server_version_id,
                slot_id=row.slot_id,
                secret_revision=row.secret_revision,
                secret_generation_id=row.secret_generation_id,
                secret_generation_digest=row.secret_generation_digest,
            )
            for row in rows
        )

    async def current_mcp_secrets_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        mcp_assets: tuple[ResolvedRunAssetFact, ...],
    ) -> tuple[RunMcpSecretSnapshot, ...]:
        """Lock the current exact closure and return only its secret-free IDs."""

        context = require_issued_private_work_context(context)
        if any(asset.kind is not AssetKind.MCP for asset in mcp_assets):
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
            if asset.id != persisted.asset_id or asset.scope != persisted.scope.value or version.payload_checksum != persisted.checksum:
                raise RunSnapshotAssetStale
        closures = await self._mcp_secret_closures(
            session,
            mcps,
            context.project_id,
            context.request_id,
        )
        self._validate_project_mcp_secret_slots(
            mcps,
            closures,
            endpoint_policy=self._endpoint_policy,
        )
        current = [
            RunMcpSecretSnapshot(
                mcp_server_id=material.mcp_server_id,
                mcp_server_version_id=material.mcp_server_version_id,
                slot_id=material.slot_id,
                secret_revision=material.revision,
                secret_generation_id=material.generation_id,
                secret_generation_digest=material.generation_digest,
            )
            for _asset, version in mcps
            for material in closures[uuid.UUID(str(version.id))].materials
        ]
        return tuple(
            sorted(
                current,
                key=lambda item: (
                    item.mcp_server_version_id.int,
                    item.slot_id.int,
                    item.secret_generation_id.int,
                ),
            )
        )

    async def list_skill_secrets(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunSkillSecretSnapshot, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                return await self.list_skill_secrets_in_session(
                    session,
                    context,
                    run_id,
                )
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_skill_secrets_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunSkillSecretSnapshot, ...]:
        context = require_issued_private_work_context(context)
        statement = (
            select(RunSkillSecretSnapshotRow)
            .where(
                RunSkillSecretSnapshotRow.project_id == context.project_id,
                RunSkillSecretSnapshotRow.owner_user_id == str(context.user_id),
                RunSkillSecretSnapshotRow.run_id == run_id,
            )
            .order_by(
                RunSkillSecretSnapshotRow.skill_version_id,
                RunSkillSecretSnapshotRow.secret_name,
            )
        )
        if lock:
            statement = statement.with_for_update(
                of=RunSkillSecretSnapshotRow,
            )
        rows = (await session.execute(statement)).scalars()
        return tuple(
            RunSkillSecretSnapshot(
                skill_id=row.skill_id,
                skill_version_id=row.skill_version_id,
                secret_name=row.secret_name,
                secret_revision=row.secret_revision,
                secret_generation_id=row.secret_generation_id,
                secret_generation_digest=row.secret_generation_digest,
            )
            for row in rows
        )

    async def current_skill_secrets_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        skill_assets: tuple[ResolvedRunAssetFact, ...],
    ) -> tuple[RunSkillSecretSnapshot, ...]:
        """Lock current Skill secret Generations and return secret-free IDs."""

        context = require_issued_private_work_context(context)
        if any(asset.kind is not AssetKind.SKILL for asset in skill_assets):
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
            if asset.id != persisted.asset_id or asset.scope != persisted.scope.value or version.payload_checksum != persisted.checksum:
                raise RunSnapshotAssetStale
        try:
            closures = await lock_skill_secret_closures(
                session,
                context.project_id,
                tuple(
                    SkillSecretClosureTarget(
                        skill_id=uuid.UUID(str(asset.id)),
                        skill_version_id=uuid.UUID(str(version.id)),
                    )
                    for asset, version in skills
                ),
                load_envelopes=False,
                require_required=True,
            )
        except SkillSecretClosureInvalid:
            raise RunSnapshotAssetStale from None
        current = [
            RunSkillSecretSnapshot(
                skill_id=material.skill_id,
                skill_version_id=material.skill_version_id,
                secret_name=material.secret_name,
                secret_revision=material.secret_revision,
                secret_generation_id=material.secret_generation_id,
                secret_generation_digest=material.secret_generation_digest,
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
                    item.secret_generation_id.int,
                ),
            )
        )

    async def lock_admitted_skill_secrets_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        persisted: tuple[RunSkillSecretSnapshot, ...],
        *,
        declared_targets: frozenset[tuple[uuid.UUID, str]],
        required_targets: frozenset[tuple[uuid.UUID, str]],
        load_envelopes: bool = False,
    ) -> tuple[LockedSkillSecretMaterial, ...]:
        """Lock exact admitted Generations without rereading Skill state."""

        context = require_issued_private_work_context(context)
        try:
            return await lock_admitted_skill_secret_materials(
                session,
                context.project_id,
                tuple(
                    AdmittedSkillSecretReference(
                        skill_id=item.skill_id,
                        skill_version_id=item.skill_version_id,
                        secret_name=item.secret_name,
                        secret_revision=item.secret_revision,
                        secret_generation_id=item.secret_generation_id,
                        secret_generation_digest=item.secret_generation_digest,
                    )
                    for item in persisted
                ),
                declared_targets=declared_targets,
                required_targets=required_targets,
                load_envelopes=load_envelopes,
            )
        except SkillSecretClosureInvalid:
            raise RunSnapshotAssetStale from None
