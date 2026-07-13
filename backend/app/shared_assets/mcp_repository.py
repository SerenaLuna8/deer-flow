from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectSystemMcpBindingRow,
)


@dataclass(frozen=True)
class McpVersionRecord:
    row: McpServerVersionRow
    slots: tuple[McpCredentialSlotRow, ...]
    grants: tuple[CredentialGrantRow, ...]


@dataclass(frozen=True)
class GrantState:
    grant: CredentialGrantRow
    mcp_status: str
    mcp_workflow_status: str
    credential_status: str
    credential_version_status: str


def _request_id(context: object) -> str:
    value = getattr(context, "request_id", None)
    return value if isinstance(value, str) else "unknown"


class McpRepository:
    """MCP persistence with project or system scope fixed by trusted contexts."""

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

    async def lock_project(self, context: ProjectContext) -> None:
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
            .with_for_update(of=[ProjectRow, ProjectMembershipRow])
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def lock_override_project(self, context: SystemAssetGovernanceContext) -> None:
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

    async def create_project_asset(self, context: ProjectContext, row: McpServerRow) -> McpServerRow:
        await self.lock_project(context)
        if row.scope != "project" or row.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_override_asset(
        self,
        context: SystemAssetGovernanceContext,
        row: McpServerRow,
    ) -> McpServerRow:
        await self.lock_override_project(context)
        if row.scope != "project" or row.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_system_asset(
        self,
        context: SystemAssetGovernanceContext,
        row: McpServerRow,
    ) -> McpServerRow:
        self._require_system_actor(context)
        if context.project_id is not None or row.scope != "system" or row.project_id is not None:
            raise AssetNotFound(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_project_asset(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> McpServerRow:
        return await self._get_project_asset(
            context,
            asset_id,
            for_update=for_update,
            lock_project=True,
        )

    async def _get_project_asset_after_lock(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
    ) -> McpServerRow:
        return await self._get_project_asset(
            context,
            asset_id,
            for_update=True,
            lock_project=False,
        )

    async def _get_project_asset(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool,
        lock_project: bool,
    ) -> McpServerRow:
        self._require_project_actor(context)
        if for_update and lock_project:
            await self.lock_project(context)
        statement = select(McpServerRow).where(
            McpServerRow.id == asset_id,
            McpServerRow.scope == "project",
            McpServerRow.project_id == context.project_id,
            self._project_context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=McpServerRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def get_override_asset(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> McpServerRow:
        return await self._get_override_asset(
            context,
            asset_id,
            for_update=for_update,
            lock_project=True,
        )

    async def _get_override_asset_after_lock(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> McpServerRow:
        return await self._get_override_asset(
            context,
            asset_id,
            for_update=True,
            lock_project=False,
        )

    async def _get_override_asset(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool,
        lock_project: bool,
    ) -> McpServerRow:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        if lock_project:
            await self.lock_override_project(context)
        statement = select(McpServerRow).where(
            McpServerRow.id == asset_id,
            McpServerRow.scope == "project",
            McpServerRow.project_id == context.project_id,
        )
        if for_update:
            statement = statement.with_for_update(of=McpServerRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def get_system_asset(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> McpServerRow:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = select(McpServerRow).where(
            McpServerRow.id == asset_id,
            McpServerRow.scope == "system",
            McpServerRow.project_id.is_(None),
        )
        if for_update:
            statement = statement.with_for_update(of=McpServerRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def list_project_visible(self, context: ProjectContext) -> tuple[McpServerRow, ...]:
        self._require_project_actor(context)
        await self.lock_project(context)
        project_rows = (
            (
                await self.session.execute(
                    select(McpServerRow).where(
                        McpServerRow.scope == "project",
                        McpServerRow.project_id == context.project_id,
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
                    select(McpServerRow)
                    .join(
                        ProjectSystemMcpBindingRow,
                        and_(
                            ProjectSystemMcpBindingRow.system_mcp_server_id == McpServerRow.id,
                            ProjectSystemMcpBindingRow.project_id == context.project_id,
                            ProjectSystemMcpBindingRow.enabled.is_(True),
                        ),
                    )
                    .where(
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        self._project_context_exists(context),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(sorted((*project_rows, *system_rows), key=lambda row: (row.created_at, row.id)))

    async def list_override_visible(
        self,
        context: SystemAssetGovernanceContext,
    ) -> tuple[McpServerRow, ...]:
        self._require_system_actor(context)
        await self.lock_override_project(context)
        statement = (
            select(McpServerRow)
            .where(
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
            )
            .order_by(McpServerRow.created_at, McpServerRow.id)
        )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def list_system_visible(
        self,
        context: SystemAssetGovernanceContext,
    ) -> tuple[McpServerRow, ...]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetForbidden(context.request_id)
        statement = select(McpServerRow).where(McpServerRow.scope == "system", McpServerRow.project_id.is_(None)).order_by(McpServerRow.created_at, McpServerRow.id)
        return tuple((await self.session.execute(statement)).scalars().all())

    async def next_version_number(self, asset: McpServerRow) -> int:
        statement = select(func.coalesce(func.max(McpServerVersionRow.version_number), 0) + 1).where(McpServerVersionRow.mcp_server_id == asset.id)
        return int((await self.session.execute(statement)).scalar_one())

    async def add_version(
        self,
        asset: McpServerRow,
        version: McpServerVersionRow,
        slots: Sequence[McpCredentialSlotRow],
        *,
        request_id: str,
    ) -> McpVersionRecord:
        if version.mcp_server_id != asset.id or any(slot.mcp_server_version_id != version.id for slot in slots):
            raise AssetNotFound(request_id)
        self.session.add(version)
        await self.session.flush()
        self.session.add_all(slots)
        await self.session.flush()
        return McpVersionRecord(version, tuple(slots), ())

    async def get_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> McpVersionRecord:
        self._require_project_actor(context)
        statement = (
            select(McpServerVersionRow)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .where(
                McpServerVersionRow.id == version_id,
                McpServerVersionRow.mcp_server_id == asset_id,
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
                self._project_context_exists(context),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=McpServerVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return await self._record(row, for_update=for_update)

    async def get_override_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> McpVersionRecord:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(McpServerVersionRow)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .where(
                McpServerVersionRow.id == version_id,
                McpServerVersionRow.mcp_server_id == asset_id,
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=McpServerVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return await self._record(row, for_update=for_update)

    async def get_system_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> McpVersionRecord:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(McpServerVersionRow)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .where(
                McpServerVersionRow.id == version_id,
                McpServerVersionRow.mcp_server_id == asset_id,
                McpServerRow.scope == "system",
                McpServerRow.project_id.is_(None),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=McpServerVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return await self._record(row, for_update=for_update)

    async def get_project_version_history(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
    ) -> tuple[McpVersionRecord, ...]:
        await self.get_project_asset(context, asset_id)
        return await self._history(select(McpServerVersionRow).where(McpServerVersionRow.mcp_server_id == asset_id).order_by(McpServerVersionRow.version_number.desc()))

    async def get_override_version_history(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> tuple[McpVersionRecord, ...]:
        await self.get_override_asset(context, asset_id)
        return await self._history(select(McpServerVersionRow).where(McpServerVersionRow.mcp_server_id == asset_id).order_by(McpServerVersionRow.version_number.desc()))

    async def get_system_version_history(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> tuple[McpVersionRecord, ...]:
        await self.get_system_asset(context, asset_id)
        return await self._history(select(McpServerVersionRow).where(McpServerVersionRow.mcp_server_id == asset_id).order_by(McpServerVersionRow.version_number.desc()))

    async def _history(self, statement) -> tuple[McpVersionRecord, ...]:
        rows = tuple((await self.session.execute(statement)).scalars().all())
        return tuple([await self._record(row, for_update=False) for row in rows])

    async def _record(
        self,
        row: McpServerVersionRow,
        *,
        for_update: bool,
    ) -> McpVersionRecord:
        slots_statement = select(McpCredentialSlotRow).where(McpCredentialSlotRow.mcp_server_version_id == row.id).order_by(McpCredentialSlotRow.name, McpCredentialSlotRow.id)
        if for_update:
            slots_statement = slots_statement.with_for_update(of=McpCredentialSlotRow)
        slots = tuple((await self.session.execute(slots_statement)).scalars().all())
        grants_statement = select(CredentialGrantRow).where(CredentialGrantRow.mcp_server_version_id == row.id).order_by(CredentialGrantRow.created_at, CredentialGrantRow.id)
        grants = tuple((await self.session.execute(grants_statement)).scalars().all())
        return McpVersionRecord(row, slots, grants)

    async def create_grants(
        self,
        version: McpServerVersionRow,
        bindings: Sequence[tuple[McpCredentialSlotRow, CredentialVersionRow]],
        *,
        user_id: uuid.UUID,
        request_id: str,
    ) -> tuple[CredentialGrantRow, ...]:
        slot_ids = tuple(slot.id for slot, _credential_version in bindings)
        if not slot_ids:
            return ()
        # Grants are always the final lock in the approval order.
        existing_statement = (
            select(CredentialGrantRow)
            .where(
                CredentialGrantRow.mcp_server_version_id == version.id,
                CredentialGrantRow.credential_slot_id.in_(slot_ids),
                CredentialGrantRow.status == "active",
            )
            .with_for_update(of=CredentialGrantRow)
        )
        existing = tuple((await self.session.execute(existing_statement)).scalars().all())
        if existing:
            raise AssetConflict(request_id)
        grants = tuple(
            CredentialGrantRow(
                mcp_server_version_id=version.id,
                credential_slot_id=slot.id,
                credential_version_id=credential_version.id,
                created_by_user_id=str(user_id),
            )
            for slot, credential_version in bindings
        )
        self.session.add_all(grants)
        await self.session.flush()
        return grants

    async def project_grant_state(self, context: ProjectContext, grant_id: uuid.UUID) -> GrantState:
        self._require_project_actor(context)
        statement = (
            select(
                CredentialGrantRow,
                McpServerRow.status,
                McpServerVersionRow.workflow_status,
                CredentialRow.status,
                CredentialVersionRow.status,
            )
            .join(McpServerVersionRow, McpServerVersionRow.id == CredentialGrantRow.mcp_server_version_id)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .join(CredentialVersionRow, CredentialVersionRow.id == CredentialGrantRow.credential_version_id)
            .join(CredentialRow, CredentialRow.id == CredentialVersionRow.credential_id)
            .where(
                CredentialGrantRow.id == grant_id,
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
                self._project_context_exists(context),
            )
        )
        result = (await self.session.execute(statement)).one_or_none()
        if result is None:
            raise AssetNotFound(context.request_id)
        return GrantState(*result)

    async def override_grant_state(
        self,
        context: SystemAssetGovernanceContext,
        grant_id: uuid.UUID,
    ) -> GrantState:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(
                CredentialGrantRow,
                McpServerRow.status,
                McpServerVersionRow.workflow_status,
                CredentialRow.status,
                CredentialVersionRow.status,
            )
            .join(McpServerVersionRow, McpServerVersionRow.id == CredentialGrantRow.mcp_server_version_id)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .join(CredentialVersionRow, CredentialVersionRow.id == CredentialGrantRow.credential_version_id)
            .join(CredentialRow, CredentialRow.id == CredentialVersionRow.credential_id)
            .where(
                CredentialGrantRow.id == grant_id,
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
            )
        )
        result = (await self.session.execute(statement)).one_or_none()
        if result is None:
            raise AssetNotFound(context.request_id)
        return GrantState(*result)

    async def system_grant_state(
        self,
        context: SystemAssetGovernanceContext,
        grant_id: uuid.UUID,
    ) -> GrantState:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(
                CredentialGrantRow,
                McpServerRow.status,
                McpServerVersionRow.workflow_status,
                CredentialRow.status,
                CredentialVersionRow.status,
            )
            .join(McpServerVersionRow, McpServerVersionRow.id == CredentialGrantRow.mcp_server_version_id)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .join(CredentialVersionRow, CredentialVersionRow.id == CredentialGrantRow.credential_version_id)
            .join(CredentialRow, CredentialRow.id == CredentialVersionRow.credential_id)
            .where(
                CredentialGrantRow.id == grant_id,
                McpServerRow.scope == "system",
                McpServerRow.project_id.is_(None),
                CredentialRow.scope == "system",
                CredentialRow.project_id.is_(None),
            )
        )
        result = (await self.session.execute(statement)).one_or_none()
        if result is None:
            raise AssetNotFound(context.request_id)
        return GrantState(*result)
