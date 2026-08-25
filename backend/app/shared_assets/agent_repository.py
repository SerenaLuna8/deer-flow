from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound
from app.shared_assets.internal_assets import BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY
from app.shared_assets.models import AgentPayload, AssetScope, SkillAssetRef
from deerflow.persistence.projects.model import ProjectDefaultAgentRow, ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentMcpRefRow,
    AgentRow,
    AgentSkillRefRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionRow,
)


class AgentCreateCommand(Protocol):
    slug: str
    display_name: str


@dataclass(frozen=True)
class AgentDefinitionRecord:
    row: AgentRow
    skill_refs: tuple[SkillAssetRef, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]


def _request_id(context: object) -> str:
    request_id = getattr(context, "request_id", None)
    return request_id if isinstance(request_id, str) else "unknown"


def _is_internal_skill_builder_agent(row: AgentRow) -> bool:
    return row.source_key == BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY


class AgentRepository:
    """Project/System Agent persistence with one mutable definition per asset."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _require_project_actor(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(_request_id(context))

    @staticmethod
    def _require_system_actor(context: SystemAssetGovernanceContext) -> None:
        if not isinstance(context, SystemAssetGovernanceContext):
            raise AssetForbidden(_request_id(context))

    @staticmethod
    def _require_system_catalog_reader(context: SystemAssetGovernanceContext | SystemAssetReadContext) -> None:
        if not isinstance(context, (SystemAssetGovernanceContext, SystemAssetReadContext)) or context.project_id is not None:
            raise AssetForbidden(_request_id(context))

    @staticmethod
    def _project_context_exists(context: ProjectContext):
        return exists(
            select(1)
            .select_from(ProjectMembershipRow)
            .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
            .where(
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
        )

    async def _lock_project_context(self, context: ProjectContext, *, read: bool = True) -> None:
        self._require_project_actor(context)
        statement = (
            select(ProjectRow.id)
            .join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id)
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
            .with_for_update(read=read, of=[ProjectRow, ProjectMembershipRow])
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def _lock_override_project(self, context: SystemAssetGovernanceContext) -> None:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetForbidden(context.request_id)
        statement = select(ProjectRow.id).where(ProjectRow.id == context.project_id, ProjectRow.status == "active", ProjectRow.is_suspended.is_(False)).with_for_update(of=ProjectRow)
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def create_project_asset(
        self,
        context: ProjectContext,
        command: AgentCreateCommand,
        payload: AgentPayload,
        *,
        definition_id: uuid.UUID,
        payload_checksum: str,
    ) -> AgentDefinitionRecord:
        await self._lock_project_context(context)
        row = self._new_row(
            scope="project",
            project_id=context.project_id,
            command=command,
            payload=payload,
            definition_id=definition_id,
            payload_checksum=payload_checksum,
            user_id=str(context.user_id),
            status="suspended",
        )
        self.session.add(row)
        await self.session.flush()
        await self._replace_refs(row.id, payload.skill_refs, payload.mcp_version_ids)
        return AgentDefinitionRecord(row, payload.skill_refs, payload.mcp_version_ids)

    async def create_override_asset(
        self,
        context: SystemAssetGovernanceContext,
        command: AgentCreateCommand,
        payload: AgentPayload,
        *,
        definition_id: uuid.UUID,
        payload_checksum: str,
    ) -> AgentDefinitionRecord:
        await self._lock_override_project(context)
        row = self._new_row(
            scope="project",
            project_id=context.project_id,
            command=command,
            payload=payload,
            definition_id=definition_id,
            payload_checksum=payload_checksum,
            user_id=str(context.user_id),
            status="suspended",
        )
        self.session.add(row)
        await self.session.flush()
        await self._replace_refs(row.id, payload.skill_refs, payload.mcp_version_ids)
        return AgentDefinitionRecord(row, payload.skill_refs, payload.mcp_version_ids)

    @staticmethod
    def _new_row(
        *,
        scope: str,
        project_id: uuid.UUID | None,
        command: AgentCreateCommand,
        payload: AgentPayload,
        definition_id: uuid.UUID,
        payload_checksum: str,
        user_id: str,
        status: str,
    ) -> AgentRow:
        return AgentRow(
            scope=scope,
            project_id=project_id,
            slug=command.slug,
            display_name=command.display_name,
            status=status,
            definition_id=definition_id,
            description=payload.description,
            agents_instructions=payload.agents_instructions,
            soul=payload.soul,
            identity=payload.identity,
            user_context=payload.user_context,
            model_ref=payload.model_ref,
            model_settings=payload.model_settings.model_dump(exclude_none=True),
            tool_groups=list(payload.tool_groups),
            payload_schema_version=4,
            payload_checksum=payload_checksum,
            revision=1,
            created_by_user_id=user_id,
            updated_by_user_id=user_id,
        )

    async def get_project_asset(self, context: ProjectContext, asset_id: uuid.UUID, *, for_update: bool = False) -> AgentRow:
        self._require_project_actor(context)
        if for_update:
            await self._lock_project_context(context)
        statement = select(AgentRow).where(
            AgentRow.id == asset_id,
            AgentRow.scope == "project",
            AgentRow.project_id == context.project_id,
            self._project_context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def get_project_visible_asset(self, context: ProjectContext, asset_id: uuid.UUID) -> AgentRow:
        self._require_project_actor(context)
        statement = select(AgentRow).where(
            AgentRow.id == asset_id,
            or_(
                and_(AgentRow.scope == "project", AgentRow.project_id == context.project_id, AgentRow.status != "archived"),
                and_(
                    AgentRow.scope == "system",
                    AgentRow.project_id.is_(None),
                    or_(AgentRow.source_key.is_(None), AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY),
                ),
            ),
            self._project_context_exists(context),
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def get_system_asset(
        self,
        context: SystemAssetGovernanceContext | SystemAssetReadContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentRow:
        self._require_system_catalog_reader(context)
        if for_update and isinstance(context, SystemAssetReadContext):
            raise AssetForbidden(context.request_id)
        statement = select(AgentRow).where(
            AgentRow.id == asset_id,
            AgentRow.scope == "system",
            AgentRow.project_id.is_(None),
            or_(AgentRow.source_key.is_(None), AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None or _is_internal_skill_builder_agent(row):
            raise AssetNotFound(context.request_id)
        return row

    async def get_override_asset(self, context: SystemAssetGovernanceContext, asset_id: uuid.UUID, *, for_update: bool = False) -> AgentRow:
        await self._lock_override_project(context)
        statement = select(AgentRow).where(AgentRow.id == asset_id, AgentRow.scope == "project", AgentRow.project_id == context.project_id)
        if for_update:
            statement = statement.with_for_update(of=AgentRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def get_definition(self, asset: AgentRow, *, for_update: bool = False) -> AgentDefinitionRecord:
        skill_refs, mcp_ids = await self._load_refs((asset.id,), for_update=for_update)
        return AgentDefinitionRecord(asset, skill_refs.get(asset.id, ()), mcp_ids.get(asset.id, ()))

    async def replace_definition(
        self,
        asset: AgentRow,
        payload: AgentPayload,
        *,
        definition_id: uuid.UUID,
        payload_checksum: str,
        updated_by_user_id: str,
    ) -> AgentDefinitionRecord:
        # Schema V1 permits mutable Project definitions only through this
        # transaction-scoped fence. Set it before touching the mapped row so
        # an autoflush cannot reach the trigger without the mutation identity.
        await self.session.scalar(
            select(
                func.set_config(
                    "deerflow.agent_definition_mutation_id",
                    str(asset.id),
                    True,
                )
            )
        )
        asset.definition_id = definition_id
        asset.description = payload.description
        asset.agents_instructions = payload.agents_instructions
        asset.soul = payload.soul
        asset.identity = payload.identity
        asset.user_context = payload.user_context
        asset.model_ref = payload.model_ref
        asset.model_settings = payload.model_settings.model_dump(exclude_none=True)
        asset.tool_groups = list(payload.tool_groups)
        asset.payload_schema_version = 4
        asset.payload_checksum = payload_checksum
        asset.updated_by_user_id = updated_by_user_id
        asset.revision += 1
        await self._replace_refs(asset.id, payload.skill_refs, payload.mcp_version_ids)
        await self.session.flush()
        return AgentDefinitionRecord(asset, payload.skill_refs, payload.mcp_version_ids)

    async def _replace_refs(
        self,
        agent_id: uuid.UUID,
        skill_refs: Sequence[SkillAssetRef],
        mcp_version_ids: Sequence[uuid.UUID],
    ) -> None:
        await self.session.scalar(select(func.set_config("deerflow.agent_definition_mutation_id", str(agent_id), True)))
        await self.session.execute(delete(AgentSkillRefRow).where(AgentSkillRefRow.agent_id == agent_id))
        await self.session.execute(delete(AgentMcpRefRow).where(AgentMcpRefRow.agent_id == agent_id))
        self.session.add_all(
            AgentSkillRefRow(
                agent_id=agent_id,
                skill_asset_scope=ref.scope.value,
                skill_asset_id=ref.asset_id,
                sort_order=index,
            )
            for index, ref in enumerate(skill_refs)
        )
        self.session.add_all(AgentMcpRefRow(agent_id=agent_id, mcp_server_version_id=version_id, sort_order=index) for index, version_id in enumerate(mcp_version_ids))
        await self.session.flush()

    async def lock_project_agents_referencing_skill(self, context: ProjectContext, skill_id: uuid.UUID) -> tuple[AgentDefinitionRecord, ...]:
        self._require_project_actor(context)
        statement = (
            select(AgentRow)
            .join(AgentSkillRefRow, AgentSkillRefRow.agent_id == AgentRow.id)
            .where(
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
                AgentSkillRefRow.skill_asset_scope == "project",
                AgentSkillRefRow.skill_asset_id == skill_id,
                self._project_context_exists(context),
            )
            .order_by(AgentRow.id)
            .with_for_update(of=[AgentRow, AgentSkillRefRow])
        )
        rows = tuple((await self.session.execute(statement)).scalars().all())
        skill_refs, mcp_ids = await self._load_refs(tuple(row.id for row in rows), for_update=True)
        return tuple(AgentDefinitionRecord(row, skill_refs.get(row.id, ()), mcp_ids.get(row.id, ())) for row in rows)

    async def clear_current_project_default(self, context: ProjectContext, asset_id: uuid.UUID) -> bool:
        await self._lock_project_context(context)
        pointer = (await self.session.execute(select(ProjectDefaultAgentRow).where(ProjectDefaultAgentRow.project_id == context.project_id).with_for_update(of=ProjectDefaultAgentRow))).scalar_one_or_none()
        if pointer is None or pointer.agent_asset_id != asset_id:
            return False
        pointer.agent_asset_id = None
        pointer.revision += 1
        pointer.updated_by_user_id = str(context.user_id)
        await self.session.flush()
        return True

    async def ensure_not_current_project_default(self, context: ProjectContext | SystemAssetGovernanceContext, asset_id: uuid.UUID) -> None:
        if isinstance(context, ProjectContext):
            await self._lock_project_context(context)
            project_id = context.project_id
        elif isinstance(context, SystemAssetGovernanceContext) and context.project_id is not None:
            await self._lock_override_project(context)
            project_id = context.project_id
        elif isinstance(context, SystemAssetGovernanceContext):
            return
        else:
            raise AssetForbidden(_request_id(context))
        value = await self.session.scalar(select(ProjectDefaultAgentRow.project_id).where(ProjectDefaultAgentRow.project_id == project_id, ProjectDefaultAgentRow.agent_asset_id == asset_id).with_for_update(of=ProjectDefaultAgentRow))
        if value is not None:
            raise AssetConflict(context.request_id)

    async def archive_project_asset(self, context: ProjectContext, asset: AgentRow) -> None:
        self._require_project_actor(context)
        if asset.scope != "project" or asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        if asset.status == "archived":
            raise AssetConflict(context.request_id)
        asset.status = "archived"
        asset.revision += 1
        asset.updated_by_user_id = str(context.user_id)
        await self.session.flush()

    async def list_project_visible(self, context: ProjectContext) -> tuple[AgentRow, ...]:
        await self._lock_project_context(context)
        project_rows = (
            (
                await self.session.execute(
                    select(AgentRow).where(
                        AgentRow.scope == "project",
                        AgentRow.project_id == context.project_id,
                        AgentRow.status != "archived",
                        self._project_context_exists(context),
                    )
                )
            )
            .scalars()
            .all()
        )
        system_rows = (
            (
                await self.session.execute(
                    select(AgentRow).where(
                        AgentRow.scope == "system",
                        AgentRow.project_id.is_(None),
                        or_(AgentRow.source_key.is_(None), AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY),
                        self._project_context_exists(context),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(sorted((*project_rows, *(row for row in system_rows if not _is_internal_skill_builder_agent(row))), key=lambda row: (row.created_at, row.id)))

    async def list_system_visible(self, context: SystemAssetGovernanceContext | SystemAssetReadContext) -> tuple[AgentRow, ...]:
        self._require_system_catalog_reader(context)
        rows = tuple(
            (
                await self.session.execute(
                    select(AgentRow)
                    .where(
                        AgentRow.scope == "system",
                        AgentRow.project_id.is_(None),
                        or_(AgentRow.source_key.is_(None), AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY),
                    )
                    .order_by(AgentRow.created_at, AgentRow.id)
                )
            )
            .scalars()
            .all()
        )
        return tuple(row for row in rows if not _is_internal_skill_builder_agent(row))

    async def list_override_visible(self, context: SystemAssetGovernanceContext) -> tuple[AgentRow, ...]:
        await self._lock_override_project(context)
        return tuple((await self.session.execute(select(AgentRow).where(AgentRow.scope == "project", AgentRow.project_id == context.project_id, AgentRow.status != "archived").order_by(AgentRow.created_at, AgentRow.id))).scalars().all())

    async def current_descriptions(self, asset_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, str]:
        ids = tuple(asset_ids)
        if not ids:
            return {}
        return {asset_id: description for asset_id, description in (await self.session.execute(select(AgentRow.id, AgentRow.description).where(AgentRow.id.in_(ids))))}

    async def _load_refs(
        self,
        agent_ids: Sequence[uuid.UUID],
        *,
        for_update: bool = False,
    ) -> tuple[dict[uuid.UUID, tuple[SkillAssetRef, ...]], dict[uuid.UUID, tuple[uuid.UUID, ...]]]:
        if not agent_ids:
            return {}, {}
        skill_statement = (
            select(AgentSkillRefRow.agent_id, AgentSkillRefRow.skill_asset_scope, AgentSkillRefRow.skill_asset_id).where(AgentSkillRefRow.agent_id.in_(agent_ids)).order_by(AgentSkillRefRow.agent_id, AgentSkillRefRow.sort_order)
        )
        mcp_statement = select(AgentMcpRefRow.agent_id, AgentMcpRefRow.mcp_server_version_id).where(AgentMcpRefRow.agent_id.in_(agent_ids)).order_by(AgentMcpRefRow.agent_id, AgentMcpRefRow.sort_order)
        if for_update:
            skill_statement = skill_statement.with_for_update(of=AgentSkillRefRow)
            mcp_statement = mcp_statement.with_for_update(of=AgentMcpRefRow)
        skill_map: dict[uuid.UUID, list[SkillAssetRef]] = {}
        for agent_id, scope, dependency_id in (await self.session.execute(skill_statement)).all():
            skill_map.setdefault(agent_id, []).append(SkillAssetRef(AssetScope(scope), dependency_id))
        mcp_map: dict[uuid.UUID, list[uuid.UUID]] = {}
        for agent_id, dependency_id in (await self.session.execute(mcp_statement)).all():
            mcp_map.setdefault(agent_id, []).append(dependency_id)
        return ({key: tuple(value) for key, value in skill_map.items()}, {key: tuple(value) for key, value in mcp_map.items()})

    async def resolve_project_skill_refs(self, context: ProjectContext, refs: Sequence[SkillAssetRef], *, require_runnable: bool) -> tuple[SkillAssetRef, ...]:
        self._require_project_actor(context)
        if not refs:
            return ()
        project_ids = tuple(ref.asset_id for ref in refs if ref.scope is AssetScope.PROJECT)
        system_ids = tuple(ref.asset_id for ref in refs if ref.scope is AssetScope.SYSTEM)
        project_statement = (
            select(SkillRow.id)
            .where(
                SkillRow.id.in_(project_ids),
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                self._project_context_exists(context),
                *((SkillRow.status == "active", SkillRow.current_version_id.is_not(None)) if require_runnable else (SkillRow.status != "archived",)),
            )
            .with_for_update(read=True, of=SkillRow)
        )
        system_statement = (
            select(SkillRow.id)
            .join(ProjectSystemSkillBindingRow, ProjectSystemSkillBindingRow.system_skill_id == SkillRow.id)
            .join(SkillVersionRow, and_(SkillVersionRow.skill_id == SkillRow.id, SkillVersionRow.id == SkillRow.current_version_id))
            .where(
                SkillRow.id.in_(system_ids),
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                ProjectSystemSkillBindingRow.project_id == context.project_id,
                ProjectSystemSkillBindingRow.enabled.is_(True),
                self._project_context_exists(context),
                *((SkillRow.status == "active", SkillVersionRow.revoked_at.is_(None)) if require_runnable else ()),
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow, ProjectSystemSkillBindingRow])
        )
        project_found = set((await self.session.execute(project_statement)).scalars().all())
        system_found = set((await self.session.execute(system_statement)).scalars().all())
        return tuple(ref for ref in refs if (ref.scope is AssetScope.PROJECT and ref.asset_id in project_found) or (ref.scope is AssetScope.SYSTEM and ref.asset_id in system_found))

    async def list_enabled_system_dependencies(self, context: ProjectContext) -> tuple[tuple[SkillAssetRef, ...], tuple[uuid.UUID, ...]]:
        await self._lock_project_context(context)
        skill_ids = (
            (
                await self.session.execute(
                    select(SkillRow.id)
                    .select_from(ProjectSystemSkillBindingRow)
                    .join(SkillRow, and_(SkillRow.id == ProjectSystemSkillBindingRow.system_skill_id, SkillRow.scope == "system", SkillRow.project_id.is_(None), SkillRow.status == "active"))
                    .join(SkillVersionRow, and_(SkillVersionRow.id == SkillRow.current_version_id, SkillVersionRow.skill_id == SkillRow.id, SkillVersionRow.revoked_at.is_(None)))
                    .where(ProjectSystemSkillBindingRow.project_id == context.project_id, ProjectSystemSkillBindingRow.enabled.is_(True))
                    .order_by(SkillRow.id)
                    .with_for_update(read=True, of=[ProjectSystemSkillBindingRow, SkillRow, SkillVersionRow])
                )
            )
            .scalars()
            .all()
        )
        mcp_ids = (
            (
                await self.session.execute(
                    select(McpServerVersionRow.id)
                    .select_from(ProjectSystemMcpBindingRow)
                    .join(McpServerRow, and_(McpServerRow.id == ProjectSystemMcpBindingRow.system_mcp_server_id, McpServerRow.scope == "system", McpServerRow.project_id.is_(None), McpServerRow.status == "active"))
                    .join(McpServerVersionRow, and_(McpServerVersionRow.id == ProjectSystemMcpBindingRow.mcp_server_version_id, McpServerVersionRow.mcp_server_id == McpServerRow.id, McpServerVersionRow.workflow_status == "published"))
                    .where(ProjectSystemMcpBindingRow.project_id == context.project_id, ProjectSystemMcpBindingRow.enabled.is_(True))
                    .order_by(McpServerRow.id, McpServerVersionRow.id)
                    .with_for_update(read=True, of=[ProjectSystemMcpBindingRow, McpServerRow, McpServerVersionRow])
                )
            )
            .scalars()
            .all()
        )
        return tuple(SkillAssetRef(AssetScope.SYSTEM, value) for value in skill_ids), tuple(mcp_ids)

    async def lock_skill_asset_slugs(self, refs: Sequence[SkillAssetRef]) -> tuple[str, ...]:
        if not refs:
            return ()
        return tuple((await self.session.execute(select(SkillRow.slug).where(SkillRow.id.in_(tuple(ref.asset_id for ref in refs))).order_by(SkillRow.id).with_for_update(read=True, of=SkillRow))).scalars().all())

    async def resolve_project_mcp_versions(self, context: ProjectContext, version_ids: Sequence[uuid.UUID]) -> tuple[uuid.UUID, ...]:
        self._require_project_actor(context)
        if not version_ids:
            return ()
        project_ids = (
            (
                await self.session.execute(
                    select(McpServerVersionRow.id)
                    .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
                    .where(
                        McpServerVersionRow.id.in_(version_ids),
                        McpServerVersionRow.workflow_status == "published",
                        McpServerRow.scope == "project",
                        McpServerRow.project_id == context.project_id,
                        McpServerRow.status == "active",
                        self._project_context_exists(context),
                    )
                    .with_for_update(read=True, of=[McpServerRow, McpServerVersionRow])
                )
            )
            .scalars()
            .all()
        )
        system_ids = (
            (
                await self.session.execute(
                    select(McpServerVersionRow.id)
                    .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
                    .join(ProjectSystemMcpBindingRow, and_(ProjectSystemMcpBindingRow.system_mcp_server_id == McpServerRow.id, ProjectSystemMcpBindingRow.mcp_server_version_id == McpServerVersionRow.id))
                    .where(
                        McpServerVersionRow.id.in_(version_ids),
                        McpServerVersionRow.workflow_status == "published",
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        McpServerRow.status == "active",
                        ProjectSystemMcpBindingRow.project_id == context.project_id,
                        ProjectSystemMcpBindingRow.enabled.is_(True),
                        self._project_context_exists(context),
                    )
                    .with_for_update(read=True, of=[McpServerRow, McpServerVersionRow, ProjectSystemMcpBindingRow])
                )
            )
            .scalars()
            .all()
        )
        return tuple((*project_ids, *system_ids))

    async def resolve_system_skill_refs(self, context: SystemAssetGovernanceContext, refs: Sequence[SkillAssetRef], *, require_runnable: bool) -> tuple[SkillAssetRef, ...]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        if any(ref.scope is not AssetScope.SYSTEM for ref in refs):
            return ()
        if not refs:
            return ()
        statement = (
            select(SkillRow.id)
            .join(SkillVersionRow, and_(SkillVersionRow.skill_id == SkillRow.id, SkillVersionRow.id == SkillRow.current_version_id))
            .where(
                SkillRow.id.in_(tuple(ref.asset_id for ref in refs)),
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                *((SkillRow.status == "active", SkillVersionRow.revoked_at.is_(None)) if require_runnable else ()),
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        )
        found = set((await self.session.execute(statement)).scalars().all())
        return tuple(ref for ref in refs if ref.asset_id in found)

    async def resolve_override_skill_refs(self, context: SystemAssetGovernanceContext, refs: Sequence[SkillAssetRef], *, require_runnable: bool) -> tuple[SkillAssetRef, ...]:
        await self._lock_override_project(context)
        if not refs:
            return ()
        project_ids = tuple(ref.asset_id for ref in refs if ref.scope is AssetScope.PROJECT)
        system_ids = tuple(ref.asset_id for ref in refs if ref.scope is AssetScope.SYSTEM)
        project_found = set(
            (
                await self.session.execute(
                    select(SkillRow.id)
                    .where(
                        SkillRow.id.in_(project_ids),
                        SkillRow.scope == "project",
                        SkillRow.project_id == context.project_id,
                        *((SkillRow.status == "active", SkillRow.current_version_id.is_not(None)) if require_runnable else (SkillRow.status != "archived",)),
                    )
                    .with_for_update(read=True, of=SkillRow)
                )
            )
            .scalars()
            .all()
        )
        system_found = set(
            (
                await self.session.execute(
                    select(SkillRow.id)
                    .join(ProjectSystemSkillBindingRow, ProjectSystemSkillBindingRow.system_skill_id == SkillRow.id)
                    .join(SkillVersionRow, and_(SkillVersionRow.skill_id == SkillRow.id, SkillVersionRow.id == SkillRow.current_version_id))
                    .where(
                        SkillRow.id.in_(system_ids),
                        SkillRow.scope == "system",
                        SkillRow.project_id.is_(None),
                        ProjectSystemSkillBindingRow.project_id == context.project_id,
                        ProjectSystemSkillBindingRow.enabled.is_(True),
                        *((SkillRow.status == "active", SkillVersionRow.revoked_at.is_(None)) if require_runnable else ()),
                    )
                    .with_for_update(read=True, of=[SkillRow, SkillVersionRow, ProjectSystemSkillBindingRow])
                )
            )
            .scalars()
            .all()
        )
        return tuple(ref for ref in refs if (ref.scope is AssetScope.PROJECT and ref.asset_id in project_found) or (ref.scope is AssetScope.SYSTEM and ref.asset_id in system_found))

    async def resolve_system_mcp_versions(self, context: SystemAssetGovernanceContext, version_ids: Sequence[uuid.UUID]) -> tuple[uuid.UUID, ...]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        if not version_ids:
            return ()
        return tuple(
            (
                await self.session.execute(
                    select(McpServerVersionRow.id)
                    .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
                    .where(McpServerVersionRow.id.in_(version_ids), McpServerVersionRow.workflow_status == "published", McpServerRow.scope == "system", McpServerRow.project_id.is_(None), McpServerRow.status == "active")
                    .with_for_update(read=True, of=[McpServerRow, McpServerVersionRow])
                )
            )
            .scalars()
            .all()
        )

    async def resolve_override_mcp_versions(self, context: SystemAssetGovernanceContext, version_ids: Sequence[uuid.UUID]) -> tuple[uuid.UUID, ...]:
        await self._lock_override_project(context)
        if not version_ids:
            return ()
        project_ids = (
            (
                await self.session.execute(
                    select(McpServerVersionRow.id)
                    .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
                    .where(McpServerVersionRow.id.in_(version_ids), McpServerVersionRow.workflow_status == "published", McpServerRow.scope == "project", McpServerRow.project_id == context.project_id, McpServerRow.status == "active")
                    .with_for_update(read=True, of=[McpServerRow, McpServerVersionRow])
                )
            )
            .scalars()
            .all()
        )
        system_ids = (
            (
                await self.session.execute(
                    select(McpServerVersionRow.id)
                    .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
                    .join(ProjectSystemMcpBindingRow, and_(ProjectSystemMcpBindingRow.system_mcp_server_id == McpServerRow.id, ProjectSystemMcpBindingRow.mcp_server_version_id == McpServerVersionRow.id))
                    .where(
                        McpServerVersionRow.id.in_(version_ids),
                        McpServerVersionRow.workflow_status == "published",
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        McpServerRow.status == "active",
                        ProjectSystemMcpBindingRow.project_id == context.project_id,
                        ProjectSystemMcpBindingRow.enabled.is_(True),
                    )
                    .with_for_update(read=True, of=[McpServerRow, McpServerVersionRow, ProjectSystemMcpBindingRow])
                )
            )
            .scalars()
            .all()
        )
        return tuple((*project_ids, *system_ids))


__all__ = ["AgentDefinitionRecord", "AgentRepository"]
