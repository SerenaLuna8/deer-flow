from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectSkillCredentialBindingRow,
    ProjectSkillCredentialConfigRow,
)


@dataclass(frozen=True)
class LockedCredentialVersion:
    credential: CredentialRow
    version: CredentialVersionRow


@dataclass(frozen=True)
class ActiveCredentialGrant:
    grant: CredentialGrantRow
    slot: McpCredentialSlotRow
    mcp_server: McpServerRow


@dataclass(frozen=True)
class ActiveSkillCredentialBinding:
    binding: ProjectSkillCredentialBindingRow
    config: ProjectSkillCredentialConfigRow


def _request_id(context: object) -> str:
    value = getattr(context, "request_id", None)
    return value if isinstance(value, str) else "unknown"


class CredentialRepository:
    """Credential persistence whose public operations always carry trusted scope."""

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

    async def create_project_credential(
        self,
        context: ProjectContext,
        row: CredentialRow,
    ) -> CredentialRow:
        await self.lock_project(context)
        if row.scope != "project" or row.project_id != context.project_id or row.is_delete:
            raise AssetNotFound(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_override_credential(
        self,
        context: SystemAssetGovernanceContext,
        row: CredentialRow,
    ) -> CredentialRow:
        await self.lock_override_project(context)
        if row.scope != "project" or row.project_id != context.project_id or row.is_delete:
            raise AssetNotFound(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_system_credential(
        self,
        context: SystemAssetGovernanceContext,
        row: CredentialRow,
    ) -> CredentialRow:
        self._require_system_actor(context)
        if context.project_id is not None or row.scope != "system" or row.project_id is not None or row.is_delete:
            raise AssetNotFound(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def mark_deleted(
        self,
        credential: CredentialRow,
        *,
        request_id: str,
    ) -> CredentialRow:
        if credential.is_delete:
            raise AssetNotFound(request_id)
        credential.is_delete = True
        credential.version += 1
        await self.session.flush()
        return credential

    async def get_project_credential(
        self,
        context: ProjectContext,
        credential_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> CredentialRow:
        self._require_project_actor(context)
        if for_update:
            await self.lock_project(context)
        statement = select(CredentialRow).where(
            CredentialRow.id == credential_id,
            CredentialRow.scope == "project",
            CredentialRow.project_id == context.project_id,
            CredentialRow.is_delete.is_(False),
            self._project_context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=CredentialRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def get_override_credential(
        self,
        context: SystemAssetGovernanceContext,
        credential_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> CredentialRow:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self.lock_override_project(context)
        statement = select(CredentialRow).where(
            CredentialRow.id == credential_id,
            CredentialRow.scope == "project",
            CredentialRow.project_id == context.project_id,
            CredentialRow.is_delete.is_(False),
        )
        if for_update:
            statement = statement.with_for_update(of=CredentialRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def get_system_credential(
        self,
        context: SystemAssetGovernanceContext,
        credential_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> CredentialRow:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = select(CredentialRow).where(
            CredentialRow.id == credential_id,
            CredentialRow.scope == "system",
            CredentialRow.project_id.is_(None),
            CredentialRow.is_delete.is_(False),
        )
        if for_update:
            statement = statement.with_for_update(of=CredentialRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def list_project_visible(self, context: ProjectContext) -> tuple[CredentialRow, ...]:
        self._require_project_actor(context)
        await self.lock_project(context)
        project_statement = (
            select(CredentialRow)
            .where(
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
                CredentialRow.is_delete.is_(False),
                self._project_context_exists(context),
            )
            .order_by(CredentialRow.created_at, CredentialRow.id)
        )
        system_statement = select(CredentialRow).where(
            CredentialRow.scope == "system",
            CredentialRow.project_id.is_(None),
            CredentialRow.is_delete.is_(False),
            self._project_context_exists(context),
        )
        project_rows = (await self.session.execute(project_statement)).scalars().all()
        system_rows = (await self.session.execute(system_statement)).scalars().all()
        return tuple(sorted((*project_rows, *system_rows), key=lambda row: (row.created_at, row.id)))

    async def list_override_visible(
        self,
        context: SystemAssetGovernanceContext,
    ) -> tuple[CredentialRow, ...]:
        self._require_system_actor(context)
        await self.lock_override_project(context)
        statement = (
            select(CredentialRow)
            .where(
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
                CredentialRow.is_delete.is_(False),
            )
            .order_by(CredentialRow.created_at, CredentialRow.id)
        )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def list_system_visible(
        self,
        context: SystemAssetGovernanceContext,
    ) -> tuple[CredentialRow, ...]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetForbidden(context.request_id)
        statement = (
            select(CredentialRow)
            .where(
                CredentialRow.scope == "system",
                CredentialRow.project_id.is_(None),
                CredentialRow.is_delete.is_(False),
            )
            .order_by(CredentialRow.created_at, CredentialRow.id)
        )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def rotation_status(
        self,
        context: SystemAssetGovernanceContext,
        *,
        active_key_id: str,
    ) -> tuple[int, int]:
        """Return eligible and already-current envelope counts without key metadata."""

        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetForbidden(context.request_id)
        eligible = func.count(CredentialEnvelopeRow.id)
        current = func.count(CredentialEnvelopeRow.id).filter(CredentialEnvelopeRow.key_id == active_key_id)
        statement = (
            select(eligible, current)
            .select_from(CredentialVersionRow)
            .join(
                CredentialRow,
                CredentialRow.id == CredentialVersionRow.credential_id,
            )
            .join(
                CredentialEnvelopeRow,
                (CredentialEnvelopeRow.credential_version_id == CredentialVersionRow.id) & CredentialEnvelopeRow.is_active.is_(True),
            )
            .where(
                CredentialVersionRow.status != "revoked",
                CredentialRow.status == "active",
                CredentialRow.is_delete.is_(False),
            )
        )
        eligible_total, current_total = (await self.session.execute(statement)).one()
        return int(eligible_total), int(current_total)

    async def get_project_version_history(
        self,
        context: ProjectContext,
        credential_id: uuid.UUID,
    ) -> tuple[CredentialVersionRow, ...]:
        self._require_project_actor(context)
        statement = (
            select(CredentialRow.id, CredentialVersionRow)
            .outerjoin(
                CredentialVersionRow,
                CredentialVersionRow.credential_id == CredentialRow.id,
            )
            .where(
                CredentialRow.id == credential_id,
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
                CredentialRow.is_delete.is_(False),
                self._project_context_exists(context),
            )
            .order_by(CredentialVersionRow.version_number.desc())
        )
        return await self._version_history(statement, context.request_id)

    async def get_override_version_history(
        self,
        context: SystemAssetGovernanceContext,
        credential_id: uuid.UUID,
    ) -> tuple[CredentialVersionRow, ...]:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(CredentialRow.id, CredentialVersionRow)
            .outerjoin(
                CredentialVersionRow,
                CredentialVersionRow.credential_id == CredentialRow.id,
            )
            .where(
                CredentialRow.id == credential_id,
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
                CredentialRow.is_delete.is_(False),
                exists(
                    select(1).where(
                        ProjectRow.id == context.project_id,
                        ProjectRow.status == "active",
                        ProjectRow.is_suspended.is_(False),
                    )
                ),
            )
            .order_by(CredentialVersionRow.version_number.desc())
        )
        return await self._version_history(statement, context.request_id)

    async def get_system_version_history(
        self,
        context: SystemAssetGovernanceContext,
        credential_id: uuid.UUID,
    ) -> tuple[CredentialVersionRow, ...]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(CredentialRow.id, CredentialVersionRow)
            .outerjoin(
                CredentialVersionRow,
                CredentialVersionRow.credential_id == CredentialRow.id,
            )
            .where(
                CredentialRow.id == credential_id,
                CredentialRow.scope == "system",
                CredentialRow.project_id.is_(None),
                CredentialRow.is_delete.is_(False),
            )
            .order_by(CredentialVersionRow.version_number.desc())
        )
        return await self._version_history(statement, context.request_id)

    async def _version_history(
        self,
        statement,
        request_id: str,
    ) -> tuple[CredentialVersionRow, ...]:
        scoped_rows = tuple((await self.session.execute(statement)).all())
        if not scoped_rows:
            raise AssetNotFound(request_id)
        return tuple(row[1] for row in scoped_rows if row[1] is not None)

    async def lock_project_credential_version(
        self,
        context: ProjectContext,
        credential_version_id: uuid.UUID,
    ) -> LockedCredentialVersion:
        self._require_project_actor(context)
        credential_statement = (
            select(CredentialRow)
            .join(CredentialVersionRow, CredentialVersionRow.credential_id == CredentialRow.id)
            .where(
                CredentialVersionRow.id == credential_version_id,
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
                CredentialRow.is_delete.is_(False),
                self._project_context_exists(context),
            )
            .with_for_update(of=CredentialRow)
        )
        credential = (await self.session.execute(credential_statement)).scalar_one_or_none()
        if credential is None:
            raise AssetNotFound(context.request_id)
        version = await self._lock_version(credential, credential_version_id, context.request_id)
        return LockedCredentialVersion(credential, version)

    async def lock_override_credential_version(
        self,
        context: SystemAssetGovernanceContext,
        credential_version_id: uuid.UUID,
    ) -> LockedCredentialVersion:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        credential_statement = (
            select(CredentialRow)
            .join(CredentialVersionRow, CredentialVersionRow.credential_id == CredentialRow.id)
            .where(
                CredentialVersionRow.id == credential_version_id,
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
                CredentialRow.is_delete.is_(False),
            )
            .with_for_update(of=CredentialRow)
        )
        credential = (await self.session.execute(credential_statement)).scalar_one_or_none()
        if credential is None:
            raise AssetNotFound(context.request_id)
        version = await self._lock_version(credential, credential_version_id, context.request_id)
        return LockedCredentialVersion(credential, version)

    async def lock_system_credential_version(
        self,
        context: SystemAssetGovernanceContext,
        credential_version_id: uuid.UUID,
    ) -> LockedCredentialVersion:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        credential_statement = (
            select(CredentialRow)
            .join(CredentialVersionRow, CredentialVersionRow.credential_id == CredentialRow.id)
            .where(
                CredentialVersionRow.id == credential_version_id,
                CredentialRow.scope == "system",
                CredentialRow.project_id.is_(None),
                CredentialRow.is_delete.is_(False),
            )
            .with_for_update(of=CredentialRow)
        )
        credential = (await self.session.execute(credential_statement)).scalar_one_or_none()
        if credential is None:
            raise AssetNotFound(context.request_id)
        version = await self._lock_version(credential, credential_version_id, context.request_id)
        return LockedCredentialVersion(credential, version)

    async def lock_project_credential_versions(
        self,
        context: ProjectContext,
        credential_version_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, LockedCredentialVersion]:
        self._require_project_actor(context)
        await self.lock_project(context)
        return await self._lock_credential_versions_globally(
            credential_version_ids,
            scope="project",
            project_id=context.project_id,
            context_filter=self._project_context_exists(context),
            request_id=context.request_id,
        )

    async def lock_override_credential_versions(
        self,
        context: SystemAssetGovernanceContext,
        credential_version_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, LockedCredentialVersion]:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self.lock_override_project(context)
        return await self._lock_credential_versions_globally(
            credential_version_ids,
            scope="project",
            project_id=context.project_id,
            context_filter=None,
            request_id=context.request_id,
        )

    async def lock_system_credential_versions(
        self,
        context: SystemAssetGovernanceContext,
        credential_version_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, LockedCredentialVersion]:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        return await self._lock_credential_versions_globally(
            credential_version_ids,
            scope="system",
            project_id=None,
            context_filter=None,
            request_id=context.request_id,
        )

    async def _lock_credential_versions_globally(
        self,
        credential_version_ids: Sequence[uuid.UUID],
        *,
        scope: str,
        project_id: uuid.UUID | None,
        context_filter: ColumnElement[bool] | None,
        request_id: str,
    ) -> dict[uuid.UUID, LockedCredentialVersion]:
        version_ids = tuple(
            sorted(
                {uuid.UUID(str(version_id)) for version_id in credential_version_ids},
                key=lambda value: value.int,
            )
        )
        if not version_ids:
            return {}
        scope_filters = [
            CredentialRow.scope == scope,
            CredentialRow.is_delete.is_(False),
        ]
        if project_id is None:
            scope_filters.append(CredentialRow.project_id.is_(None))
        else:
            scope_filters.append(CredentialRow.project_id == project_id)
        if context_filter is not None:
            scope_filters.append(context_filter)

        reference_statement = (
            select(CredentialVersionRow.id, CredentialVersionRow.credential_id)
            .join(CredentialRow, CredentialRow.id == CredentialVersionRow.credential_id)
            .where(
                CredentialVersionRow.id.in_(version_ids),
                *scope_filters,
            )
        )
        references = {uuid.UUID(str(version_id)): uuid.UUID(str(credential_id)) for version_id, credential_id in (await self.session.execute(reference_statement)).all()}
        if set(references) != set(version_ids):
            raise AssetNotFound(request_id)

        credentials: dict[uuid.UUID, CredentialRow] = {}
        for credential_id in sorted(set(references.values()), key=lambda value: value.int):
            credential_statement = (
                select(CredentialRow)
                .where(
                    CredentialRow.id == credential_id,
                    *scope_filters,
                )
                .with_for_update(of=CredentialRow)
            )
            credential = (await self.session.execute(credential_statement)).scalar_one_or_none()
            if credential is None:
                raise AssetNotFound(request_id)
            credentials[credential_id] = credential

        locked: dict[uuid.UUID, LockedCredentialVersion] = {}
        ordered_versions = sorted(
            references.items(),
            key=lambda item: (item[1].int, item[0].int),
        )
        for version_id, credential_id in ordered_versions:
            version_statement = (
                select(CredentialVersionRow)
                .where(
                    CredentialVersionRow.id == version_id,
                    CredentialVersionRow.credential_id == credential_id,
                )
                .with_for_update(of=CredentialVersionRow)
            )
            version = (await self.session.execute(version_statement)).scalar_one_or_none()
            if version is None:
                raise AssetNotFound(request_id)
            locked[version_id] = LockedCredentialVersion(
                credentials[credential_id],
                version,
            )
        return locked

    async def _lock_version(
        self,
        credential: CredentialRow,
        credential_version_id: uuid.UUID,
        request_id: str,
    ) -> CredentialVersionRow:
        statement = (
            select(CredentialVersionRow)
            .where(
                CredentialVersionRow.id == credential_version_id,
                CredentialVersionRow.credential_id == credential.id,
            )
            .with_for_update(of=CredentialVersionRow)
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(request_id)
        return row

    async def lock_current_version(
        self,
        credential: CredentialRow,
        *,
        request_id: str,
    ) -> CredentialVersionRow:
        if credential.current_version_id is None:
            raise AssetNotFound(request_id)
        return await self._lock_version(credential, credential.current_version_id, request_id)

    async def next_version_number(self, credential: CredentialRow) -> int:
        statement = select(func.coalesce(func.max(CredentialVersionRow.version_number), 0) + 1).where(CredentialVersionRow.credential_id == credential.id)
        return int((await self.session.execute(statement)).scalar_one())

    async def add_version(
        self,
        credential: CredentialRow,
        version: CredentialVersionRow,
        envelope: CredentialEnvelopeRow,
        *,
        request_id: str,
    ) -> CredentialVersionRow:
        if version.credential_id != credential.id or envelope.credential_version_id != version.id:
            raise AssetNotFound(request_id)
        self.session.add(version)
        await self.session.flush()
        self.session.add(envelope)
        await self.session.flush()
        return version

    async def lock_all_versions(
        self,
        credential: CredentialRow,
    ) -> tuple[CredentialVersionRow, ...]:
        statement = select(CredentialVersionRow).where(CredentialVersionRow.credential_id == credential.id).order_by(CredentialVersionRow.version_number).with_for_update(of=CredentialVersionRow)
        return tuple((await self.session.execute(statement)).scalars().all())

    async def lock_active_grants(
        self,
        credential: CredentialRow,
    ) -> tuple[ActiveCredentialGrant, ...]:
        statement = (
            select(CredentialGrantRow, McpCredentialSlotRow, McpServerRow)
            .join(
                CredentialVersionRow,
                CredentialVersionRow.id == CredentialGrantRow.credential_version_id,
            )
            .join(
                McpCredentialSlotRow,
                (McpCredentialSlotRow.id == CredentialGrantRow.credential_slot_id) & (McpCredentialSlotRow.mcp_server_version_id == CredentialGrantRow.mcp_server_version_id),
            )
            .join(
                McpServerVersionRow,
                McpServerVersionRow.id == CredentialGrantRow.mcp_server_version_id,
            )
            .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
            .where(
                CredentialVersionRow.credential_id == credential.id,
                CredentialGrantRow.status == "active",
            )
            .order_by(CredentialGrantRow.id)
            .with_for_update(of=CredentialGrantRow)
        )
        return tuple(ActiveCredentialGrant(grant, slot, mcp_server) for grant, slot, mcp_server in (await self.session.execute(statement)).all())

    async def lock_active_skill_bindings(
        self,
        credential: CredentialRow,
    ) -> tuple[ActiveSkillCredentialBinding, ...]:
        references = tuple(
            (
                await self.session.execute(
                    select(
                        ProjectSkillCredentialBindingRow.project_id,
                        ProjectSkillCredentialBindingRow.skill_id,
                        ProjectSkillCredentialBindingRow.skill_version_id,
                    )
                    .where(
                        ProjectSkillCredentialBindingRow.credential_id == credential.id,
                        ProjectSkillCredentialBindingRow.status == "active",
                    )
                    .order_by(
                        ProjectSkillCredentialBindingRow.project_id,
                        ProjectSkillCredentialBindingRow.skill_id,
                        ProjectSkillCredentialBindingRow.skill_version_id,
                    )
                )
            ).all()
        )
        config_keys = sorted(
            {
                (
                    uuid.UUID(str(reference.project_id)),
                    uuid.UUID(str(reference.skill_id)),
                    uuid.UUID(str(reference.skill_version_id)),
                )
                for reference in references
            },
            key=lambda item: (item[0].int, item[1].int, item[2].int),
        )
        configs: dict[
            tuple[uuid.UUID, uuid.UUID, uuid.UUID],
            ProjectSkillCredentialConfigRow,
        ] = {}
        for project_id, skill_id, skill_version_id in config_keys:
            config = (
                await self.session.execute(
                    select(ProjectSkillCredentialConfigRow)
                    .where(
                        ProjectSkillCredentialConfigRow.project_id == project_id,
                        ProjectSkillCredentialConfigRow.skill_id == skill_id,
                        ProjectSkillCredentialConfigRow.skill_version_id == skill_version_id,
                    )
                    .with_for_update(of=ProjectSkillCredentialConfigRow)
                )
            ).scalar_one_or_none()
            if config is None:
                raise AssetNotFound("unknown")
            configs[(project_id, skill_id, skill_version_id)] = config
        result: list[ActiveSkillCredentialBinding] = []
        for key in config_keys:
            project_id, skill_id, skill_version_id = key
            bindings = tuple(
                (
                    await self.session.execute(
                        select(ProjectSkillCredentialBindingRow)
                        .where(
                            ProjectSkillCredentialBindingRow.project_id == project_id,
                            ProjectSkillCredentialBindingRow.skill_id == skill_id,
                            ProjectSkillCredentialBindingRow.skill_version_id == skill_version_id,
                            ProjectSkillCredentialBindingRow.status == "active",
                        )
                        .order_by(
                            ProjectSkillCredentialBindingRow.secret_name,
                            ProjectSkillCredentialBindingRow.id,
                        )
                        .with_for_update(of=ProjectSkillCredentialBindingRow)
                    )
                )
                .scalars()
                .all()
            )
            config = configs[key]
            if not any(binding.credential_id == credential.id for binding in bindings):
                continue
            if any(binding.config_revision != config.revision or binding.skill_version_id != config.skill_version_id for binding in bindings):
                raise AssetNotFound("unknown")
            result.extend(ActiveSkillCredentialBinding(binding, config) for binding in bindings)
        return tuple(result)

    async def revoke_grants(
        self,
        grants: Sequence[CredentialGrantRow],
        *,
        user_id: uuid.UUID,
        revoked_at: datetime,
    ) -> None:
        for grant in grants:
            if grant.status != "active":
                continue
            grant.status = "revoked"
            grant.version += 1
            grant.revoked_at = revoked_at
            grant.revoked_by_user_id = str(user_id)
        await self.session.flush()

    async def revoke_skill_bindings(
        self,
        bindings: Sequence[ActiveSkillCredentialBinding],
        *,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
        revoked_at: datetime,
    ) -> None:
        grouped: dict[
            tuple[uuid.UUID, uuid.UUID, uuid.UUID],
            list[ActiveSkillCredentialBinding],
        ] = {}
        for item in bindings:
            key = (
                uuid.UUID(str(item.config.project_id)),
                uuid.UUID(str(item.config.skill_id)),
                uuid.UUID(str(item.config.skill_version_id)),
            )
            grouped.setdefault(key, []).append(item)

        created: list[ProjectSkillCredentialBindingRow] = []
        for key in sorted(
            grouped,
            key=lambda item: (item[0].int, item[1].int, item[2].int),
        ):
            items = grouped[key]
            if not any(item.binding.credential_id == credential_id for item in items):
                continue
            config = items[0].config
            config.revision += 1
            config.updated_by_user_id = str(user_id)
            for item in items:
                binding = item.binding
                if binding.status != "active":
                    continue
                binding.status = "revoked"
                binding.revoked_at = revoked_at
                binding.revoked_by_user_id = str(user_id)
                if binding.credential_id != credential_id:
                    created.append(
                        ProjectSkillCredentialBindingRow(
                            project_id=binding.project_id,
                            skill_id=binding.skill_id,
                            skill_version_id=binding.skill_version_id,
                            secret_name=binding.secret_name,
                            credential_id=binding.credential_id,
                            credential_version_id=binding.credential_version_id,
                            config_revision=config.revision,
                            created_by_user_id=str(user_id),
                        )
                    )
        await self.session.flush()
        self.session.add_all(created)
        await self.session.flush()

    async def migrate_skill_bindings(
        self,
        bindings: Sequence[ActiveSkillCredentialBinding],
        target_version: CredentialVersionRow,
        *,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
        migrated_at: datetime,
    ) -> tuple[ProjectSkillCredentialBindingRow, ...]:
        grouped: dict[
            tuple[uuid.UUID, uuid.UUID, uuid.UUID],
            list[ActiveSkillCredentialBinding],
        ] = {}
        for item in bindings:
            key = (
                uuid.UUID(str(item.config.project_id)),
                uuid.UUID(str(item.config.skill_id)),
                uuid.UUID(str(item.config.skill_version_id)),
            )
            grouped.setdefault(key, []).append(item)

        created: list[ProjectSkillCredentialBindingRow] = []
        for key in sorted(
            grouped,
            key=lambda item: (item[0].int, item[1].int, item[2].int),
        ):
            items = grouped[key]
            if not any(item.binding.credential_id == credential_id and item.binding.credential_version_id != target_version.id for item in items):
                continue
            config = items[0].config
            config.revision += 1
            config.updated_by_user_id = str(user_id)
            for item in items:
                binding = item.binding
                binding.status = "revoked"
                binding.revoked_at = migrated_at
                binding.revoked_by_user_id = str(user_id)
                created.append(
                    ProjectSkillCredentialBindingRow(
                        project_id=binding.project_id,
                        skill_id=binding.skill_id,
                        skill_version_id=binding.skill_version_id,
                        secret_name=binding.secret_name,
                        credential_id=binding.credential_id,
                        credential_version_id=(target_version.id if binding.credential_id == credential_id else binding.credential_version_id),
                        config_revision=config.revision,
                        created_by_user_id=str(user_id),
                    )
                )
        await self.session.flush()
        self.session.add_all(created)
        await self.session.flush()
        return tuple(created)

    async def migrate_grants(
        self,
        grants: Sequence[ActiveCredentialGrant],
        target_version: CredentialVersionRow,
        *,
        user_id: uuid.UUID,
        migrated_at: datetime,
    ) -> tuple[CredentialGrantRow, ...]:
        await self.revoke_grants(
            tuple(item.grant for item in grants),
            user_id=user_id,
            revoked_at=migrated_at,
        )
        created = tuple(
            CredentialGrantRow(
                mcp_server_version_id=item.grant.mcp_server_version_id,
                credential_slot_id=item.grant.credential_slot_id,
                credential_version_id=target_version.id,
                created_by_user_id=str(user_id),
            )
            for item in grants
        )
        self.session.add_all(created)
        await self.session.flush()
        return created
