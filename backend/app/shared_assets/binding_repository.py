from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import (
    AssetForbidden,
    AssetNotFound,
    AssetValidationFailed,
    SkillRuntimeNameConflict,
)
from app.shared_assets.internal_assets import (
    BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
)
from app.shared_assets.mcp_secret_closure import lock_mcp_secret_closure
from app.shared_assets.models import AssetKind, AssetScope, AssetSelection
from app.shared_assets.skill_secret_closure import (
    SkillSecretClosureInvalid,
    lock_skill_secret_closure,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
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
    SkillVersionRow,
)

_Actor = ProjectContext | SystemAssetGovernanceContext


@dataclass(frozen=True)
class BindingTarget:
    asset: AgentRow | SkillRow | McpServerRow
    version: AgentRow | SkillVersionRow | McpServerVersionRow

    @property
    def version_id(self) -> uuid.UUID:
        """Return the stable wire identifier for the selected definition/version."""

        if isinstance(self.version, AgentRow):
            return self.version.definition_id
        return self.version.id


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
_TARGET_TYPES = {
    AssetKind.SKILL: (SkillRow, SkillVersionRow, "skill_id"),
    AssetKind.MCP: (McpServerRow, McpServerVersionRow, "mcp_server_id"),
}


def _request_id(context: object) -> str:
    value = getattr(context, "request_id", None)
    return value if isinstance(value, str) else "unknown"


class BindingRepository:
    """Project-scoped system binding persistence with a fixed lock order."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _require_actor(context: _Actor) -> None:
        if isinstance(context, ProjectContext):
            return
        if isinstance(context, SystemAssetGovernanceContext) and context.project_id is not None:
            return
        raise AssetForbidden(_request_id(context))

    @staticmethod
    def _project_id(context: _Actor) -> uuid.UUID:
        BindingRepository._require_actor(context)
        project_id = context.project_id
        if not isinstance(project_id, uuid.UUID):
            raise AssetForbidden(_request_id(context))
        return project_id

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

    async def lock_project(self, context: _Actor, *, read: bool = False) -> None:
        project_id = self._project_id(context)
        statement = select(ProjectRow.id).where(
            ProjectRow.id == project_id,
            ProjectRow.status == "active",
            ProjectRow.is_suspended.is_(False),
        )
        if isinstance(context, ProjectContext):
            statement = statement.join(
                ProjectMembershipRow,
                ProjectMembershipRow.project_id == ProjectRow.id,
            ).where(
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
            statement = statement.with_for_update(read=read, of=[ProjectRow, ProjectMembershipRow])
        else:
            statement = statement.with_for_update(read=read, of=ProjectRow)
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def get_binding(
        self,
        context: _Actor,
        kind: AssetKind,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
        read: bool = False,
        required: bool = True,
    ):
        project_id = self._project_id(context)
        binding_type, asset_column, _version_column = _BINDING_TYPES[kind]
        statement = select(binding_type).where(
            binding_type.project_id == project_id,
            getattr(binding_type, asset_column) == asset_id,
        )
        if isinstance(context, ProjectContext):
            statement = statement.where(self._project_context_exists(context))
        if for_update:
            statement = statement.with_for_update(read=read, of=binding_type)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None and required:
            raise AssetNotFound(context.request_id)
        return row

    async def list_bindings(
        self,
        context: _Actor,
        kind: AssetKind,
    ) -> tuple[object, ...]:
        """Return this project's persisted bindings after validating scope.

        The explicit project lock distinguishes a valid project with no bindings
        from a stale or cross-project context, which must remain a 404.
        """
        await self.lock_project(context, read=True)
        project_id = self._project_id(context)
        binding_type, asset_column, _version_column = _BINDING_TYPES[kind]
        statement = select(binding_type).where(binding_type.project_id == project_id)
        if isinstance(context, ProjectContext):
            statement = statement.where(self._project_context_exists(context))
        statement = statement.order_by(getattr(binding_type, asset_column))
        return tuple((await self.session.execute(statement)).scalars().all())

    async def current_version_id(
        self,
        context: _Actor,
        kind: AssetKind,
        asset_id: uuid.UUID,
    ) -> uuid.UUID:
        if kind is AssetKind.MCP:
            row = await self.get_binding(context, kind, asset_id, read=True)
            return uuid.UUID(str(row.mcp_server_version_id))
        if kind is AssetKind.AGENT:
            value = await self.session.scalar(
                select(AgentRow.definition_id).where(
                    AgentRow.id == asset_id,
                    AgentRow.scope == AssetScope.SYSTEM.value,
                    AgentRow.project_id.is_(None),
                )
            )
            if not isinstance(value, uuid.UUID):
                raise AssetNotFound(context.request_id)
            return value
        asset_type, _version_type, _parent_column = _TARGET_TYPES[kind]
        value = await self.session.scalar(
            select(asset_type.current_version_id).where(
                asset_type.id == asset_id,
                asset_type.scope == AssetScope.SYSTEM.value,
                asset_type.project_id.is_(None),
            )
        )
        if not isinstance(value, uuid.UUID):
            raise AssetNotFound(context.request_id)
        return value

    async def lock_target(
        self,
        context: _Actor,
        selection: AssetSelection,
        *,
        allow_archived: bool = False,
        read: bool = False,
    ) -> BindingTarget:
        if selection.kind is AssetKind.MCP and selection.version_id is None:
            raise AssetValidationFailed(context.request_id)
        if selection.kind is AssetKind.AGENT:
            asset_type = AgentRow
        else:
            asset_type, version_type, parent_column = _TARGET_TYPES[selection.kind]
        asset_statement = (
            select(asset_type)
            .where(
                asset_type.id == selection.asset_id,
                asset_type.scope == "system",
                asset_type.project_id.is_(None),
            )
            .with_for_update(read=read, of=asset_type)
        )
        asset = (await self.session.execute(asset_statement)).scalar_one_or_none()
        if asset is None:
            raise AssetNotFound(context.request_id)
        if selection.kind is AssetKind.AGENT and isinstance(asset, AgentRow) and asset.source_key == BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY:
            # The Builder is a server implementation detail. Knowing its UUID
            # must not turn it into a project-bindable System Agent.
            raise AssetNotFound(context.request_id)
        if asset.status == "suspended" or (asset.status == "archived" and not allow_archived):
            raise AssetValidationFailed(context.request_id)
        if selection.kind is AssetKind.AGENT:
            if selection.version_id is not None and selection.version_id != asset.definition_id:
                raise AssetValidationFailed(context.request_id)
            return BindingTarget(asset, asset)
        resolved_version_id = selection.version_id if selection.kind is AssetKind.MCP else asset.current_version_id
        if not isinstance(resolved_version_id, uuid.UUID):
            raise AssetValidationFailed(context.request_id)
        version_statement = (
            select(version_type)
            .where(
                version_type.id == resolved_version_id,
                getattr(version_type, parent_column) == selection.asset_id,
            )
            .with_for_update(read=read, of=version_type)
        )
        if selection.kind in {AssetKind.AGENT, AssetKind.SKILL}:
            version_statement = version_statement.where(
                version_type.version_number == 1,
            )
        if selection.kind is AssetKind.SKILL:
            version_statement = version_statement.where(
                SkillVersionRow.revoked_at.is_(None),
            )
        elif selection.kind is AssetKind.MCP:
            version_statement = version_statement.where(
                McpServerVersionRow.workflow_status == "published",
            )
        version = (await self.session.execute(version_statement)).scalar_one_or_none()
        if version is None:
            raise AssetValidationFailed(context.request_id)
        return BindingTarget(asset, version)

    async def ensure_system_skill_runtime_name_available(
        self,
        context: _Actor,
        target: BindingTarget,
    ) -> None:
        """Reject a System Skill whose runtime name is already active in-project.

        The caller owns the project lock before reaching this check.  Project
        Skill activation takes the same project lock before performing the
        inverse check, so concurrent enable/activate operations cannot both
        commit a duplicate runtime name.
        """

        project_id = self._project_id(context)
        if not isinstance(target.asset, SkillRow) or target.asset.scope != "system":
            raise AssetValidationFailed(context.request_id)
        conflict = await self.session.scalar(
            select(
                exists().where(
                    SkillRow.scope == "project",
                    SkillRow.project_id == project_id,
                    SkillRow.status == "active",
                    func.lower(SkillRow.slug) == target.asset.slug.casefold(),
                )
            )
        )
        if conflict:
            raise SkillRuntimeNameConflict(context.request_id)

    async def lock_current_system_mcp_target(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
    ) -> BindingTarget:
        """Lock one System MCP and resolve its current version under that lock."""

        if not isinstance(context, ProjectContext):
            raise AssetForbidden(_request_id(context))
        if not isinstance(asset_id, uuid.UUID):
            raise AssetValidationFailed(context.request_id)
        asset_statement = (
            select(McpServerRow)
            .where(
                McpServerRow.id == asset_id,
                McpServerRow.scope == "system",
                McpServerRow.project_id.is_(None),
            )
            .with_for_update(of=McpServerRow)
        )
        asset = (await self.session.execute(asset_statement)).scalar_one_or_none()
        if asset is None:
            raise AssetNotFound(context.request_id)
        current_version_id = asset.current_published_version_id
        if asset.status != "active" or not isinstance(current_version_id, uuid.UUID):
            raise AssetValidationFailed(context.request_id)
        version_statement = (
            select(McpServerVersionRow)
            .where(
                McpServerVersionRow.id == current_version_id,
                McpServerVersionRow.mcp_server_id == asset_id,
                McpServerVersionRow.workflow_status == "published",
            )
            .with_for_update(of=McpServerVersionRow)
        )
        version = (await self.session.execute(version_statement)).scalar_one_or_none()
        if version is None:
            raise AssetValidationFailed(context.request_id)
        return BindingTarget(asset, version)

    async def lock_system_version(
        self,
        context: _Actor,
        kind: AssetKind,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        read: bool = False,
        allow_revoked: bool = False,
    ):
        self._require_actor(context)
        if kind is AssetKind.AGENT:
            statement = (
                select(AgentRow)
                .where(
                    AgentRow.id == asset_id,
                    AgentRow.scope == AssetScope.SYSTEM.value,
                    AgentRow.project_id.is_(None),
                    AgentRow.definition_id == version_id,
                )
                .with_for_update(read=read, of=AgentRow)
            )
            definition = (await self.session.execute(statement)).scalar_one_or_none()
            if definition is None:
                raise AssetNotFound(context.request_id)
            return definition
        _asset_type, version_type, parent_column = _TARGET_TYPES[kind]
        statement = (
            select(version_type)
            .where(
                version_type.id == version_id,
                getattr(version_type, parent_column) == asset_id,
            )
            .with_for_update(read=read, of=version_type)
        )
        if kind is AssetKind.MCP:
            statement = statement.where(
                McpServerVersionRow.workflow_status == "published",
            )
        if kind is AssetKind.SKILL and not allow_revoked:
            statement = statement.where(SkillVersionRow.revoked_at.is_(None))
        version = (await self.session.execute(statement)).scalar_one_or_none()
        if version is None:
            raise AssetNotFound(context.request_id)
        return version

    async def add_binding(
        self,
        context: _Actor,
        selection: AssetSelection,
    ):
        project_id = self._project_id(context)
        binding_type, asset_column, version_column = _BINDING_TYPES[selection.kind]
        values = {asset_column: selection.asset_id}
        if version_column is not None:
            values[version_column] = selection.version_id
        row = binding_type(
            project_id=project_id,
            **values,
            enabled=True,
            created_by_user_id=str(context.user_id),
            updated_by_user_id=str(context.user_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def validate_target_dependencies(
        self,
        context: _Actor,
        selection: AssetSelection,
    ) -> None:
        if selection.kind is AssetKind.MCP:
            if selection.version_id is None:
                raise AssetValidationFailed(context.request_id)
            await self._validate_mcp_versions(
                (selection.version_id,),
                context,
                require_secrets=False,
            )
            return
        target = await self.lock_target(context, selection, read=True)
        if selection.kind is AssetKind.SKILL:
            return
        if selection.kind is not AssetKind.AGENT:
            return
        skill_ids = tuple(
            (
                await self.session.execute(
                    select(
                        AgentSkillRefRow.skill_asset_scope,
                        AgentSkillRefRow.skill_asset_id,
                    )
                    .where(AgentSkillRefRow.agent_id == target.asset.id)
                    .order_by(AgentSkillRefRow.sort_order)
                    .with_for_update(read=True, of=AgentSkillRefRow)
                )
            ).all()
        )
        mcp_ids = tuple(
            (await self.session.execute(select(AgentMcpRefRow.mcp_server_version_id).where(AgentMcpRefRow.agent_id == target.asset.id).order_by(AgentMcpRefRow.mcp_server_version_id).with_for_update(read=True, of=AgentMcpRefRow)))
            .scalars()
            .all()
        )
        if any(scope != AssetScope.SYSTEM.value for scope, _asset_id in skill_ids):
            raise AssetValidationFailed(context.request_id)
        for _scope, skill_asset_id in skill_ids:
            binding = await self.get_binding(
                context,
                AssetKind.SKILL,
                skill_asset_id,
                read=True,
                required=False,
            )
            if binding is None or not binding.enabled:
                raise AssetValidationFailed(context.request_id)
            skill_target = await self.lock_target(
                context,
                AssetSelection(AssetKind.SKILL, skill_asset_id),
                read=True,
            )
            try:
                await lock_skill_secret_closure(
                    self.session,
                    self._project_id(context),
                    skill_asset_id,
                    skill_target.version.id,
                )
            except SkillSecretClosureInvalid:
                raise AssetValidationFailed(context.request_id) from None
        if not await self._system_versions_are_bound(
            context,
            AssetKind.MCP,
            mcp_ids,
        ):
            raise AssetValidationFailed(context.request_id)
        await self._validate_mcp_versions(
            mcp_ids,
            context,
            require_secrets=True,
        )

    async def _system_versions_are_bound(
        self,
        context: _Actor,
        kind: AssetKind,
        version_ids: Sequence[uuid.UUID],
    ) -> bool:
        if not version_ids:
            return True
        project_id = self._project_id(context)
        binding_type, asset_column, version_column = _BINDING_TYPES[kind]
        asset_type, version_type, parent_column = _TARGET_TYPES[kind]
        for version_id in sorted(
            {uuid.UUID(str(value)) for value in version_ids},
            key=lambda value: value.int,
        ):
            binding = (
                await self.session.execute(
                    select(binding_type)
                    .where(
                        binding_type.project_id == project_id,
                        getattr(binding_type, version_column) == version_id,
                        binding_type.enabled.is_(True),
                    )
                    .with_for_update(read=True, of=binding_type)
                )
            ).scalar_one_or_none()
            if binding is None:
                return False
            asset_id = uuid.UUID(str(getattr(binding, asset_column)))
            asset = (
                await self.session.execute(
                    select(asset_type)
                    .where(
                        asset_type.id == asset_id,
                        asset_type.scope == "system",
                        asset_type.project_id.is_(None),
                        asset_type.status != "suspended",
                    )
                    .with_for_update(read=True, of=asset_type)
                )
            ).scalar_one_or_none()
            if asset is None:
                return False
            version = (
                await self.session.execute(
                    select(version_type)
                    .where(
                        version_type.id == version_id,
                        getattr(version_type, parent_column) == asset_id,
                        version_type.workflow_status == "published",
                    )
                    .with_for_update(read=True, of=version_type)
                )
            ).scalar_one_or_none()
            if kind is AssetKind.SKILL and version is not None and version.revoked_at is not None:
                return False
            if version is None:
                return False
        return True

    async def _validate_mcp_versions(
        self,
        version_ids: Sequence[uuid.UUID],
        context: _Actor,
        *,
        require_secrets: bool,
    ) -> None:
        if not version_ids:
            return
        version_rows = tuple(
            (
                await self.session.execute(
                    select(McpServerVersionRow, McpServerRow)
                    .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
                    .where(
                        McpServerVersionRow.id.in_(version_ids),
                        McpServerVersionRow.workflow_status == "published",
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        McpServerRow.status != "suspended",
                    )
                )
            ).all()
        )
        by_version = {version.id: (version, asset) for version, asset in version_rows}
        if set(by_version) != set(version_ids):
            raise AssetValidationFailed(context.request_id)
        if not require_secrets:
            return
        for version_id in sorted(by_version, key=lambda value: value.int):
            version, asset = by_version[version_id]
            slots = tuple((await self.session.execute(select(McpSecretSlotRow).where(McpSecretSlotRow.mcp_server_version_id == version.id).order_by(McpSecretSlotRow.id).with_for_update(read=True, of=McpSecretSlotRow))).scalars().all())
            await lock_mcp_secret_closure(
                self.session,
                project_id=self._project_id(context),
                mcp_server_id=asset.id,
                mcp_server_version_id=version.id,
                slots=slots,
                request_id=context.request_id,
            )
