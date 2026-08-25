from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_payload_checksum import (
    agent_payload_checksum,
    persisted_agent_payload_checksum_matches,
)
from app.shared_assets.binding_repository import BindingRepository
from app.shared_assets.catalog_state_repository import CatalogStateRepository
from app.shared_assets.errors import (
    AgentArchived,
    AssetForbidden,
    AssetNotFound,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.internal_assets import (
    BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
)
from app.shared_assets.mcp_repository import McpVersionRecord
from app.shared_assets.mcp_secret_closure import (
    McpSecretClosure,
    lock_mcp_secret_closure,
)
from app.shared_assets.mcp_secret_store import McpSecretStore
from app.shared_assets.mcp_service import McpService
from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedAssetSnapshot,
    ResolvedMcpSnapshot,
    ResolvedRunAssetClosure,
    ResolvedRunAssetFact,
    ResolvedSkillSnapshot,
    ResolvedSkillVersionSnapshot,
    SkillAssetRef,
    SkillSecretRequirementSnapshot,
    WorkflowStatus,
)
from app.shared_assets.skill_repository import SkillVersionRecord
from app.shared_assets.skill_secret_policy import parse_skill_secret_declarations
from app.shared_assets.skill_service import SkillService
from app.system_settings.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from deerflow.persistence.shared_assets import (
    AgentMcpRefRow,
    AgentRow,
    AgentSkillRefRow,
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.secrets import SecretKey


@dataclass(frozen=True)
class MaterializedMcpSecrets:
    """Short-lived MCP plaintext; the secret mapping is intentionally repr-hidden."""

    mcp_version_id: uuid.UUID
    by_slot: Mapping[str, Mapping[str, object]] = field(repr=False)


@dataclass(frozen=True)
class _ResolvedRecord:
    scope: AssetScope
    asset: AgentRow | SkillRow | McpServerRow
    version: AgentRow | SkillVersionRow | McpServerVersionRow

    @property
    def version_id(self) -> uuid.UUID:
        return self.version.definition_id if isinstance(self.version, AgentRow) else self.version.id

    @property
    def payload_checksum(self) -> str:
        return self.version.payload_checksum


@dataclass(frozen=True)
class _RunAssetClosurePlan:
    lead: ResolvedAgentSnapshot
    delegated_agents: tuple[ResolvedAgentSnapshot, ...]
    skill_records: tuple[_ResolvedRecord, ...]
    mcp_records: tuple[_ResolvedRecord, ...]
    main_skill_count: int
    main_mcp_count: int


_ASSET_TYPES = {
    AssetKind.SKILL: (SkillRow, SkillVersionRow, "skill_id"),
    AssetKind.MCP: (McpServerRow, McpServerVersionRow, "mcp_server_id"),
}
_BINDING_TYPES = {
    AssetKind.AGENT: (
        ProjectSystemAgentBindingRow,
        "system_agent_id",
        None,
    ),
    AssetKind.SKILL: (
        ProjectSystemSkillBindingRow,
        "system_skill_id",
        None,
    ),
    AssetKind.MCP: (
        ProjectSystemMcpBindingRow,
        "system_mcp_server_id",
        "mcp_server_version_id",
    ),
}

BUILTIN_MAIN_AGENT_SOURCE_KEY = "builtin:agent:project-assistant"
BUILTIN_SKILL_CREATOR_SOURCE_KEY = "builtin:skill:skill-creator"


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


class ProjectAssetResolver:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        secret_key: SecretKey | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_key = secret_key

    async def resolve_project_asset_snapshot(
        self,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> ResolvedAssetSnapshot:
        self._validate_resolve_input(context, selection)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = BindingRepository(session)
                    await repository.lock_project(context, read=True)
                    record = await self._resolve_record(session, repository, context, selection)
                    snapshot = await self._snapshot(
                        session,
                        context,
                        selection.kind,
                        record,
                        0,
                    )
                    generation = await CatalogStateRepository(session).read_generation()
                    return replace(snapshot, catalog_generation=generation)
        except (AssetForbidden, AssetValidationFailed):
            raise
        except AssetResolutionUnavailable:
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(context.request_id) from None

    async def resolve_project_asset_snapshot_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> ResolvedAssetSnapshot:
        """Resolve an exact snapshot inside a caller-owned transaction.

        The caller must already hold the project/membership locks.  This is the
        same resolver path as :meth:`resolve_project_asset_snapshot`, without a
        nested session or a second project lock, so private-run admission can
        preserve project -> membership -> Thread -> Run/assets ordering.
        """

        self._validate_resolve_input(context, selection)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise AssetValidationFailed(context.request_id)
        try:
            repository = BindingRepository(session)
            record = await self._resolve_record(session, repository, context, selection)
            snapshot = await self._snapshot(
                session,
                context,
                selection.kind,
                record,
                0,
            )
            generation = await CatalogStateRepository(session).read_generation()
            return replace(snapshot, catalog_generation=generation)
        except (AssetForbidden, AssetValidationFailed, AssetResolutionUnavailable):
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(context.request_id) from None

    async def resolve_run_asset_closure_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> ResolvedRunAssetClosure:
        """Resolve the exact Agent/Skill/MCP closure admitted for one Run.

        Every Agent reference resolves its current Definition and every Skill
        reference resolves its Current Version under the admission transaction.
        MCP keeps its established exact release binding. The resulting closure
        is persisted in full and is the Worker's sole executable asset source.
        """

        self._validate_resolve_input(context, selection)
        if selection.kind is not AssetKind.AGENT:
            raise AssetValidationFailed(context.request_id)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise AssetValidationFailed(context.request_id)
        try:
            plan = await self._resolve_run_asset_closure_plan_in_session(
                session,
                context,
                selection,
            )
            skills = tuple(
                [
                    await self._skill_version_snapshot(
                        session,
                        context,
                        record,
                        0,
                    )
                    for record in plan.skill_records
                ]
            )
            mcps = tuple([await self._mcp_snapshot(session, context, record, 0) for record in plan.mcp_records])
            return await self._finalize_run_closure(
                session,
                lead=plan.lead,
                delegated_agents=plan.delegated_agents,
                skills=skills,
                mcps=mcps,
                main_skill_version_ids=tuple(item.version_id for item in skills[: plan.main_skill_count]),
                main_mcp_version_ids=tuple(item.version_id for item in mcps[: plan.main_mcp_count]),
            )
        except (AssetForbidden, AssetValidationFailed, AssetResolutionUnavailable):
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(context.request_id) from None

    async def resolve_run_asset_facts_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> tuple[ResolvedRunAssetFact, ...]:
        """Resolve ordered closure identity without loading Skill file bytes."""

        self._validate_resolve_input(context, selection)
        if selection.kind is not AssetKind.AGENT:
            raise AssetValidationFailed(context.request_id)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise AssetValidationFailed(context.request_id)
        try:
            plan = await self._resolve_run_asset_closure_plan_in_session(
                session,
                context,
                selection,
            )
            generation = await CatalogStateRepository(session).read_generation()
            facts = [
                ResolvedRunAssetFact(
                    kind=AssetKind.AGENT,
                    dependency_order=dependency_order,
                    scope=agent.scope,
                    asset_id=agent.asset_id,
                    version_id=agent.version_id,
                    checksum=agent.checksum,
                    catalog_generation=generation,
                )
                for dependency_order, agent in enumerate((plan.lead, *plan.delegated_agents))
            ]
            for kind, records in (
                (AssetKind.SKILL, plan.skill_records),
                (AssetKind.MCP, plan.mcp_records),
            ):
                for record in records:
                    facts.append(
                        ResolvedRunAssetFact(
                            kind=kind,
                            dependency_order=len(facts),
                            scope=record.scope,
                            asset_id=record.asset.id,
                            version_id=record.version_id,
                            checksum=record.payload_checksum,
                            catalog_generation=generation,
                        )
                    )
            return tuple(facts)
        except (AssetForbidden, AssetValidationFailed, AssetResolutionUnavailable):
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(context.request_id) from None

    async def _resolve_run_asset_closure_plan_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> _RunAssetClosurePlan:
        repository = BindingRepository(session)
        lead_record = await self._resolve_record(
            session,
            repository,
            context,
            selection,
        )
        (
            lead,
            lead_skill_records,
            lead_mcp_records,
        ) = await self._agent_snapshot_with_dependencies(
            session,
            context,
            lead_record,
            0,
        )
        if not self._is_canonical_main_record(lead_record):
            self._assert_unique_skill_runtime_names(
                lead_skill_records,
                context.request_id,
            )
            self._assert_one_version_per_asset(
                lead_skill_records,
                context.request_id,
            )
            self._assert_one_version_per_asset(
                lead_mcp_records,
                context.request_id,
            )
            return _RunAssetClosurePlan(
                lead=lead,
                delegated_agents=(),
                skill_records=lead_skill_records,
                mcp_records=lead_mcp_records,
                main_skill_count=len(lead_skill_records),
                main_mcp_count=len(lead_mcp_records),
            )

        delegated_records = tuple(
            record
            for record in await self._main_pool_records(
                session,
                context,
                AssetKind.AGENT,
            )
            if record.asset.id != lead.asset_id
        )
        main_skill_records = await self._main_pool_records(
            session,
            context,
            AssetKind.SKILL,
        )
        main_mcp_records = await self._main_pool_records(
            session,
            context,
            AssetKind.MCP,
        )
        delegated_agent_items: list[ResolvedAgentSnapshot] = []
        delegated_skill_records: list[tuple[_ResolvedRecord, ...]] = []
        delegated_mcp_records: list[tuple[_ResolvedRecord, ...]] = []
        for record in delegated_records:
            try:
                (
                    delegated_agent,
                    skill_dependencies,
                    mcp_dependencies,
                ) = await self._agent_snapshot_with_dependencies(
                    session,
                    context,
                    record,
                    0,
                )
            except AssetResolutionUnavailable:
                # Main exposes only executable delegate candidates.  One
                # project Agent with a stale/suspended exact dependency must
                # not make the canonical project entry unavailable. Storage
                # failures are intentionally not swallowed here.
                continue
            delegated_agent_items.append(delegated_agent)
            delegated_skill_records.append(skill_dependencies)
            delegated_mcp_records.append(mcp_dependencies)
        skill_records = list(main_skill_records)
        mcp_records = list(main_mcp_records)
        self._append_delegate_only_dependencies(
            tuple(delegated_skill_records),
            skill_records,
            context.request_id,
        )
        self._append_delegate_only_dependencies(
            tuple(delegated_mcp_records),
            mcp_records,
            context.request_id,
        )
        self._assert_unique_skill_runtime_names(
            skill_records,
            context.request_id,
        )
        return _RunAssetClosurePlan(
            lead=lead,
            delegated_agents=tuple(delegated_agent_items),
            skill_records=tuple(skill_records),
            mcp_records=tuple(mcp_records),
            main_skill_count=len(main_skill_records),
            main_mcp_count=len(main_mcp_records),
        )

    async def resolve_internal_skill_builder_closure_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> ResolvedRunAssetClosure:
        """Resolve the one server-owned Skill Builder execution closure.

        This is deliberately separate from the public project resolver: the
        packaged Builder Agent is an implementation detail and never becomes
        generally executable merely because a caller knows its UUID.
        """

        if (
            not isinstance(session, AsyncSession)
            or not session.in_transaction()
            or type(context) is not ProjectContext
            or Capability.SHARED_ASSETS_EDIT not in context.capabilities
            or Capability.SHARED_ASSETS_READ not in context.capabilities
        ):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))
        try:
            record = await self._internal_system_record(
                session,
                context,
                kind=AssetKind.AGENT,
                source_key=BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
            )
            version = record.version
            if not isinstance(version, AgentRow):
                raise AssetResolutionUnavailable(context.request_id)
            skill_refs = tuple(
                (
                    await session.execute(
                        select(
                            AgentSkillRefRow.skill_asset_scope,
                            AgentSkillRefRow.skill_asset_id,
                        )
                        .where(
                            AgentSkillRefRow.agent_id == version.id,
                        )
                        .order_by(AgentSkillRefRow.sort_order)
                        .with_for_update(read=True, of=AgentSkillRefRow)
                    )
                ).all()
            )
            mcp_ids = tuple(
                (
                    await session.execute(
                        select(AgentMcpRefRow.mcp_server_version_id)
                        .where(
                            AgentMcpRefRow.agent_id == version.id,
                        )
                        .order_by(AgentMcpRefRow.sort_order)
                        .with_for_update(read=True, of=AgentMcpRefRow)
                    )
                )
                .scalars()
                .all()
            )
            if mcp_ids or len(skill_refs) != 1 or skill_refs[0][0] != AssetScope.SYSTEM.value:
                raise AssetResolutionUnavailable(context.request_id)
            # The packaged creator is an implementation dependency of the
            # internal Builder Agent.  Resolve its exact immutable reference
            # by canonical source key; requiring a project System binding here
            # would make a fresh project unable to run the server-owned Agent.
            skill_record = await self._internal_system_record(
                session,
                context,
                kind=AssetKind.SKILL,
                source_key=BUILTIN_SKILL_CREATOR_SOURCE_KEY,
                asset_id=skill_refs[0][1],
            )
            lead = await self._agent_snapshot(
                session,
                context,
                record,
                0,
                exact_dependency_records=((skill_record,), ()),
            )
            skill = await self._skill_version_snapshot(
                session,
                context,
                skill_record,
                0,
            )
            return await self._finalize_run_closure(
                session,
                lead=lead,
                delegated_agents=(),
                skills=(skill,),
                mcps=(),
                main_skill_version_ids=(skill.version_id,),
                main_mcp_version_ids=(),
            )
        except (AssetForbidden, AssetValidationFailed, AssetResolutionUnavailable):
            raise
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(context.request_id) from None

    async def resolve_internal_skill_builder_snapshot_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> ResolvedAssetSnapshot:
        """Re-materialize only exact assets admitted for a Builder Run."""

        if not isinstance(session, AsyncSession) or not session.in_transaction() or type(context) is not ProjectContext or Capability.SHARED_ASSETS_EDIT not in context.capabilities or selection.version_id is None:
            raise AssetForbidden(getattr(context, "request_id", "unknown"))
        source_key = {
            AssetKind.AGENT: BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
            AssetKind.SKILL: BUILTIN_SKILL_CREATOR_SOURCE_KEY,
        }.get(selection.kind)
        if source_key is None:
            raise AssetResolutionUnavailable(context.request_id)
        record = await self._internal_system_record(
            session,
            context,
            kind=selection.kind,
            source_key=source_key,
            asset_id=selection.asset_id,
            version_id=selection.version_id,
        )
        snapshot = await self._snapshot(
            session,
            context,
            selection.kind,
            record,
            0,
        )
        generation = await CatalogStateRepository(session).read_generation()
        return replace(snapshot, catalog_generation=generation)

    @staticmethod
    async def _internal_system_record(
        session: AsyncSession,
        context: ProjectContext,
        *,
        kind: AssetKind,
        source_key: str,
        asset_id: uuid.UUID | None = None,
        version_id: uuid.UUID | None = None,
    ) -> _ResolvedRecord:
        if kind not in {AssetKind.AGENT, AssetKind.SKILL}:
            raise AssetResolutionUnavailable(context.request_id)
        if kind is AssetKind.AGENT:
            statement = (
                select(AgentRow)
                .where(
                    AgentRow.scope == AssetScope.SYSTEM.value,
                    AgentRow.project_id.is_(None),
                    AgentRow.source_key == source_key,
                    AgentRow.status == "active",
                )
                .with_for_update(read=True, of=AgentRow)
            )
            if asset_id is not None:
                statement = statement.where(AgentRow.id == asset_id)
            if version_id is not None:
                statement = statement.where(AgentRow.definition_id == version_id)
            agent = (await session.execute(statement)).scalar_one_or_none()
            if agent is None:
                raise AssetResolutionUnavailable(context.request_id)
            return _ResolvedRecord(AssetScope.SYSTEM, agent, agent)
        asset_type, version_type, parent_column = _ASSET_TYPES[kind]
        statement = (
            select(asset_type, version_type)
            .join(
                version_type,
                getattr(version_type, parent_column) == asset_type.id,
            )
            .where(
                asset_type.scope == AssetScope.SYSTEM.value,
                asset_type.project_id.is_(None),
                asset_type.source_key == source_key,
                asset_type.status == "active",
                version_type.id == asset_type.current_version_id,
                version_type.version_number == 1,
            )
            .with_for_update(read=True, of=[asset_type, version_type])
        )
        if asset_id is not None:
            statement = statement.where(asset_type.id == asset_id)
        if version_id is not None:
            statement = statement.where(version_type.id == version_id)
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise AssetResolutionUnavailable(context.request_id)
        asset, version = row
        if kind is AssetKind.SKILL and version.revoked_at is not None:
            raise AssetResolutionUnavailable(context.request_id)
        return _ResolvedRecord(AssetScope.SYSTEM, asset, version)

    async def resolve_run_asset_snapshot_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> ResolvedAssetSnapshot:
        """Resolve one exact current snapshot for maintenance-only callers.

        Worker execution uses the self-contained persisted Run Snapshot and
        never calls this catalog resolver.
        """

        self._validate_resolve_input(context, selection)
        if selection.version_id is None:
            raise AssetValidationFailed(context.request_id)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise AssetValidationFailed(context.request_id)
        try:
            repository = BindingRepository(session)
            record = await self._resolve_run_record(
                session,
                repository,
                context,
                selection,
            )
            snapshot = await self._snapshot(
                session,
                context,
                selection.kind,
                record,
                0,
            )
            generation = await CatalogStateRepository(session).read_generation()
            return replace(snapshot, catalog_generation=generation)
        except (AssetForbidden, AssetValidationFailed, AssetResolutionUnavailable):
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(context.request_id) from None

    async def _resolve_run_record(
        self,
        session: AsyncSession,
        repository: BindingRepository,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> _ResolvedRecord:
        version_id = selection.version_id
        if version_id is None:
            raise AssetValidationFailed(context.request_id)
        if selection.kind is AssetKind.AGENT:
            project_agent = (
                await session.execute(
                    select(AgentRow)
                    .where(
                        AgentRow.id == selection.asset_id,
                        AgentRow.scope == AssetScope.PROJECT.value,
                        AgentRow.project_id == context.project_id,
                        AgentRow.status.in_(("active", "archived")),
                        AgentRow.definition_id == version_id,
                    )
                    .with_for_update(read=True, of=AgentRow)
                )
            ).scalar_one_or_none()
            if project_agent is not None:
                return _ResolvedRecord(AssetScope.PROJECT, project_agent, project_agent)

            main_agent = (
                await session.execute(
                    select(AgentRow)
                    .where(
                        AgentRow.id == selection.asset_id,
                        AgentRow.scope == AssetScope.SYSTEM.value,
                        AgentRow.project_id.is_(None),
                        AgentRow.source_key == BUILTIN_MAIN_AGENT_SOURCE_KEY,
                        AgentRow.status == "active",
                        AgentRow.definition_id == version_id,
                    )
                    .with_for_update(read=True, of=AgentRow)
                )
            ).scalar_one_or_none()
            if main_agent is not None:
                return _ResolvedRecord(AssetScope.SYSTEM, main_agent, main_agent)

            binding = await repository.get_binding(
                context,
                AssetKind.AGENT,
                selection.asset_id,
                for_update=True,
                read=True,
                required=False,
            )
            if binding is None or not binding.enabled:
                raise AssetResolutionUnavailable(context.request_id)
            try:
                target = await repository.lock_target(
                    context,
                    selection,
                    allow_archived=True,
                    read=True,
                )
            except SharedAssetError:
                raise AssetResolutionUnavailable(context.request_id) from None
            if target.asset.status != "active" or target.version_id != version_id:
                raise AssetResolutionUnavailable(context.request_id)
            record = _ResolvedRecord(AssetScope.SYSTEM, target.asset, target.version)
            self._assert_public_agent_record(record, context.request_id)
            return record

        asset_type, version_type, parent_column = _ASSET_TYPES[selection.kind]
        project_row = (
            await session.execute(
                select(asset_type, version_type)
                .join(
                    version_type,
                    getattr(version_type, parent_column) == asset_type.id,
                )
                .where(
                    asset_type.id == selection.asset_id,
                    asset_type.scope == AssetScope.PROJECT.value,
                    asset_type.project_id == context.project_id,
                    (asset_type.status.in_(("active", "archived")) if selection.kind is AssetKind.AGENT else asset_type.status == "active"),
                    version_type.id == version_id,
                    *((version_type.workflow_status == WorkflowStatus.PUBLISHED.value,) if selection.kind is AssetKind.MCP else (asset_type.current_version_id == version_id,)),
                )
                .with_for_update(read=True, of=[asset_type, version_type])
            )
        ).one_or_none()
        if project_row is not None:
            asset, version = project_row
            return _ResolvedRecord(AssetScope.PROJECT, asset, version)

        binding = await repository.get_binding(
            context,
            selection.kind,
            selection.asset_id,
            for_update=True,
            read=True,
            required=False,
        )
        if binding is None or not binding.enabled:
            raise AssetResolutionUnavailable(context.request_id)
        _binding_type, _asset_column, version_column = _BINDING_TYPES[selection.kind]
        if version_column is not None and getattr(binding, version_column) != version_id:
            raise AssetResolutionUnavailable(context.request_id)
        try:
            target = await repository.lock_target(
                context,
                selection,
                allow_archived=True,
                read=True,
            )
        except SharedAssetError:
            raise AssetResolutionUnavailable(context.request_id) from None
        if target.asset.status != "active" or target.version_id != version_id:
            raise AssetResolutionUnavailable(context.request_id)
        record = _ResolvedRecord(AssetScope.SYSTEM, target.asset, target.version)
        self._assert_public_agent_record(record, context.request_id)
        return record

    @staticmethod
    def _is_canonical_main_record(record: _ResolvedRecord) -> bool:
        return record.scope is AssetScope.SYSTEM and isinstance(record.asset, AgentRow) and record.asset.source_key == BUILTIN_MAIN_AGENT_SOURCE_KEY

    @staticmethod
    def _assert_one_version_per_asset(
        records: Sequence[_ResolvedRecord],
        request_id: str,
    ) -> None:
        if len({record.asset.id for record in records}) != len(records):
            raise AssetResolutionUnavailable(request_id)

    @staticmethod
    def _assert_unique_skill_runtime_names(
        records: Sequence[_ResolvedRecord],
        request_id: str,
    ) -> None:
        assets_by_name: dict[str, uuid.UUID] = {}
        for record in records:
            if not isinstance(record.asset, SkillRow):
                raise AssetResolutionUnavailable(request_id)
            asset_id = uuid.UUID(str(record.asset.id))
            runtime_name = record.asset.slug.casefold()
            existing = assets_by_name.get(runtime_name)
            if existing is not None and existing != asset_id:
                raise AssetResolutionUnavailable(request_id)
            assets_by_name[runtime_name] = asset_id

    async def _finalize_run_closure(
        self,
        session: AsyncSession,
        *,
        lead: ResolvedAgentSnapshot,
        delegated_agents: tuple[ResolvedAgentSnapshot, ...],
        skills: tuple[ResolvedSkillVersionSnapshot, ...],
        mcps: tuple[ResolvedMcpSnapshot, ...],
        main_skill_version_ids: tuple[uuid.UUID, ...],
        main_mcp_version_ids: tuple[uuid.UUID, ...],
    ) -> ResolvedRunAssetClosure:
        generation = await CatalogStateRepository(session).read_generation()
        return ResolvedRunAssetClosure(
            lead_agent=replace(lead, catalog_generation=generation),
            delegated_agents=tuple(replace(item, catalog_generation=generation) for item in delegated_agents),
            skills=tuple(replace(item, catalog_generation=generation) for item in skills),
            mcps=tuple(replace(item, catalog_generation=generation) for item in mcps),
            main_skill_version_ids=main_skill_version_ids,
            main_mcp_version_ids=main_mcp_version_ids,
        )

    async def _main_pool_records(
        self,
        session: AsyncSession,
        context: ProjectContext,
        kind: AssetKind,
    ) -> tuple[_ResolvedRecord, ...]:
        if kind is AssetKind.AGENT:
            project_agents = tuple(
                (
                    await session.execute(
                        select(AgentRow)
                        .where(
                            AgentRow.scope == AssetScope.PROJECT.value,
                            AgentRow.project_id == context.project_id,
                            AgentRow.status == "active",
                        )
                        .order_by(AgentRow.id, AgentRow.definition_id)
                        .with_for_update(read=True, of=AgentRow)
                    )
                )
                .scalars()
                .all()
            )
            system_agents = tuple(
                (
                    await session.execute(
                        select(AgentRow)
                        .join(
                            ProjectSystemAgentBindingRow,
                            ProjectSystemAgentBindingRow.system_agent_id == AgentRow.id,
                        )
                        .where(
                            ProjectSystemAgentBindingRow.project_id == context.project_id,
                            ProjectSystemAgentBindingRow.enabled.is_(True),
                            AgentRow.scope == AssetScope.SYSTEM.value,
                            AgentRow.project_id.is_(None),
                            AgentRow.status == "active",
                        )
                        .order_by(AgentRow.id, AgentRow.definition_id)
                        .with_for_update(
                            read=True,
                            of=[ProjectSystemAgentBindingRow, AgentRow],
                        )
                    )
                )
                .scalars()
                .all()
            )
            records = [
                *(_ResolvedRecord(AssetScope.PROJECT, agent, agent) for agent in project_agents),
                *(_ResolvedRecord(AssetScope.SYSTEM, agent, agent) for agent in system_agents),
            ]
            for record in records:
                self._assert_public_agent_record(record, context.request_id)
            records.sort(
                key=lambda record: (
                    uuid.UUID(str(record.asset.id)).int,
                    record.version_id.int,
                )
            )
            return tuple(records)

        asset_type, version_type, parent_column = _ASSET_TYPES[kind]
        pointer_column = asset_type.current_published_version_id if kind is AssetKind.MCP else asset_type.current_version_id
        project_rows = tuple(
            (
                await session.execute(
                    select(asset_type, version_type)
                    .join(
                        version_type,
                        version_type.id == pointer_column,
                    )
                    .where(
                        asset_type.scope == AssetScope.PROJECT.value,
                        asset_type.project_id == context.project_id,
                        asset_type.status == "active",
                        getattr(version_type, parent_column) == asset_type.id,
                        *((version_type.workflow_status == WorkflowStatus.PUBLISHED.value,) if kind is AssetKind.MCP else ()),
                    )
                    .order_by(asset_type.id, version_type.id)
                    .with_for_update(read=True, of=[asset_type, version_type])
                )
            )
            .tuples()
            .all()
        )
        binding_type, asset_column, binding_version_column = _BINDING_TYPES[kind]
        system_version_join = version_type.id == getattr(binding_type, binding_version_column) if binding_version_column is not None else version_type.id == asset_type.current_version_id
        system_rows = tuple(
            (
                await session.execute(
                    select(asset_type, version_type)
                    .join(
                        binding_type,
                        getattr(binding_type, asset_column) == asset_type.id,
                    )
                    .join(
                        version_type,
                        system_version_join,
                    )
                    .where(
                        binding_type.project_id == context.project_id,
                        binding_type.enabled.is_(True),
                        asset_type.scope == AssetScope.SYSTEM.value,
                        asset_type.project_id.is_(None),
                        asset_type.status == "active",
                        getattr(version_type, parent_column) == asset_type.id,
                        *((version_type.workflow_status == WorkflowStatus.PUBLISHED.value,) if kind is AssetKind.MCP else ()),
                        *((SkillVersionRow.revoked_at.is_(None),) if kind is AssetKind.SKILL else ()),
                    )
                    .order_by(asset_type.id, version_type.id)
                    .with_for_update(
                        read=True,
                        of=[binding_type, asset_type, version_type],
                    )
                )
            )
            .tuples()
            .all()
        )
        records = [
            *(_ResolvedRecord(AssetScope.PROJECT, asset, version) for asset, version in project_rows),
            *(_ResolvedRecord(AssetScope.SYSTEM, asset, version) for asset, version in system_rows),
        ]
        records.sort(
            key=lambda record: (
                uuid.UUID(str(record.asset.id)).int,
                record.version_id.int,
            )
        )
        return tuple(records)

    @staticmethod
    def _append_delegate_only_dependencies(
        dependency_records: tuple[tuple[_ResolvedRecord, ...], ...],
        records: list[_ResolvedRecord],
        request_id: str,
    ) -> None:
        current_by_asset_id = {record.asset.id for record in records}
        seen_version_ids = {record.version_id for record in records}
        for agent_records in dependency_records:
            for record in agent_records:
                if record.version_id in seen_version_ids:
                    continue
                if record.asset.id not in current_by_asset_id:
                    raise AssetResolutionUnavailable(request_id)
                records.append(record)
                seen_version_ids.add(record.version_id)

    async def materialize_mcp_secrets(
        self,
        context: ProjectContext,
        resolved: ResolvedMcpSnapshot,
    ) -> MaterializedMcpSecrets:
        request_id = getattr(context, "request_id", "unknown")
        self._validate_materialize_input(context, resolved, request_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await self._materialize(
                        session,
                        context,
                        resolved,
                        request_id,
                        lock_project=True,
                        expected_secrets=None,
                    )
        except (AssetValidationFailed, AssetResolutionUnavailable):
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(request_id) from None

    async def materialize_mcp_secrets_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        resolved: ResolvedMcpSnapshot,
        *,
        expected_secrets: tuple[
            tuple[uuid.UUID, uuid.UUID, str],
            ...,
        ]
        | None = None,
    ) -> MaterializedMcpSecrets:
        """Decrypt an exact MCP closure inside the caller's locked transaction."""

        request_id = getattr(context, "request_id", "unknown")
        self._validate_materialize_input(context, resolved, request_id)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise AssetValidationFailed(request_id)
        try:
            return await self._materialize(
                session,
                context,
                resolved,
                request_id,
                lock_project=False,
                expected_secrets=expected_secrets,
            )
        except (AssetValidationFailed, AssetResolutionUnavailable):
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(request_id) from None

    @staticmethod
    def _validate_materialize_input(
        context: ProjectContext,
        resolved: ResolvedMcpSnapshot,
        request_id: str,
    ) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(request_id)
        if Capability.SHARED_ASSETS_EXECUTE not in context.capabilities:
            raise AssetForbidden(request_id)
        if (
            type(resolved) is not ResolvedMcpSnapshot
            or resolved.kind is not AssetKind.MCP
            or not isinstance(resolved.scope, AssetScope)
            or not isinstance(resolved.asset_id, uuid.UUID)
            or not isinstance(resolved.version_id, uuid.UUID)
            or not isinstance(resolved.checksum, str)
            or type(resolved.catalog_generation) is not int
            or resolved.catalog_generation < 0
            or not isinstance(resolved.definition, Mapping)
            or any(not isinstance(generation_id, uuid.UUID) for generation_id in resolved.secret_generation_ids)
            or len(set(resolved.secret_generation_ids)) != len(resolved.secret_generation_ids)
            or not isinstance(resolved.secret_digest, str)
            or len(resolved.secret_digest) != 64
        ):
            raise AssetValidationFailed(request_id)

    @staticmethod
    def _validate_resolve_input(
        context: ProjectContext,
        selection: AssetSelection,
    ) -> None:
        request_id = getattr(context, "request_id", "unknown")
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(request_id)
        if Capability.SHARED_ASSETS_READ not in context.capabilities:
            raise AssetForbidden(request_id)
        if not isinstance(selection, AssetSelection) or not isinstance(selection.kind, AssetKind) or not isinstance(selection.asset_id, uuid.UUID) or (selection.version_id is not None and not isinstance(selection.version_id, uuid.UUID)):
            raise AssetValidationFailed(request_id)

    async def _resolve_record(
        self,
        session: AsyncSession,
        repository: BindingRepository,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> _ResolvedRecord:
        if selection.kind is AssetKind.AGENT:
            project_agent = (
                await session.execute(
                    select(AgentRow)
                    .where(
                        AgentRow.id == selection.asset_id,
                        AgentRow.scope == AssetScope.PROJECT.value,
                        AgentRow.project_id == context.project_id,
                    )
                    .with_for_update(read=True, of=AgentRow)
                )
            ).scalar_one_or_none()
            if project_agent is not None:
                if project_agent.status == "archived":
                    raise AgentArchived(context.request_id)
                if selection.version_id is not None and selection.version_id != project_agent.definition_id:
                    raise AssetResolutionUnavailable(context.request_id)
                self._assert_asset_state(project_agent, project_agent, context.request_id)
                return _ResolvedRecord(AssetScope.PROJECT, project_agent, project_agent)

            # Canonical packaged Main is the project entry Agent and therefore
            # is available without a per-project System Agent binding.
            main_agent = (
                await session.execute(
                    select(AgentRow)
                    .where(
                        AgentRow.id == selection.asset_id,
                        AgentRow.scope == AssetScope.SYSTEM.value,
                        AgentRow.project_id.is_(None),
                        AgentRow.source_key == BUILTIN_MAIN_AGENT_SOURCE_KEY,
                        AgentRow.status == "active",
                    )
                    .with_for_update(read=True, of=AgentRow)
                )
            ).scalar_one_or_none()
            if main_agent is not None:
                if selection.version_id is not None and selection.version_id != main_agent.definition_id:
                    raise AssetResolutionUnavailable(context.request_id)
                return _ResolvedRecord(AssetScope.SYSTEM, main_agent, main_agent)

            binding = await repository.get_binding(
                context,
                AssetKind.AGENT,
                selection.asset_id,
                for_update=True,
                read=True,
                required=False,
            )
            if binding is None or not binding.enabled:
                raise AssetResolutionUnavailable(context.request_id)
            try:
                target = await repository.lock_target(
                    context,
                    selection,
                    allow_archived=True,
                    read=True,
                )
            except SharedAssetError:
                raise AssetResolutionUnavailable(context.request_id) from None
            self._assert_asset_state(target.asset, target.version, context.request_id)
            record = _ResolvedRecord(AssetScope.SYSTEM, target.asset, target.version)
            self._assert_public_agent_record(record, context.request_id)
            return record

        asset_type, version_type, parent_column = _ASSET_TYPES[selection.kind]
        project_statement = (
            select(asset_type)
            .where(
                asset_type.id == selection.asset_id,
                asset_type.scope == "project",
                asset_type.project_id == context.project_id,
            )
            .with_for_update(read=True, of=asset_type)
        )
        asset = (await session.execute(project_statement)).scalar_one_or_none()
        if asset is not None:
            if selection.kind is AssetKind.MCP:
                version_id = selection.version_id or asset.current_published_version_id
            else:
                version_id = asset.current_version_id
            non_mcp_version_mismatch = selection.kind is not AssetKind.MCP and selection.version_id is not None and selection.version_id != version_id
            if version_id is None or non_mcp_version_mismatch:
                raise AssetResolutionUnavailable(context.request_id)
            version = await self._lock_version(
                session,
                version_type,
                parent_column,
                asset.id,
                version_id,
                context.request_id,
            )
            self._assert_asset_state(asset, version, context.request_id)
            return _ResolvedRecord(AssetScope.PROJECT, asset, version)

        binding = await repository.get_binding(
            context,
            selection.kind,
            selection.asset_id,
            for_update=True,
            read=True,
            required=False,
        )
        if binding is None or not binding.enabled:
            raise AssetResolutionUnavailable(context.request_id)
        _binding_type, _asset_column, version_column = _BINDING_TYPES[selection.kind]
        pinned_version_id = getattr(binding, version_column) if version_column is not None else None
        if selection.version_id is not None and selection.version_id != pinned_version_id:
            if selection.kind is AssetKind.MCP:
                raise AssetResolutionUnavailable(context.request_id)
        try:
            target = await repository.lock_target(
                context,
                AssetSelection(selection.kind, selection.asset_id, pinned_version_id),
                allow_archived=True,
                read=True,
            )
        except SharedAssetError:
            raise AssetResolutionUnavailable(context.request_id) from None
        self._assert_asset_state(target.asset, target.version, context.request_id)
        record = _ResolvedRecord(AssetScope.SYSTEM, target.asset, target.version)
        self._assert_public_agent_record(record, context.request_id)
        return record

    @staticmethod
    def _assert_public_agent_record(
        record: _ResolvedRecord,
        request_id: str,
    ) -> None:
        if record.scope is AssetScope.SYSTEM and isinstance(record.asset, AgentRow) and record.asset.source_key == BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY:
            raise AssetResolutionUnavailable(request_id)

    @staticmethod
    async def _lock_version(
        session: AsyncSession,
        version_type,
        parent_column: str,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        request_id: str,
    ):
        statement = (
            select(version_type)
            .where(
                version_type.id == version_id,
                getattr(version_type, parent_column) == asset_id,
            )
            .with_for_update(read=True, of=version_type)
        )
        version = (await session.execute(statement)).scalar_one_or_none()
        if version is None:
            raise AssetResolutionUnavailable(request_id)
        return version

    @staticmethod
    def _assert_asset_state(asset, version, request_id: str) -> None:
        if isinstance(asset, AgentRow):
            if version is not asset or asset.status != "active":
                raise AssetResolutionUnavailable(request_id)
            return
        unavailable = asset.status != "active" if isinstance(asset, SkillRow) else asset.status == "suspended"
        workflow_unavailable = isinstance(version, McpServerVersionRow) and version.workflow_status != "published"
        current_mismatch = not isinstance(version, McpServerVersionRow) and asset.current_version_id != version.id
        invalid_system_v1 = isinstance(asset, SkillRow) and asset.scope == AssetScope.SYSTEM.value and version.version_number != 1
        revoked_skill = isinstance(version, SkillVersionRow) and version.revoked_at is not None
        if unavailable or workflow_unavailable or current_mismatch or invalid_system_v1 or revoked_skill:
            raise AssetResolutionUnavailable(request_id)

    async def _snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        kind: AssetKind,
        record: _ResolvedRecord,
        generation: int,
    ) -> ResolvedAssetSnapshot:
        if kind is AssetKind.AGENT:
            return await self._agent_snapshot(session, context, record, generation)
        if kind is AssetKind.SKILL:
            return await self._skill_snapshot(session, context, record, generation)
        return await self._mcp_snapshot(session, context, record, generation)

    async def _agent_snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        record: _ResolvedRecord,
        generation: int,
        *,
        exact_dependency_records: tuple[
            tuple[_ResolvedRecord, ...],
            tuple[_ResolvedRecord, ...],
        ]
        | None = None,
    ) -> ResolvedAgentSnapshot:
        version = record.version
        if not isinstance(version, AgentRow):
            raise AssetResolutionUnavailable(context.request_id)
        skill_ref_rows = tuple(
            (
                await session.execute(
                    select(
                        AgentSkillRefRow.skill_asset_scope,
                        AgentSkillRefRow.skill_asset_id,
                    )
                    .where(AgentSkillRefRow.agent_id == version.id)
                    .order_by(AgentSkillRefRow.sort_order)
                    .with_for_update(read=True, of=AgentSkillRefRow)
                )
            ).all()
        )
        skill_refs = tuple(SkillAssetRef(AssetScope(scope), asset_id) for scope, asset_id in skill_ref_rows)
        if record.scope is AssetScope.SYSTEM and any(ref.scope is not AssetScope.SYSTEM for ref in skill_refs):
            raise AssetResolutionUnavailable(context.request_id)
        mcp_ids = tuple((await session.execute(select(AgentMcpRefRow.mcp_server_version_id).where(AgentMcpRefRow.agent_id == version.id).order_by(AgentMcpRefRow.sort_order).with_for_update(read=True, of=AgentMcpRefRow))).scalars().all())
        if exact_dependency_records is None:
            skill_records = await self._resolve_skill_asset_refs(
                session,
                context,
                skill_refs,
            )
            mcp_records = await self._assert_exact_dependencies(
                session,
                context,
                AssetKind.MCP,
                mcp_ids,
            )
        else:
            skill_records, mcp_records = exact_dependency_records
            if tuple(SkillAssetRef(item.scope, item.asset.id) for item in skill_records) != skill_refs or tuple(item.version.id for item in mcp_records) != mcp_ids:
                raise AssetResolutionUnavailable(context.request_id)
        skill_ids = tuple(item.version.id for item in skill_records)
        if len(skill_records) != len(skill_refs) or len(mcp_records) != len(mcp_ids):
            raise AssetResolutionUnavailable(context.request_id)
        await self._lock_mcp_secret_closures(
            session,
            context,
            mcp_records,
            context.request_id,
        )
        dependencies = tuple((*skill_ids, *mcp_ids))
        try:
            model_settings = AgentModelSettings.model_validate({} if version.model_settings is None else version.model_settings)
        except ValidationError:
            raise AssetResolutionUnavailable(context.request_id) from None
        if version.payload_schema_version != 4 or not isinstance(version.tool_groups, list) or (version.model_ref != DEFAULT_MODEL_REF and exact_model_ref(version.model_ref) is None):
            raise AssetResolutionUnavailable(context.request_id)
        payload = AgentPayload(
            description=version.description,
            payload_schema_version=version.payload_schema_version,
            agents_instructions=version.agents_instructions,
            soul=version.soul,
            identity=version.identity,
            user_context=version.user_context,
            model_ref=version.model_ref,
            model_settings=model_settings,
            tool_groups=tuple(version.tool_groups),
            skill_refs=skill_refs,
            mcp_version_ids=mcp_ids,
        )
        if not persisted_agent_payload_checksum_matches(
            payload,
            version.payload_checksum,
        ):
            raise AssetResolutionUnavailable(context.request_id)
        runtime_payload = payload
        runtime_checksum = agent_payload_checksum(runtime_payload)
        return ResolvedAgentSnapshot(
            kind=AssetKind.AGENT,
            scope=record.scope,
            asset_id=record.asset.id,
            # Run Snapshot keeps the generic historical wire field name. For
            # Agent entries it carries the Definition identity.
            version_id=version.definition_id,
            checksum=runtime_checksum,
            catalog_generation=generation,
            dependency_version_ids=dependencies,
            payload=runtime_payload,
            skill_version_ids=skill_ids,
            slug=record.asset.slug,
            source_key=record.asset.source_key,
        )

    async def _agent_snapshot_with_dependencies(
        self,
        session: AsyncSession,
        context: ProjectContext,
        record: _ResolvedRecord,
        generation: int,
    ) -> tuple[
        ResolvedAgentSnapshot,
        tuple[_ResolvedRecord, ...],
        tuple[_ResolvedRecord, ...],
    ]:
        version = record.version
        if not isinstance(version, AgentRow):
            raise AssetResolutionUnavailable(context.request_id)
        skill_ref_rows = tuple(
            (
                await session.execute(
                    select(
                        AgentSkillRefRow.skill_asset_scope,
                        AgentSkillRefRow.skill_asset_id,
                    )
                    .where(AgentSkillRefRow.agent_id == version.id)
                    .order_by(AgentSkillRefRow.sort_order)
                    .with_for_update(read=True, of=AgentSkillRefRow)
                )
            ).all()
        )
        skill_refs = tuple(SkillAssetRef(AssetScope(scope), asset_id) for scope, asset_id in skill_ref_rows)
        mcp_ids = tuple((await session.execute(select(AgentMcpRefRow.mcp_server_version_id).where(AgentMcpRefRow.agent_id == version.id).order_by(AgentMcpRefRow.sort_order).with_for_update(read=True, of=AgentMcpRefRow))).scalars().all())
        skill_records = await self._resolve_skill_asset_refs(
            session,
            context,
            skill_refs,
        )
        mcp_records = await self._assert_exact_dependencies(
            session,
            context,
            AssetKind.MCP,
            mcp_ids,
        )
        snapshot = await self._agent_snapshot(
            session,
            context,
            record,
            generation,
            exact_dependency_records=(skill_records, mcp_records),
        )
        return snapshot, skill_records, mcp_records

    async def _skill_snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        record: _ResolvedRecord,
        generation: int,
    ) -> ResolvedSkillSnapshot:
        version = record.version
        if not isinstance(version, SkillVersionRow):
            raise AssetResolutionUnavailable(context.request_id)
        if record.scope is AssetScope.SYSTEM and version.revoked_at is not None:
            raise AssetResolutionUnavailable(context.request_id)
        rows = tuple((await session.execute(select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == version.id).order_by(SkillVersionFileRow.path).with_for_update(read=True, of=SkillVersionFileRow))).scalars().all())
        skill_record = SkillVersionRecord(version, rows)
        try:
            files = await asyncio.to_thread(
                SkillService._verified_archive_files,
                skill_record,
                context.request_id,
            )
        except AssetValidationFailed:
            raise AssetResolutionUnavailable(context.request_id) from None
        try:
            requirements = [
                SkillSecretRequirementSnapshot(
                    name=item.name,
                    target_env=item.target_env,
                    optional=item.optional,
                )
                for item in parse_skill_secret_declarations(
                    version.secret_requirements,
                    request_id=context.request_id,
                )
            ]
        except SharedAssetError:
            raise AssetResolutionUnavailable(context.request_id) from None
        return ResolvedSkillSnapshot(
            kind=AssetKind.SKILL,
            scope=record.scope,
            asset_id=record.asset.id,
            version_id=version.id,
            checksum=version.payload_checksum,
            catalog_generation=generation,
            dependency_version_ids=(),
            files=files,
            secret_requirements=tuple(requirements),
        )

    async def _skill_version_snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        record: _ResolvedRecord,
        generation: int,
    ) -> ResolvedSkillVersionSnapshot:
        """Resolve sealed Skill facts without selecting immutable file bytes."""

        del session
        version = record.version
        if (
            not isinstance(version, SkillVersionRow)
            or (record.scope is AssetScope.SYSTEM and version.revoked_at is not None)
            or version.files_sealed is not True
            or type(version.file_count) is not int
            or not 1 <= version.file_count <= 16_384
            or type(version.content_size_bytes) is not int
            or not 0 <= version.content_size_bytes <= 100 * 1024 * 1024
        ):
            raise AssetResolutionUnavailable(context.request_id)
        try:
            requirements = tuple(
                SkillSecretRequirementSnapshot(
                    name=item.name,
                    target_env=item.target_env,
                    optional=item.optional,
                )
                for item in parse_skill_secret_declarations(
                    version.secret_requirements,
                    request_id=context.request_id,
                )
            )
        except SharedAssetError:
            raise AssetResolutionUnavailable(context.request_id) from None
        return ResolvedSkillVersionSnapshot(
            kind=AssetKind.SKILL,
            scope=record.scope,
            asset_id=record.asset.id,
            version_id=version.id,
            checksum=version.payload_checksum,
            catalog_generation=generation,
            dependency_version_ids=(),
            file_count=version.file_count,
            content_size_bytes=version.content_size_bytes,
            secret_requirements=requirements,
        )

    async def _mcp_snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        record: _ResolvedRecord,
        generation: int,
    ) -> ResolvedMcpSnapshot:
        if not isinstance(record.version, McpServerVersionRow):
            raise AssetResolutionUnavailable(context.request_id)
        slots = tuple(
            (await session.execute(select(McpSecretSlotRow).where(McpSecretSlotRow.mcp_server_version_id == record.version.id).order_by(McpSecretSlotRow.name, McpSecretSlotRow.id).with_for_update(read=True, of=McpSecretSlotRow)))
            .scalars()
            .all()
        )
        mcp_record = McpVersionRecord(record.version, slots)
        closure = await lock_mcp_secret_closure(
            session,
            project_id=context.project_id,
            mcp_server_id=record.asset.id,
            mcp_server_version_id=record.version.id,
            slots=slots,
            request_id=context.request_id,
        )
        safe_definition = self._safe_mcp_definition(
            mcp_record,
            context.request_id,
        )
        return ResolvedMcpSnapshot(
            kind=AssetKind.MCP,
            scope=record.scope,
            asset_id=record.asset.id,
            version_id=record.version.id,
            checksum=record.version.payload_checksum,
            catalog_generation=generation,
            dependency_version_ids=(),
            definition=safe_definition,
            secret_generation_ids=tuple(material.generation_id for material in closure.materials),
            secret_digest=closure.digest,
        )

    @staticmethod
    def _safe_mcp_definition(
        record: McpVersionRecord,
        request_id: str,
    ) -> Mapping[str, object]:
        definition = McpService._definition_from_record(record)
        safe_definition = _freeze(
            {
                "description": definition.description,
                "transport": definition.transport,
                "command": definition.command,
                "args": definition.args,
                "url": definition.url,
                "env": definition.env,
                "headers": definition.headers,
                "oauth": definition.oauth,
                "routing": definition.routing,
                "tool_overrides": definition.tool_overrides,
                "timeout_seconds": definition.timeout_seconds,
                "secret_slots": tuple(
                    {
                        "name": slot.name,
                        "purpose": slot.purpose,
                        "payload_schema": slot.payload_schema,
                        "required": slot.required,
                    }
                    for slot in definition.secret_slots
                ),
            }
        )
        if not isinstance(safe_definition, Mapping):
            raise AssetResolutionUnavailable(request_id)
        return safe_definition

    async def _assert_exact_dependencies(
        self,
        session: AsyncSession,
        context: ProjectContext,
        kind: AssetKind,
        version_ids: Sequence[uuid.UUID],
    ) -> tuple[_ResolvedRecord, ...]:
        records_by_version_id: dict[uuid.UUID, _ResolvedRecord] = {}
        for version_id in sorted(
            {uuid.UUID(str(value)) for value in version_ids},
            key=lambda value: value.int,
        ):
            records_by_version_id[version_id] = await self._dependency_record(
                session,
                context,
                kind,
                version_id,
            )
        return tuple(records_by_version_id[uuid.UUID(str(version_id))] for version_id in version_ids)

    async def _resolve_skill_asset_refs(
        self,
        session: AsyncSession,
        context: ProjectContext,
        refs: Sequence[SkillAssetRef],
    ) -> tuple[_ResolvedRecord, ...]:
        records: list[_ResolvedRecord] = []
        seen: set[SkillAssetRef] = set()
        repository = BindingRepository(session)
        for ref in refs:
            if ref in seen:
                raise AssetResolutionUnavailable(context.request_id)
            seen.add(ref)
            if ref.scope is AssetScope.PROJECT:
                asset = (
                    await session.execute(
                        select(SkillRow)
                        .where(
                            SkillRow.id == ref.asset_id,
                            SkillRow.scope == AssetScope.PROJECT.value,
                            SkillRow.project_id == context.project_id,
                            SkillRow.status == "active",
                        )
                        .with_for_update(read=True, of=SkillRow)
                    )
                ).scalar_one_or_none()
                if asset is None or asset.current_version_id is None:
                    raise AssetResolutionUnavailable(context.request_id)
                version = await self._lock_version(
                    session,
                    SkillVersionRow,
                    "skill_id",
                    asset.id,
                    asset.current_version_id,
                    context.request_id,
                )
                self._assert_asset_state(asset, version, context.request_id)
                records.append(_ResolvedRecord(AssetScope.PROJECT, asset, version))
                continue
            binding = await repository.get_binding(
                context,
                AssetKind.SKILL,
                ref.asset_id,
                for_update=True,
                read=True,
                required=False,
            )
            if binding is None or not binding.enabled:
                raise AssetResolutionUnavailable(context.request_id)
            try:
                target = await repository.lock_target(
                    context,
                    AssetSelection(AssetKind.SKILL, ref.asset_id),
                    read=True,
                )
            except SharedAssetError:
                raise AssetResolutionUnavailable(context.request_id) from None
            self._assert_asset_state(target.asset, target.version, context.request_id)
            records.append(_ResolvedRecord(AssetScope.SYSTEM, target.asset, target.version))
        return tuple(records)

    async def _dependency_record(
        self,
        session: AsyncSession,
        context: ProjectContext,
        kind: AssetKind,
        version_id: uuid.UUID,
    ) -> _ResolvedRecord:
        asset_type, version_type, parent_column = _ASSET_TYPES[kind]
        parent_id = (await session.execute(select(getattr(version_type, parent_column)).where(version_type.id == version_id))).scalar_one_or_none()
        status_predicate = asset_type.status == "active" if kind is AssetKind.SKILL else asset_type.status != "suspended"
        project_statement = (
            select(asset_type)
            .where(
                asset_type.id == parent_id,
                asset_type.scope == "project",
                asset_type.project_id == context.project_id,
                status_predicate,
            )
            .with_for_update(read=True, of=asset_type)
        )
        project_asset = (await session.execute(project_statement)).scalar_one_or_none()
        if project_asset is not None:
            project_version = await self._lock_version(
                session,
                version_type,
                parent_column,
                uuid.UUID(str(project_asset.id)),
                version_id,
                context.request_id,
            )
            self._assert_asset_state(
                project_asset,
                project_version,
                context.request_id,
            )
            return _ResolvedRecord(
                AssetScope.PROJECT,
                project_asset,
                project_version,
            )
        binding_type, asset_column, binding_version_column = _BINDING_TYPES[kind]
        binding_statement = (
            select(binding_type)
            .where(
                binding_type.project_id == context.project_id,
                getattr(binding_type, binding_version_column) == version_id,
                binding_type.enabled.is_(True),
            )
            .with_for_update(read=True, of=binding_type)
        )
        binding = (await session.execute(binding_statement)).scalar_one_or_none()
        if binding is None:
            raise AssetResolutionUnavailable(context.request_id)
        asset_id = uuid.UUID(str(getattr(binding, asset_column)))
        asset_statement = (
            select(asset_type)
            .where(
                asset_type.id == asset_id,
                asset_type.scope == "system",
                asset_type.project_id.is_(None),
                status_predicate,
            )
            .with_for_update(read=True, of=asset_type)
        )
        system_asset = (await session.execute(asset_statement)).scalar_one_or_none()
        if system_asset is None:
            raise AssetResolutionUnavailable(context.request_id)
        system_version = await self._lock_version(
            session,
            version_type,
            parent_column,
            asset_id,
            version_id,
            context.request_id,
        )
        self._assert_asset_state(
            system_asset,
            system_version,
            context.request_id,
        )
        return _ResolvedRecord(AssetScope.SYSTEM, system_asset, system_version)

    async def _mcp_record_for_version(
        self,
        session: AsyncSession,
        context: ProjectContext,
        version_id: uuid.UUID,
    ) -> _ResolvedRecord:
        return await self._dependency_record(
            session,
            context,
            AssetKind.MCP,
            version_id,
        )

    async def _lock_mcp_secret_closures(
        self,
        session: AsyncSession,
        context: ProjectContext,
        records: Sequence[_ResolvedRecord],
        request_id: str,
    ) -> dict[uuid.UUID, McpSecretClosure]:
        closures: dict[uuid.UUID, McpSecretClosure] = {}
        for record in records:
            if not isinstance(record.version, McpServerVersionRow):
                raise AssetResolutionUnavailable(request_id)
            slots = tuple(
                (
                    await session.execute(
                        select(McpSecretSlotRow)
                        .where(
                            McpSecretSlotRow.mcp_server_version_id == record.version.id,
                        )
                        .order_by(McpSecretSlotRow.name, McpSecretSlotRow.id)
                        .with_for_update(read=True, of=McpSecretSlotRow)
                    )
                )
                .scalars()
                .all()
            )
            try:
                closure = await lock_mcp_secret_closure(
                    session,
                    project_id=context.project_id,
                    mcp_server_id=record.asset.id,
                    mcp_server_version_id=record.version.id,
                    slots=slots,
                    request_id=request_id,
                )
            except AssetValidationFailed:
                raise AssetResolutionUnavailable(request_id) from None
            closures[uuid.UUID(str(record.version.id))] = closure
        return closures

    async def _materialize(
        self,
        session: AsyncSession,
        context: ProjectContext,
        resolved: ResolvedMcpSnapshot,
        request_id: str,
        *,
        lock_project: bool,
        expected_secrets: tuple[
            tuple[uuid.UUID, uuid.UUID, str],
            ...,
        ]
        | None,
    ) -> MaterializedMcpSecrets:
        repository = BindingRepository(session)
        if lock_project:
            await repository.lock_project(context, read=True)
        scope = resolved.scope
        if scope is AssetScope.SYSTEM:
            binding = await repository.get_binding(
                context,
                AssetKind.MCP,
                resolved.asset_id,
                for_update=True,
                read=True,
                required=False,
            )
            if binding is None or not binding.enabled or binding.mcp_server_version_id != resolved.version_id:
                raise AssetResolutionUnavailable(request_id)
        asset_filters = [
            McpServerRow.id == resolved.asset_id,
            McpServerRow.scope == scope.value,
        ]
        if scope is AssetScope.PROJECT:
            asset_filters.append(McpServerRow.project_id == context.project_id)
        else:
            asset_filters.append(McpServerRow.project_id.is_(None))
        asset_statement = select(McpServerRow).where(*asset_filters).with_for_update(read=True, of=McpServerRow)
        asset = (await session.execute(asset_statement)).scalar_one_or_none()
        version_statement = (
            select(McpServerVersionRow)
            .where(
                McpServerVersionRow.id == resolved.version_id,
                McpServerVersionRow.mcp_server_id == resolved.asset_id,
            )
            .with_for_update(read=True, of=McpServerVersionRow)
        )
        version = (await session.execute(version_statement)).scalar_one_or_none()
        if asset is None or version is None or asset.status == "suspended" or version.workflow_status != "published" or version.payload_checksum != resolved.checksum:
            raise AssetResolutionUnavailable(request_id)

        record = _ResolvedRecord(scope, asset, version)
        closure = (
            await self._lock_mcp_secret_closures(
                session,
                context,
                (record,),
                request_id,
            )
        )[uuid.UUID(str(version.id))]
        current_refs = tuple(
            sorted(
                (
                    (
                        material.slot_id,
                        material.generation_id,
                        material.generation_digest,
                    )
                    for material in closure.materials
                ),
                key=lambda item: (item[0].int, item[1].int, item[2]),
            )
        )
        if expected_secrets is not None:
            try:
                normalized_values = tuple(
                    (
                        uuid.UUID(str(item[0])),
                        uuid.UUID(str(item[1])),
                        str(item[2]),
                    )
                    for item in expected_secrets
                    if isinstance(item, tuple) and len(item) == 3
                )
            except (AttributeError, TypeError, ValueError):
                raise AssetValidationFailed(request_id)
            if len(normalized_values) != len(expected_secrets):
                raise AssetValidationFailed(request_id)
            normalized_expected = tuple(
                sorted(
                    normalized_values,
                    key=lambda item: (item[0].int, item[1].int, item[2]),
                )
            )
            if current_refs != normalized_expected:
                raise AssetResolutionUnavailable(request_id)
        locked_definition = self._safe_mcp_definition(
            McpVersionRecord(
                version,
                tuple((await session.execute(select(McpSecretSlotRow).where(McpSecretSlotRow.mcp_server_version_id == version.id).order_by(McpSecretSlotRow.name, McpSecretSlotRow.id))).scalars().all()),
            ),
            request_id,
        )
        if locked_definition != resolved.definition or tuple(material.generation_id for material in closure.materials) != resolved.secret_generation_ids:
            raise AssetResolutionUnavailable(request_id)
        by_slot: dict[str, Mapping[str, object]] = {}
        store = McpSecretStore(session, secret_key=self._secret_key)
        for material in closure.materials:
            payload = store.materialize(material, request_id=request_id)
            frozen_payload = _freeze(payload)
            if not isinstance(frozen_payload, Mapping):
                raise AssetResolutionUnavailable(request_id)
            by_slot[material.slot_name] = frozen_payload
        return MaterializedMcpSecrets(
            mcp_version_id=version.id,
            by_slot=MappingProxyType(by_slot),
        )


async def resolve_project_asset_snapshot(
    context: ProjectContext,
    selection: AssetSelection,
    *,
    session_factory: Callable[[], AsyncSession],
) -> ResolvedAssetSnapshot:
    """Functional adapter for call sites that do not keep a resolver instance."""

    return await ProjectAssetResolver(session_factory).resolve_project_asset_snapshot(
        context,
        selection,
    )


async def materialize_mcp_secrets(
    context: ProjectContext,
    resolved: ResolvedMcpSnapshot,
    *,
    session_factory: Callable[[], AsyncSession],
    secret_key: SecretKey | None = None,
) -> MaterializedMcpSecrets:
    """Functional internal adapter; plaintext exists only in the returned object."""

    return await ProjectAssetResolver(
        session_factory,
        secret_key=secret_key,
    ).materialize_mcp_secrets(context, resolved)
