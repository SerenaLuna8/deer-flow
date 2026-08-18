from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound
from app.shared_assets.internal_assets import (
    BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
)
from deerflow.persistence.projects.model import (
    ProjectDefaultAgentRow,
    ProjectMembershipRow,
    ProjectRow,
)
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
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
class AgentVersionRecord:
    row: AgentVersionRow
    skill_version_ids: tuple[uuid.UUID, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]


def _request_id(context: object) -> str:
    request_id = getattr(context, "request_id", None)
    return request_id if isinstance(request_id, str) else "unknown"


def _is_internal_skill_builder_agent(row: AgentRow) -> bool:
    return row.source_key == BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY


class AgentRepository:
    """Typed Agent persistence with scope embedded in every public lookup."""

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
    def _require_system_catalog_reader(
        context: SystemAssetGovernanceContext | SystemAssetReadContext,
    ) -> None:
        if not isinstance(context, (SystemAssetGovernanceContext, SystemAssetReadContext)):
            raise AssetForbidden(_request_id(context))
        if context.project_id is not None:
            raise AssetForbidden(context.request_id)

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

    async def _lock_project_context(self, context: ProjectContext) -> None:
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
            .with_for_update(read=True, of=[ProjectRow, ProjectMembershipRow])
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def _lock_override_project(self, context: SystemAssetGovernanceContext) -> None:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetForbidden(context.request_id)
        statement = (
            select(ProjectRow.id)
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
            .with_for_update(of=ProjectRow)
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def create_project_asset(self, context: ProjectContext, command: AgentCreateCommand) -> AgentRow:
        self._require_project_actor(context)
        await self._lock_project_context(context)
        row = AgentRow(
            scope="project",
            project_id=context.project_id,
            slug=command.slug,
            display_name=command.display_name,
            created_by_user_id=str(context.user_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def current_published_descriptions(
        self,
        asset_ids: Sequence[uuid.UUID],
    ) -> Mapping[uuid.UUID, str]:
        """Load current descriptions in one query for already-authorized rows."""

        ids = tuple(asset_ids)
        if not ids:
            return {}
        statement = (
            select(AgentRow.id, AgentVersionRow.description)
            .join(
                AgentVersionRow,
                AgentVersionRow.id == AgentRow.current_published_version_id,
            )
            .where(AgentRow.id.in_(ids))
        )
        return {asset_id: description for asset_id, description in (await self.session.execute(statement))}

    async def create_system_asset(
        self,
        context: SystemAssetGovernanceContext,
        command: AgentCreateCommand,
    ) -> AgentRow:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetForbidden(context.request_id)
        row = AgentRow(
            scope="system",
            project_id=None,
            slug=command.slug,
            display_name=command.display_name,
            created_by_user_id=str(context.user_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_override_asset(
        self,
        context: SystemAssetGovernanceContext,
        command: AgentCreateCommand,
    ) -> AgentRow:
        await self._lock_override_project(context)
        row = AgentRow(
            scope="project",
            project_id=context.project_id,
            slug=command.slug,
            display_name=command.display_name,
            created_by_user_id=str(context.user_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_project_asset(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentRow:
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

    async def ensure_not_current_project_default(
        self,
        context: ProjectContext | SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> None:
        """Lock the pointer before the Agent and reject destructive changes."""

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
        statement = (
            select(ProjectDefaultAgentRow.project_id)
            .where(
                ProjectDefaultAgentRow.project_id == project_id,
                ProjectDefaultAgentRow.agent_asset_id == asset_id,
            )
            .with_for_update(of=ProjectDefaultAgentRow)
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is not None:
            raise AssetConflict(context.request_id)

    async def clear_current_project_default(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
    ) -> bool:
        """Clear a matching default pointer before locking the Agent."""

        self._require_project_actor(context)
        await self._lock_project_context(context)
        pointer = (
            await self.session.execute(
                select(ProjectDefaultAgentRow)
                .where(
                    ProjectDefaultAgentRow.project_id == context.project_id,
                )
                .with_for_update(of=ProjectDefaultAgentRow)
            )
        ).scalar_one_or_none()
        if pointer is None or pointer.agent_asset_id != asset_id:
            return False
        pointer.agent_asset_id = None
        pointer.revision += 1
        pointer.updated_by_user_id = str(context.user_id)
        await self.session.flush()
        return True

    async def archive_project_asset(
        self,
        context: ProjectContext,
        asset: AgentRow,
    ) -> None:
        """Archive one locked project Agent while retaining its package."""

        self._require_project_actor(context)
        if asset.scope != "project" or asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        if asset.status == "archived":
            raise AssetConflict(context.request_id)
        asset.status = "archived"
        asset.version += 1
        await self.session.flush()

    async def get_system_asset(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentRow:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = select(AgentRow).where(
            AgentRow.id == asset_id,
            AgentRow.scope == "system",
            AgentRow.project_id.is_(None),
            or_(
                AgentRow.source_key.is_(None),
                AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
            ),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None or _is_internal_skill_builder_agent(row):
            raise AssetNotFound(context.request_id)
        return row

    async def get_override_asset(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentRow:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self._lock_override_project(context)
        statement = select(AgentRow).where(
            AgentRow.id == asset_id,
            AgentRow.scope == "project",
            AgentRow.project_id == context.project_id,
        )
        if for_update:
            statement = statement.with_for_update(of=AgentRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def next_project_version_number(self, context: ProjectContext, asset: AgentRow) -> int:
        self._require_project_actor(context)
        statement = (
            select(func.coalesce(func.max(AgentVersionRow.version_number), 0) + 1)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentRow.id == asset.id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
                self._project_context_exists(context),
            )
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def next_system_version_number(
        self,
        context: SystemAssetGovernanceContext,
        asset: AgentRow,
    ) -> int:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(func.coalesce(func.max(AgentVersionRow.version_number), 0) + 1)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentRow.id == asset.id,
                AgentRow.scope == "system",
                AgentRow.project_id.is_(None),
            )
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def next_override_version_number(
        self,
        context: SystemAssetGovernanceContext,
        asset: AgentRow,
    ) -> int:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self._lock_override_project(context)
        statement = (
            select(func.coalesce(func.max(AgentVersionRow.version_number), 0) + 1)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentRow.id == asset.id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
            )
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def create_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version: AgentVersionRow,
        skill_version_ids: Sequence[uuid.UUID],
        mcp_version_ids: Sequence[uuid.UUID],
    ) -> AgentVersionRecord:
        self._require_project_actor(context)
        asset = await self.get_project_asset(context, asset_id, for_update=True)
        if version.agent_id != asset.id:
            raise AssetNotFound(context.request_id)
        self.session.add(version)
        await self.session.flush()
        await self._add_refs(version.id, skill_version_ids, mcp_version_ids)
        return AgentVersionRecord(version, tuple(skill_version_ids), tuple(mcp_version_ids))

    async def create_system_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version: AgentVersionRow,
        skill_version_ids: Sequence[uuid.UUID],
        mcp_version_ids: Sequence[uuid.UUID],
    ) -> AgentVersionRecord:
        self._require_system_actor(context)
        asset = await self.get_system_asset(context, asset_id, for_update=True)
        if version.agent_id != asset.id:
            raise AssetNotFound(context.request_id)
        self.session.add(version)
        await self.session.flush()
        await self._add_refs(version.id, skill_version_ids, mcp_version_ids)
        return AgentVersionRecord(version, tuple(skill_version_ids), tuple(mcp_version_ids))

    async def create_override_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version: AgentVersionRow,
        skill_version_ids: Sequence[uuid.UUID],
        mcp_version_ids: Sequence[uuid.UUID],
    ) -> AgentVersionRecord:
        self._require_system_actor(context)
        asset = await self.get_override_asset(context, asset_id, for_update=True)
        if version.agent_id != asset.id:
            raise AssetNotFound(context.request_id)
        self.session.add(version)
        await self.session.flush()
        await self._add_refs(version.id, skill_version_ids, mcp_version_ids)
        return AgentVersionRecord(version, tuple(skill_version_ids), tuple(mcp_version_ids))

    async def _add_refs(
        self,
        version_id: uuid.UUID,
        skill_version_ids: Sequence[uuid.UUID],
        mcp_version_ids: Sequence[uuid.UUID],
    ) -> None:
        self.session.add_all(
            AgentVersionSkillRefRow(
                agent_version_id=version_id,
                skill_version_id=dependency_id,
                sort_order=index,
            )
            for index, dependency_id in enumerate(skill_version_ids)
        )
        self.session.add_all(
            AgentVersionMcpRefRow(
                agent_version_id=version_id,
                mcp_server_version_id=dependency_id,
                sort_order=index,
            )
            for index, dependency_id in enumerate(mcp_version_ids)
        )
        await self.session.flush()

    async def get_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersionRecord:
        self._require_project_actor(context)
        statement = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentVersionRow.id == version_id,
                AgentVersionRow.agent_id == asset_id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
                self._project_context_exists(context),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=AgentVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        skill_ids, mcp_ids = await self._load_refs((row.id,), for_update=for_update)
        return AgentVersionRecord(row, skill_ids.get(row.id, ()), mcp_ids.get(row.id, ()))

    async def get_system_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersionRecord:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentVersionRow.id == version_id,
                AgentVersionRow.agent_id == asset_id,
                AgentRow.scope == "system",
                AgentRow.project_id.is_(None),
                or_(
                    AgentRow.source_key.is_(None),
                    AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
                ),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=AgentVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        skill_ids, mcp_ids = await self._load_refs((row.id,), for_update=for_update)
        return AgentVersionRecord(row, skill_ids.get(row.id, ()), mcp_ids.get(row.id, ()))

    async def get_override_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersionRecord:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self._lock_override_project(context)
        statement = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentVersionRow.id == version_id,
                AgentVersionRow.agent_id == asset_id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=AgentVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        skill_ids, mcp_ids = await self._load_refs((row.id,), for_update=for_update)
        return AgentVersionRecord(row, skill_ids.get(row.id, ()), mcp_ids.get(row.id, ()))

    async def get_latest_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersionRecord | None:
        self._require_project_actor(context)
        statement = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentVersionRow.agent_id == asset_id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
                self._project_context_exists(context),
            )
            .order_by(AgentVersionRow.version_number.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update(of=AgentVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None
        skill_ids, mcp_ids = await self._load_refs((row.id,), for_update=for_update)
        return AgentVersionRecord(row, skill_ids.get(row.id, ()), mcp_ids.get(row.id, ()))

    async def get_latest_system_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersionRecord | None:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentVersionRow.agent_id == asset_id,
                AgentRow.scope == "system",
                AgentRow.project_id.is_(None),
                or_(
                    AgentRow.source_key.is_(None),
                    AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
                ),
            )
            .order_by(AgentVersionRow.version_number.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update(of=AgentVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None
        skill_ids, mcp_ids = await self._load_refs((row.id,), for_update=for_update)
        return AgentVersionRecord(row, skill_ids.get(row.id, ()), mcp_ids.get(row.id, ()))

    async def get_latest_override_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersionRecord | None:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self._lock_override_project(context)
        statement = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentVersionRow.agent_id == asset_id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
            )
            .order_by(AgentVersionRow.version_number.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update(of=AgentVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None
        skill_ids, mcp_ids = await self._load_refs((row.id,), for_update=for_update)
        return AgentVersionRecord(row, skill_ids.get(row.id, ()), mcp_ids.get(row.id, ()))

    async def list_project_visible(self, context: ProjectContext) -> tuple[AgentRow, ...]:
        self._require_project_actor(context)
        await self._lock_project_context(context)
        project_statement = select(AgentRow).where(
            AgentRow.scope == "project",
            AgentRow.project_id == context.project_id,
            AgentRow.status != "archived",
            self._project_context_exists(context),
        )
        system_statement = select(AgentRow).where(
            AgentRow.scope == "system",
            AgentRow.project_id.is_(None),
            or_(
                AgentRow.source_key.is_(None),
                AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
            ),
            self._project_context_exists(context),
        )
        project_rows = (await self.session.execute(project_statement)).scalars().all()
        system_rows = (await self.session.execute(system_statement)).scalars().all()
        # Keep a defensive in-process filter as well. It protects the catalog
        # boundary when a test double, proxy, or future query rewrite does not
        # enforce the SQL predicate exactly as intended.
        public_system_rows = tuple(row for row in system_rows if not _is_internal_skill_builder_agent(row))
        return tuple(
            sorted(
                (*project_rows, *public_system_rows),
                key=lambda row: (row.created_at, row.id),
            )
        )

    async def list_system_visible(
        self,
        context: SystemAssetGovernanceContext | SystemAssetReadContext,
    ) -> tuple[AgentRow, ...]:
        self._require_system_catalog_reader(context)
        statement = select(AgentRow).where(
            AgentRow.scope == "system",
            AgentRow.project_id.is_(None),
            or_(
                AgentRow.source_key.is_(None),
                AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
            ),
        )
        statement = statement.order_by(AgentRow.created_at, AgentRow.id)
        rows = tuple((await self.session.execute(statement)).scalars().all())
        return tuple(row for row in rows if not _is_internal_skill_builder_agent(row))

    async def list_override_visible(
        self,
        context: SystemAssetGovernanceContext,
    ) -> tuple[AgentRow, ...]:
        await self._lock_override_project(context)
        statement = (
            select(AgentRow)
            .where(
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
            )
            .order_by(AgentRow.created_at, AgentRow.id)
        )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def get_project_version_history(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
    ) -> tuple[AgentVersionRecord, ...]:
        self._require_project_actor(context)
        statement = (
            select(AgentRow.id, AgentVersionRow)
            .outerjoin(
                AgentVersionRow,
                and_(
                    AgentVersionRow.agent_id == AgentRow.id,
                    or_(
                        AgentRow.scope == "project",
                        and_(
                            AgentRow.scope == "system",
                            AgentVersionRow.workflow_status == "published",
                        ),
                    ),
                ),
            )
            .where(
                AgentRow.id == asset_id,
                or_(
                    and_(
                        AgentRow.scope == "project",
                        AgentRow.project_id == context.project_id,
                    ),
                    and_(
                        AgentRow.scope == "system",
                        AgentRow.project_id.is_(None),
                        or_(
                            AgentRow.source_key.is_(None),
                            AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
                        ),
                    ),
                ),
                self._project_context_exists(context),
            )
            .order_by(AgentVersionRow.version_number.desc())
        )
        scoped_rows = tuple((await self.session.execute(statement)).all())
        if not scoped_rows:
            raise AssetNotFound(context.request_id)
        rows = tuple(row[1] for row in scoped_rows if row[1] is not None)
        skill_ids, mcp_ids = await self._load_refs(tuple(row.id for row in rows))
        return tuple(
            AgentVersionRecord(
                row,
                skill_ids.get(row.id, ()),
                mcp_ids.get(row.id, ()),
            )
            for row in rows
        )

    async def get_system_version_history(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> tuple[AgentVersionRecord, ...]:
        self._require_system_actor(context)
        await self.get_system_asset(context, asset_id)
        statement = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentVersionRow.agent_id == asset_id,
                AgentRow.scope == "system",
                AgentRow.project_id.is_(None),
            )
            .order_by(AgentVersionRow.version_number.desc())
        )
        return await self._history(statement)

    async def get_override_version_history(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> tuple[AgentVersionRecord, ...]:
        self._require_system_actor(context)
        await self.get_override_asset(context, asset_id)
        statement = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(
                AgentVersionRow.agent_id == asset_id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
            )
            .order_by(AgentVersionRow.version_number.desc())
        )
        return await self._history(statement)

    async def _history(self, statement) -> tuple[AgentVersionRecord, ...]:
        rows = tuple((await self.session.execute(statement)).scalars().all())
        skill_ids, mcp_ids = await self._load_refs(tuple(row.id for row in rows))
        return tuple(AgentVersionRecord(row, skill_ids.get(row.id, ()), mcp_ids.get(row.id, ())) for row in rows)

    async def _load_refs(
        self,
        version_ids: Sequence[uuid.UUID],
        *,
        for_update: bool = False,
    ) -> tuple[dict[uuid.UUID, tuple[uuid.UUID, ...]], dict[uuid.UUID, tuple[uuid.UUID, ...]]]:
        skill_statement = (
            select(
                AgentVersionSkillRefRow.agent_version_id,
                AgentVersionSkillRefRow.skill_version_id,
            )
            .where(AgentVersionSkillRefRow.agent_version_id.in_(version_ids))
            .order_by(AgentVersionSkillRefRow.agent_version_id, AgentVersionSkillRefRow.sort_order)
        )
        mcp_statement = (
            select(
                AgentVersionMcpRefRow.agent_version_id,
                AgentVersionMcpRefRow.mcp_server_version_id,
            )
            .where(AgentVersionMcpRefRow.agent_version_id.in_(version_ids))
            .order_by(AgentVersionMcpRefRow.agent_version_id, AgentVersionMcpRefRow.sort_order)
        )
        if for_update:
            skill_statement = skill_statement.with_for_update(of=AgentVersionSkillRefRow)
            mcp_statement = mcp_statement.with_for_update(of=AgentVersionMcpRefRow)
        skill_map: dict[uuid.UUID, list[uuid.UUID]] = {}
        for version_id, dependency_id in (await self.session.execute(skill_statement)).all():
            skill_map.setdefault(version_id, []).append(dependency_id)
        mcp_map: dict[uuid.UUID, list[uuid.UUID]] = {}
        for version_id, dependency_id in (await self.session.execute(mcp_statement)).all():
            mcp_map.setdefault(version_id, []).append(dependency_id)
        return (
            {key: tuple(value) for key, value in skill_map.items()},
            {key: tuple(value) for key, value in mcp_map.items()},
        )

    async def resolve_project_skill_versions(
        self,
        context: ProjectContext,
        version_ids: Sequence[uuid.UUID],
    ) -> tuple[uuid.UUID, ...]:
        self._require_project_actor(context)
        if not version_ids:
            return ()
        project_statement = (
            select(SkillVersionRow.id)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id.in_(version_ids),
                SkillVersionRow.workflow_status == "published",
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status == "active",
                self._project_context_exists(context),
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        )
        system_statement = (
            select(SkillVersionRow.id)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .join(
                ProjectSystemSkillBindingRow,
                and_(
                    ProjectSystemSkillBindingRow.system_skill_id == SkillRow.id,
                    ProjectSystemSkillBindingRow.skill_version_id == SkillVersionRow.id,
                ),
            )
            .where(
                SkillVersionRow.id.in_(version_ids),
                SkillVersionRow.workflow_status == "published",
                SkillVersionRow.revoked_at.is_(None),
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                SkillRow.status == "active",
                ProjectSystemSkillBindingRow.project_id == context.project_id,
                ProjectSystemSkillBindingRow.enabled.is_(True),
                self._project_context_exists(context),
            )
            .with_for_update(
                read=True,
                of=[SkillRow, SkillVersionRow, ProjectSystemSkillBindingRow],
            )
        )
        project_ids = (await self.session.execute(project_statement)).scalars().all()
        system_ids = (await self.session.execute(system_statement)).scalars().all()
        return tuple((*project_ids, *system_ids))

    async def list_enabled_system_dependency_versions(
        self,
        context: ProjectContext,
    ) -> tuple[tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]:
        """Freeze every enabled, active, published System Skill/MCP binding.

        Agent revisions store exact dependency version IDs.  Defaults therefore
        come from the project's enabled bindings rather than from the global
        catalog, preserving the project authorization boundary.
        """

        self._require_project_actor(context)
        await self._lock_project_context(context)
        skill_statement = (
            select(SkillVersionRow.id)
            .select_from(ProjectSystemSkillBindingRow)
            .join(
                SkillRow,
                and_(
                    SkillRow.id == ProjectSystemSkillBindingRow.system_skill_id,
                    SkillRow.scope == "system",
                    SkillRow.project_id.is_(None),
                    SkillRow.status == "active",
                ),
            )
            .join(
                SkillVersionRow,
                and_(
                    SkillVersionRow.id == ProjectSystemSkillBindingRow.skill_version_id,
                    SkillVersionRow.skill_id == SkillRow.id,
                    SkillVersionRow.workflow_status == "published",
                    SkillVersionRow.revoked_at.is_(None),
                ),
            )
            .where(
                ProjectSystemSkillBindingRow.project_id == context.project_id,
                ProjectSystemSkillBindingRow.enabled.is_(True),
            )
            .order_by(SkillRow.id, SkillVersionRow.id)
            .with_for_update(
                read=True,
                of=[
                    ProjectSystemSkillBindingRow,
                    SkillRow,
                    SkillVersionRow,
                ],
            )
        )
        mcp_statement = (
            select(McpServerVersionRow.id)
            .select_from(ProjectSystemMcpBindingRow)
            .join(
                McpServerRow,
                and_(
                    McpServerRow.id == ProjectSystemMcpBindingRow.system_mcp_server_id,
                    McpServerRow.scope == "system",
                    McpServerRow.project_id.is_(None),
                    McpServerRow.status == "active",
                ),
            )
            .join(
                McpServerVersionRow,
                and_(
                    McpServerVersionRow.id == ProjectSystemMcpBindingRow.mcp_server_version_id,
                    McpServerVersionRow.mcp_server_id == McpServerRow.id,
                    McpServerVersionRow.workflow_status == "published",
                ),
            )
            .where(
                ProjectSystemMcpBindingRow.project_id == context.project_id,
                ProjectSystemMcpBindingRow.enabled.is_(True),
            )
            .order_by(McpServerRow.id, McpServerVersionRow.id)
            .with_for_update(
                read=True,
                of=[
                    ProjectSystemMcpBindingRow,
                    McpServerRow,
                    McpServerVersionRow,
                ],
            )
        )
        skill_ids = (await self.session.execute(skill_statement)).scalars().all()
        mcp_ids = (await self.session.execute(mcp_statement)).scalars().all()
        return tuple(skill_ids), tuple(mcp_ids)

    async def lock_skill_version_slugs(
        self,
        version_ids: Sequence[uuid.UUID],
    ) -> tuple[str, ...]:
        """Read slugs for dependency rows already scope-validated and locked."""

        if not version_ids:
            return ()
        statement = select(SkillRow.slug).join(SkillVersionRow, SkillVersionRow.skill_id == SkillRow.id).where(SkillVersionRow.id.in_(version_ids)).with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        return tuple((await self.session.execute(statement)).scalars().all())

    async def resolve_project_mcp_versions(
        self,
        context: ProjectContext,
        version_ids: Sequence[uuid.UUID],
    ) -> tuple[uuid.UUID, ...]:
        self._require_project_actor(context)
        if not version_ids:
            return ()
        project_statement = (
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
        system_statement = (
            select(McpServerVersionRow.id)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .join(
                ProjectSystemMcpBindingRow,
                and_(
                    ProjectSystemMcpBindingRow.system_mcp_server_id == McpServerRow.id,
                    ProjectSystemMcpBindingRow.mcp_server_version_id == McpServerVersionRow.id,
                ),
            )
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
            .with_for_update(
                read=True,
                of=[McpServerRow, McpServerVersionRow, ProjectSystemMcpBindingRow],
            )
        )
        project_ids = (await self.session.execute(project_statement)).scalars().all()
        system_ids = (await self.session.execute(system_statement)).scalars().all()
        return tuple((*project_ids, *system_ids))

    async def resolve_system_skill_versions(
        self,
        context: SystemAssetGovernanceContext,
        version_ids: Sequence[uuid.UUID],
    ) -> tuple[uuid.UUID, ...]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        if not version_ids:
            return ()
        statement = (
            select(SkillVersionRow.id)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id.in_(version_ids),
                SkillVersionRow.workflow_status == "published",
                SkillVersionRow.revoked_at.is_(None),
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                SkillRow.status == "active",
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def resolve_override_skill_versions(
        self,
        context: SystemAssetGovernanceContext,
        version_ids: Sequence[uuid.UUID],
    ) -> tuple[uuid.UUID, ...]:
        await self._lock_override_project(context)
        if not version_ids:
            return ()
        project_statement = (
            select(SkillVersionRow.id)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id.in_(version_ids),
                SkillVersionRow.workflow_status == "published",
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status == "active",
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        )
        system_statement = (
            select(SkillVersionRow.id)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .join(
                ProjectSystemSkillBindingRow,
                and_(
                    ProjectSystemSkillBindingRow.system_skill_id == SkillRow.id,
                    ProjectSystemSkillBindingRow.skill_version_id == SkillVersionRow.id,
                ),
            )
            .where(
                SkillVersionRow.id.in_(version_ids),
                SkillVersionRow.workflow_status == "published",
                SkillVersionRow.revoked_at.is_(None),
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                SkillRow.status == "active",
                ProjectSystemSkillBindingRow.project_id == context.project_id,
                ProjectSystemSkillBindingRow.enabled.is_(True),
            )
            .with_for_update(
                read=True,
                of=[SkillRow, SkillVersionRow, ProjectSystemSkillBindingRow],
            )
        )
        project_ids = (await self.session.execute(project_statement)).scalars().all()
        system_ids = (await self.session.execute(system_statement)).scalars().all()
        return tuple((*project_ids, *system_ids))

    async def resolve_system_mcp_versions(
        self,
        context: SystemAssetGovernanceContext,
        version_ids: Sequence[uuid.UUID],
    ) -> tuple[uuid.UUID, ...]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        if not version_ids:
            return ()
        statement = (
            select(McpServerVersionRow.id)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .where(
                McpServerVersionRow.id.in_(version_ids),
                McpServerVersionRow.workflow_status == "published",
                McpServerRow.scope == "system",
                McpServerRow.project_id.is_(None),
                McpServerRow.status == "active",
            )
            .with_for_update(read=True, of=[McpServerRow, McpServerVersionRow])
        )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def resolve_override_mcp_versions(
        self,
        context: SystemAssetGovernanceContext,
        version_ids: Sequence[uuid.UUID],
    ) -> tuple[uuid.UUID, ...]:
        await self._lock_override_project(context)
        if not version_ids:
            return ()
        project_statement = (
            select(McpServerVersionRow.id)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .where(
                McpServerVersionRow.id.in_(version_ids),
                McpServerVersionRow.workflow_status == "published",
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
                McpServerRow.status == "active",
            )
            .with_for_update(read=True, of=[McpServerRow, McpServerVersionRow])
        )
        system_statement = (
            select(McpServerVersionRow.id)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .join(
                ProjectSystemMcpBindingRow,
                and_(
                    ProjectSystemMcpBindingRow.system_mcp_server_id == McpServerRow.id,
                    ProjectSystemMcpBindingRow.mcp_server_version_id == McpServerVersionRow.id,
                ),
            )
            .where(
                McpServerVersionRow.id.in_(version_ids),
                McpServerVersionRow.workflow_status == "published",
                McpServerRow.scope == "system",
                McpServerRow.project_id.is_(None),
                McpServerRow.status == "active",
                ProjectSystemMcpBindingRow.project_id == context.project_id,
                ProjectSystemMcpBindingRow.enabled.is_(True),
            )
            .with_for_update(
                read=True,
                of=[McpServerRow, McpServerVersionRow, ProjectSystemMcpBindingRow],
            )
        )
        project_ids = (await self.session.execute(project_statement)).scalars().all()
        system_ids = (await self.session.execute(system_statement)).scalars().all()
        return tuple((*project_ids, *system_ids))
