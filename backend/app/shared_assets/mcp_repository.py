from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound
from deerflow.persistence.private_work.model import (
    RunAssetVersionRow,
    RunMcpGrantSnapshotRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentVersionMcpRefRow,
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

    async def plan_project_asset_deletion(
        self,
        context: ProjectContext,
        asset: McpServerRow,
    ) -> tuple[uuid.UUID, ...]:
        """Lock one project MCP package and reject every retained external use."""

        self._require_project_actor(context)
        if asset.scope != "project" or asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        version_ids = tuple(
            (
                await self.session.execute(
                    select(McpServerVersionRow.id)
                    .where(McpServerVersionRow.mcp_server_id == asset.id)
                    .order_by(
                        McpServerVersionRow.version_number,
                        McpServerVersionRow.id,
                    )
                    .with_for_update(of=McpServerVersionRow)
                )
            )
            .scalars()
            .all()
        )
        _locked_slot_ids = tuple(
            (
                await self.session.execute(
                    select(McpCredentialSlotRow.id)
                    .where(
                        McpCredentialSlotRow.mcp_server_version_id.in_(version_ids),
                    )
                    .order_by(McpCredentialSlotRow.id)
                    .with_for_update(of=McpCredentialSlotRow)
                )
            )
            .scalars()
            .all()
        )
        _locked_grant_ids = tuple(
            (
                await self.session.execute(
                    select(CredentialGrantRow.id)
                    .where(
                        CredentialGrantRow.mcp_server_version_id.in_(version_ids),
                    )
                    .order_by(CredentialGrantRow.id)
                    .with_for_update(of=CredentialGrantRow)
                )
            )
            .scalars()
            .all()
        )
        retained_reference_exists = bool(
            await self.session.scalar(
                select(
                    or_(
                        exists().where(
                            AgentVersionMcpRefRow.mcp_server_version_id.in_(
                                version_ids,
                            ),
                        ),
                        exists().where(
                            RunAssetVersionRow.project_id == context.project_id,
                            RunAssetVersionRow.asset_kind == "mcp",
                            RunAssetVersionRow.asset_scope == "project",
                            RunAssetVersionRow.asset_id == asset.id,
                        ),
                        exists().where(
                            RunMcpGrantSnapshotRow.mcp_version_id.in_(version_ids),
                        ),
                        exists().where(
                            or_(
                                ProjectSystemMcpBindingRow.system_mcp_server_id == asset.id,
                                ProjectSystemMcpBindingRow.mcp_server_version_id.in_(
                                    version_ids,
                                ),
                            ),
                        ),
                    )
                )
            )
        )
        if retained_reference_exists:
            raise AssetConflict(context.request_id)
        # Keep both lock queries above even when the identifiers are otherwise
        # unused: they close FK insertion races before the reference check.
        return version_ids

    async def delete_project_asset(
        self,
        context: ProjectContext,
        asset: McpServerRow,
        version_ids: Sequence[uuid.UUID],
    ) -> None:
        """Physically remove one already-locked, externally unreferenced MCP."""

        self._require_project_actor(context)
        selected_version_ids = tuple(version_ids)
        if asset.scope != "project" or asset.project_id != context.project_id or len(set(selected_version_ids)) != len(selected_version_ids):
            raise AssetNotFound(context.request_id)

        # This transient state is never committed. The transaction-local exact
        # asset id authorizes deleting immutable slots for this package only.
        asset.current_published_version_id = None
        asset.status = "archived"
        await self.session.flush()
        await self.session.scalar(
            select(
                func.set_config(
                    "deerflow.mcp_hard_delete_asset_id",
                    str(asset.id),
                    True,
                )
            )
        )

        if selected_version_ids:
            await self.session.execute(
                delete(CredentialGrantRow).where(
                    CredentialGrantRow.mcp_server_version_id.in_(
                        selected_version_ids,
                    )
                )
            )
            await self.session.execute(
                delete(McpCredentialSlotRow).where(
                    McpCredentialSlotRow.mcp_server_version_id.in_(
                        selected_version_ids,
                    )
                )
            )
            child = aliased(McpServerVersionRow)
            remaining = set(selected_version_ids)
            while remaining:
                deleted_ids = set(
                    (
                        await self.session.execute(
                            delete(McpServerVersionRow)
                            .where(
                                McpServerVersionRow.id.in_(remaining),
                                McpServerVersionRow.mcp_server_id == asset.id,
                                ~exists().where(
                                    child.supersedes_version_id == McpServerVersionRow.id,
                                ),
                            )
                            .returning(McpServerVersionRow.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not deleted_ids:
                    raise AssetConflict(context.request_id)
                remaining.difference_update(deleted_ids)

        await self.session.delete(asset)
        await self.session.flush()

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
                    select(McpServerRow).where(
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        McpServerRow.status == "active",
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
        context: SystemAssetGovernanceContext | SystemAssetReadContext,
    ) -> tuple[McpServerRow, ...]:
        self._require_system_catalog_reader(context)
        statement = (
            select(McpServerRow)
            .where(
                McpServerRow.scope == "system",
                McpServerRow.project_id.is_(None),
                McpServerRow.status == "active",
            )
            .order_by(McpServerRow.created_at, McpServerRow.id)
        )
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

    async def get_project_current_configuration(
        self,
        context: ProjectContext,
        asset: McpServerRow,
        *,
        for_update: bool = False,
    ) -> McpVersionRecord | None:
        """Return the editable head without exposing arbitrary history.

        A current-lineage pending revision is the configuration the editor
        must resume. Otherwise the exact published pointer is returned.
        Callers that request a lock must already hold the project/asset lock.
        """

        self._require_project_actor(context)
        if asset.scope != "project" or asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        pending_lineage = McpServerVersionRow.supersedes_version_id.is_(None) if asset.current_published_version_id is None else McpServerVersionRow.supersedes_version_id == asset.current_published_version_id
        eligible = [
            and_(
                McpServerVersionRow.workflow_status == "pending_approval",
                pending_lineage,
            )
        ]
        if asset.current_published_version_id is not None:
            eligible.append(
                and_(
                    McpServerVersionRow.id == asset.current_published_version_id,
                    McpServerVersionRow.workflow_status == "published",
                )
            )
        statement = (
            select(McpServerVersionRow)
            .where(
                McpServerVersionRow.mcp_server_id == asset.id,
                or_(*eligible),
            )
            .order_by(
                (McpServerVersionRow.workflow_status == "pending_approval").desc(),
                McpServerVersionRow.version_number.desc(),
                McpServerVersionRow.id,
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update(of=McpServerVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None
        return await self._record(row, for_update=for_update)

    async def get_project_visible_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> McpVersionRecord:
        """Load one exact project-visible version without scanning history."""

        self._require_project_actor(context)
        statement = (
            select(McpServerVersionRow)
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .where(
                McpServerVersionRow.id == version_id,
                McpServerVersionRow.mcp_server_id == asset_id,
                or_(
                    and_(
                        McpServerRow.scope == "project",
                        McpServerRow.project_id == context.project_id,
                    ),
                    and_(
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        McpServerVersionRow.workflow_status == "published",
                    ),
                ),
                self._project_context_exists(context),
            )
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return await self._record(row, for_update=False)

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
        self._require_project_actor(context)
        statement = (
            select(McpServerRow.id, McpServerVersionRow)
            .outerjoin(
                McpServerVersionRow,
                and_(
                    McpServerVersionRow.mcp_server_id == McpServerRow.id,
                    or_(
                        McpServerRow.scope == "project",
                        and_(
                            McpServerRow.scope == "system",
                            McpServerVersionRow.workflow_status == "published",
                        ),
                    ),
                ),
            )
            .where(
                McpServerRow.id == asset_id,
                or_(
                    and_(
                        McpServerRow.scope == "project",
                        McpServerRow.project_id == context.project_id,
                    ),
                    and_(
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                    ),
                ),
                self._project_context_exists(context),
            )
            .order_by(McpServerVersionRow.version_number.desc())
        )
        return await self._history(statement, context.request_id)

    async def get_override_version_history(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> tuple[McpVersionRecord, ...]:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(McpServerRow.id, McpServerVersionRow)
            .outerjoin(
                McpServerVersionRow,
                McpServerVersionRow.mcp_server_id == McpServerRow.id,
            )
            .where(
                McpServerRow.id == asset_id,
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
                exists(
                    select(1).where(
                        ProjectRow.id == context.project_id,
                        ProjectRow.status == "active",
                        ProjectRow.is_suspended.is_(False),
                    )
                ),
            )
            .order_by(McpServerVersionRow.version_number.desc())
        )
        return await self._history(statement, context.request_id)

    async def get_system_version_history(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> tuple[McpVersionRecord, ...]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(McpServerRow.id, McpServerVersionRow)
            .outerjoin(
                McpServerVersionRow,
                McpServerVersionRow.mcp_server_id == McpServerRow.id,
            )
            .where(
                McpServerRow.id == asset_id,
                McpServerRow.scope == "system",
                McpServerRow.project_id.is_(None),
            )
            .order_by(McpServerVersionRow.version_number.desc())
        )
        return await self._history(statement, context.request_id)

    async def _history(
        self,
        statement,
        request_id: str,
    ) -> tuple[McpVersionRecord, ...]:
        scoped_rows = tuple((await self.session.execute(statement)).all())
        if not scoped_rows:
            raise AssetNotFound(request_id)
        rows = tuple(row[1] for row in scoped_rows if row[1] is not None)
        records = []
        for row in rows:
            records.append(await self._record(row, for_update=False))
        return tuple(records)

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

    async def replace_system_grants(
        self,
        version: McpServerVersionRow,
        slots: Sequence[McpCredentialSlotRow],
        bindings: Sequence[tuple[McpCredentialSlotRow, CredentialVersionRow]],
        *,
        expected_active_grant_versions: Mapping[str, int],
        user_id: uuid.UUID,
        request_id: str,
    ) -> tuple[CredentialGrantRow, ...]:
        slot_names = {slot.id: slot.name for slot in slots}
        existing_statement = (
            select(CredentialGrantRow)
            .where(
                CredentialGrantRow.mcp_server_version_id == version.id,
                CredentialGrantRow.status == "active",
            )
            .order_by(CredentialGrantRow.credential_slot_id)
            .with_for_update(of=CredentialGrantRow)
        )
        existing = tuple((await self.session.execute(existing_statement)).scalars().all())
        if any(grant.credential_slot_id not in slot_names for grant in existing):
            raise AssetConflict(request_id)
        actual_versions = {slot_names[grant.credential_slot_id]: grant.version for grant in existing}
        if dict(expected_active_grant_versions) != actual_versions:
            raise AssetConflict(request_id)

        desired_versions = {slot.id: credential_version.id for slot, credential_version in bindings}
        if len(desired_versions) != len(bindings):
            raise AssetConflict(request_id)
        if len(existing) == len(desired_versions) and all(desired_versions.get(grant.credential_slot_id) == grant.credential_version_id for grant in existing):
            return existing

        now = datetime.now(UTC)
        for grant in existing:
            grant.status = "revoked"
            grant.version += 1
            grant.revoked_at = now
            grant.revoked_by_user_id = str(user_id)
        await self.session.flush()
        return await self.create_grants(
            version,
            bindings,
            user_id=user_id,
            request_id=request_id,
        )

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
                CredentialRow.is_delete.is_(False),
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
                CredentialRow.is_delete.is_(False),
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
                CredentialRow.is_delete.is_(False),
            )
        )
        result = (await self.session.execute(statement)).one_or_none()
        if result is None:
            raise AssetNotFound(context.request_id)
        return GrantState(*result)
