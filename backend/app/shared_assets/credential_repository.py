from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
)


@dataclass(frozen=True)
class LockedCredentialVersion:
    credential: CredentialRow
    version: CredentialVersionRow


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
        if row.scope != "project" or row.project_id != context.project_id:
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
        if row.scope != "project" or row.project_id != context.project_id:
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
        if context.project_id is not None or row.scope != "system" or row.project_id is not None:
            raise AssetNotFound(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

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
        if for_update:
            await self.lock_override_project(context)
        statement = select(CredentialRow).where(
            CredentialRow.id == credential_id,
            CredentialRow.scope == "project",
            CredentialRow.project_id == context.project_id,
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
        )
        if for_update:
            statement = statement.with_for_update(of=CredentialRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

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
            )
            .with_for_update(of=CredentialRow)
        )
        credential = (await self.session.execute(credential_statement)).scalar_one_or_none()
        if credential is None:
            raise AssetNotFound(context.request_id)
        version = await self._lock_version(credential, credential_version_id, context.request_id)
        return LockedCredentialVersion(credential, version)

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
