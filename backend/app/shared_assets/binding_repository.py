from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound, AssetValidationFailed
from app.shared_assets.models import AssetKind, AssetSelection
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
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
    version: AgentVersionRow | SkillVersionRow | McpServerVersionRow


_BINDING_TYPES = {
    AssetKind.AGENT: (
        ProjectSystemAgentBindingRow,
        "system_agent_id",
        "agent_version_id",
    ),
    AssetKind.SKILL: (
        ProjectSystemSkillBindingRow,
        "system_skill_id",
        "skill_version_id",
    ),
    AssetKind.MCP: (
        ProjectSystemMcpBindingRow,
        "system_mcp_server_id",
        "mcp_server_version_id",
    ),
}
_TARGET_TYPES = {
    AssetKind.AGENT: (AgentRow, AgentVersionRow, "agent_id"),
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

    async def lock_target(
        self,
        context: _Actor,
        selection: AssetSelection,
        *,
        allow_archived: bool = False,
        read: bool = False,
    ) -> BindingTarget:
        if selection.version_id is None:
            raise AssetValidationFailed(context.request_id)
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
        if asset.status == "suspended" or (asset.status == "archived" and not allow_archived):
            raise AssetValidationFailed(context.request_id)
        version_statement = (
            select(version_type)
            .where(
                version_type.id == selection.version_id,
                getattr(version_type, parent_column) == selection.asset_id,
                version_type.workflow_status == "published",
            )
            .with_for_update(read=read, of=version_type)
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
    ):
        self._require_actor(context)
        _asset_type, version_type, parent_column = _TARGET_TYPES[kind]
        statement = (
            select(version_type)
            .where(
                version_type.id == version_id,
                getattr(version_type, parent_column) == asset_id,
                version_type.workflow_status == "published",
            )
            .with_for_update(read=read, of=version_type)
        )
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
        row = binding_type(
            project_id=project_id,
            **{
                asset_column: selection.asset_id,
                version_column: selection.version_id,
            },
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
        if selection.version_id is None:
            raise AssetValidationFailed(context.request_id)
        if selection.kind is AssetKind.MCP:
            await self._validate_mcp_versions((selection.version_id,), context.request_id)
            return
        if selection.kind is not AssetKind.AGENT:
            return
        skill_ids = tuple((await self.session.execute(select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == selection.version_id))).scalars().all())
        mcp_ids = tuple((await self.session.execute(select(AgentVersionMcpRefRow.mcp_server_version_id).where(AgentVersionMcpRefRow.agent_version_id == selection.version_id))).scalars().all())
        if not await self._system_versions_are_bound(
            context,
            AssetKind.SKILL,
            skill_ids,
        ):
            raise AssetValidationFailed(context.request_id)
        if not await self._system_versions_are_bound(
            context,
            AssetKind.MCP,
            mcp_ids,
        ):
            raise AssetValidationFailed(context.request_id)
        await self._validate_mcp_versions(mcp_ids, context.request_id)

    async def _system_versions_are_bound(
        self,
        context: _Actor,
        kind: AssetKind,
        version_ids: Sequence[uuid.UUID],
    ) -> bool:
        if not version_ids:
            return True
        project_id = self._project_id(context)
        binding_type, _asset_column, version_column = _BINDING_TYPES[kind]
        asset_type, version_type, parent_column = _TARGET_TYPES[kind]
        rows = tuple(
            (
                await self.session.execute(
                    select(getattr(binding_type, version_column))
                    .join(
                        version_type,
                        version_type.id == getattr(binding_type, version_column),
                    )
                    .join(
                        asset_type,
                        asset_type.id == getattr(version_type, parent_column),
                    )
                    .where(
                        binding_type.project_id == project_id,
                        binding_type.enabled.is_(True),
                        getattr(binding_type, version_column).in_(version_ids),
                        version_type.workflow_status == "published",
                        asset_type.scope == "system",
                        asset_type.project_id.is_(None),
                        asset_type.status != "suspended",
                    )
                )
            )
            .scalars()
            .all()
        )
        return set(rows) == set(version_ids)

    async def _validate_mcp_versions(
        self,
        version_ids: Sequence[uuid.UUID],
        request_id: str,
    ) -> None:
        if not version_ids:
            return
        version_rows = tuple(
            (
                await self.session.execute(
                    select(McpServerVersionRow.id)
                    .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
                    .where(
                        McpServerVersionRow.id.in_(version_ids),
                        McpServerVersionRow.workflow_status == "published",
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        McpServerRow.status != "suspended",
                    )
                )
            )
            .scalars()
            .all()
        )
        if set(version_rows) != set(version_ids):
            raise AssetValidationFailed(request_id)
        slot_rows = (
            await self.session.execute(
                select(
                    McpCredentialSlotRow.id,
                    McpCredentialSlotRow.mcp_server_version_id,
                    McpCredentialSlotRow.required,
                    CredentialGrantRow.id,
                    CredentialGrantRow.status,
                    CredentialRow.scope,
                    CredentialRow.project_id,
                    CredentialRow.status,
                    CredentialVersionRow.status,
                    exists(
                        select(1).where(
                            CredentialEnvelopeRow.credential_version_id == CredentialVersionRow.id,
                            CredentialEnvelopeRow.is_active.is_(True),
                        )
                    ).label("has_active_envelope"),
                )
                .outerjoin(
                    CredentialGrantRow,
                    and_(
                        CredentialGrantRow.mcp_server_version_id == McpCredentialSlotRow.mcp_server_version_id,
                        CredentialGrantRow.credential_slot_id == McpCredentialSlotRow.id,
                        CredentialGrantRow.status == "active",
                    ),
                )
                .outerjoin(
                    CredentialVersionRow,
                    CredentialVersionRow.id == CredentialGrantRow.credential_version_id,
                )
                .outerjoin(
                    CredentialRow,
                    CredentialRow.id == CredentialVersionRow.credential_id,
                )
                .where(McpCredentialSlotRow.mcp_server_version_id.in_(version_ids))
            )
        ).all()
        for (
            _slot_id,
            _version_id,
            required,
            grant_id,
            grant_status,
            credential_scope,
            credential_project_id,
            credential_status,
            version_status,
            has_active_envelope,
        ) in slot_rows:
            if grant_id is None:
                if required:
                    raise AssetValidationFailed(request_id)
                continue
            if grant_status != "active" or credential_scope != "system" or credential_project_id is not None or credential_status != "active" or version_status not in {"active", "retired"} or not has_active_envelope:
                raise AssetValidationFailed(request_id)
