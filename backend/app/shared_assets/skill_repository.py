from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)


class SkillCreateCommand(Protocol):
    slug: str
    display_name: str


@dataclass(frozen=True)
class SkillVersionFileMetadataRecord:
    skill_version_id: uuid.UUID
    path: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class SkillVersionRecord:
    row: SkillVersionRow
    files: tuple[SkillVersionFileRow | SkillVersionFileMetadataRecord, ...]


@dataclass(frozen=True)
class SkillVersionMetadataRecord:
    asset: SkillRow
    version: SkillVersionRow
    files: tuple[SkillVersionFileMetadataRecord, ...]


def _request_id(context: object) -> str:
    request_id = getattr(context, "request_id", None)
    return request_id if isinstance(request_id, str) else "unknown"


class SkillRepository:
    """Typed Skill persistence with trusted scope in every public lookup."""

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

    async def create_project_asset(self, context: ProjectContext, command: SkillCreateCommand) -> SkillRow:
        self._require_project_actor(context)
        await self._lock_project_context(context)
        row = SkillRow(
            scope="project",
            project_id=context.project_id,
            slug=command.slug,
            display_name=command.display_name,
            created_by_user_id=str(context.user_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_system_asset(
        self,
        context: SystemAssetGovernanceContext,
        command: SkillCreateCommand,
    ) -> SkillRow:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetForbidden(context.request_id)
        row = SkillRow(
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
        command: SkillCreateCommand,
    ) -> SkillRow:
        await self._lock_override_project(context)
        row = SkillRow(
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
    ) -> SkillRow:
        self._require_project_actor(context)
        if for_update:
            await self._lock_project_context(context)
        statement = select(SkillRow).where(
            SkillRow.id == asset_id,
            SkillRow.scope == "project",
            SkillRow.project_id == context.project_id,
            self._project_context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=SkillRow)
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
    ) -> SkillRow:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = select(SkillRow).where(
            SkillRow.id == asset_id,
            SkillRow.scope == "system",
            SkillRow.project_id.is_(None),
        )
        if for_update:
            statement = statement.with_for_update(of=SkillRow)
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
    ) -> SkillRow:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self._lock_override_project(context)
        statement = select(SkillRow).where(
            SkillRow.id == asset_id,
            SkillRow.scope == "project",
            SkillRow.project_id == context.project_id,
        )
        if for_update:
            statement = statement.with_for_update(of=SkillRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def next_project_version_number(self, context: ProjectContext, asset: SkillRow) -> int:
        self._require_project_actor(context)
        statement = (
            select(func.coalesce(func.max(SkillVersionRow.version_number), 0) + 1)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillRow.id == asset.id,
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                self._project_context_exists(context),
            )
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def next_system_version_number(
        self,
        context: SystemAssetGovernanceContext,
        asset: SkillRow,
    ) -> int:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(func.coalesce(func.max(SkillVersionRow.version_number), 0) + 1)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillRow.id == asset.id,
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
            )
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def next_override_version_number(
        self,
        context: SystemAssetGovernanceContext,
        asset: SkillRow,
    ) -> int:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self._lock_override_project(context)
        statement = (
            select(func.coalesce(func.max(SkillVersionRow.version_number), 0) + 1)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillRow.id == asset.id,
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
            )
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def create_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version: SkillVersionRow,
        files: Sequence[SkillVersionFileRow],
    ) -> SkillVersionRecord:
        self._require_project_actor(context)
        asset = await self.get_project_asset(context, asset_id, for_update=True)
        if version.skill_id != asset.id or any(file.skill_version_id != version.id for file in files):
            raise AssetNotFound(context.request_id)
        return await self._create_version(version, files)

    async def create_system_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version: SkillVersionRow,
        files: Sequence[SkillVersionFileRow],
    ) -> SkillVersionRecord:
        self._require_system_actor(context)
        asset = await self.get_system_asset(context, asset_id, for_update=True)
        if version.skill_id != asset.id or any(file.skill_version_id != version.id for file in files):
            raise AssetNotFound(context.request_id)
        return await self._create_version(version, files)

    async def create_override_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version: SkillVersionRow,
        files: Sequence[SkillVersionFileRow],
    ) -> SkillVersionRecord:
        self._require_system_actor(context)
        asset = await self.get_override_asset(context, asset_id, for_update=True)
        if version.skill_id != asset.id or any(file.skill_version_id != version.id for file in files):
            raise AssetNotFound(context.request_id)
        return await self._create_version(version, files)

    async def _create_version(
        self,
        version: SkillVersionRow,
        files: Sequence[SkillVersionFileRow],
    ) -> SkillVersionRecord:
        file_snapshot = tuple(files)
        self.session.add(version)
        await self.session.flush()
        self.session.add_all(file_snapshot)
        await self.session.flush()
        return SkillVersionRecord(version, file_snapshot)

    async def get_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillVersionRecord:
        self._require_project_actor(context)
        statement = (
            select(SkillVersionRow)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                self._project_context_exists(context),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=SkillVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return SkillVersionRecord(row, await self._load_files(row.id, for_update=for_update))

    async def get_project_visible_version_metadata(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillVersionMetadataRecord:
        """Load preview metadata without selecting file content BLOBs."""

        self._require_project_actor(context)
        await self._lock_project_context(context)
        statement = (
            select(SkillRow, SkillVersionRow)
            .join(SkillVersionRow, SkillVersionRow.skill_id == SkillRow.id)
            .where(
                SkillRow.id == asset_id,
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                or_(
                    and_(
                        SkillRow.scope == "project",
                        SkillRow.project_id == context.project_id,
                    ),
                    and_(
                        SkillRow.scope == "system",
                        SkillRow.project_id.is_(None),
                        SkillVersionRow.workflow_status == "published",
                    ),
                ),
                self._project_context_exists(context),
            )
        )
        selected = (await self.session.execute(statement)).one_or_none()
        if selected is None:
            raise AssetNotFound(context.request_id)
        asset, version = selected
        return SkillVersionMetadataRecord(
            asset=asset,
            version=version,
            files=await self._load_file_metadata(version.id),
        )

    async def load_project_visible_version_file_content(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        path: str,
    ) -> bytes:
        """Load one authorized file body after repeating the trusted scope predicate."""

        self._require_project_actor(context)
        await self._lock_project_context(context)
        statement = (
            select(SkillVersionFileRow.content)
            .join(SkillVersionRow, SkillVersionRow.id == SkillVersionFileRow.skill_version_id)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillRow.id == asset_id,
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillVersionFileRow.path == path,
                or_(
                    and_(
                        SkillRow.scope == "project",
                        SkillRow.project_id == context.project_id,
                    ),
                    and_(
                        SkillRow.scope == "system",
                        SkillRow.project_id.is_(None),
                        SkillVersionRow.workflow_status == "published",
                    ),
                ),
                self._project_context_exists(context),
            )
        )
        content = (await self.session.execute(statement)).scalar_one_or_none()
        if content is None:
            raise AssetNotFound(context.request_id)
        return bytes(content)

    async def get_system_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillVersionRecord:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(SkillVersionRow)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=SkillVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return SkillVersionRecord(row, await self._load_files(row.id, for_update=for_update))

    async def get_override_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillVersionRecord:
        self._require_system_actor(context)
        if context.project_id is None:
            raise AssetNotFound(context.request_id)
        await self._lock_override_project(context)
        statement = (
            select(SkillVersionRow)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=SkillVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return SkillVersionRecord(row, await self._load_files(row.id, for_update=for_update))

    async def load_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillVersionRecord:
        self._require_project_actor(context)
        await self._lock_project_context(context)
        project_statement = (
            select(SkillVersionRow)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillVersionRow.workflow_status == "published",
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status != "suspended",
                self._project_context_exists(context),
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        )
        row = (await self.session.execute(project_statement)).scalar_one_or_none()
        if row is None:
            system_statement = (
                select(SkillVersionRow)
                .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
                .join(
                    ProjectSystemSkillBindingRow,
                    and_(
                        ProjectSystemSkillBindingRow.system_skill_id == SkillRow.id,
                        ProjectSystemSkillBindingRow.skill_version_id == SkillVersionRow.id,
                    ),
                )
                .where(
                    SkillVersionRow.id == version_id,
                    SkillVersionRow.skill_id == asset_id,
                    SkillVersionRow.workflow_status == "published",
                    SkillRow.scope == "system",
                    SkillRow.project_id.is_(None),
                    SkillRow.status != "suspended",
                    ProjectSystemSkillBindingRow.project_id == context.project_id,
                    ProjectSystemSkillBindingRow.enabled.is_(True),
                    self._project_context_exists(context),
                )
                .with_for_update(
                    read=True,
                    of=[SkillRow, SkillVersionRow, ProjectSystemSkillBindingRow],
                )
            )
            row = (await self.session.execute(system_statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return SkillVersionRecord(row, await self._load_files(row.id))

    async def load_system_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillVersionRecord:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(SkillVersionRow)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillVersionRow.workflow_status == "published",
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                SkillRow.status != "suspended",
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return SkillVersionRecord(row, await self._load_files(row.id))

    async def load_override_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillVersionRecord:
        await self._lock_override_project(context)
        statement = (
            select(SkillVersionRow)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillVersionRow.workflow_status == "published",
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status != "suspended",
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return SkillVersionRecord(row, await self._load_files(row.id))

    async def get_project_version_history(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
    ) -> tuple[SkillVersionRecord, ...]:
        self._require_project_actor(context)
        statement = (
            select(SkillRow.id, SkillVersionRow)
            .outerjoin(
                SkillVersionRow,
                and_(
                    SkillVersionRow.skill_id == SkillRow.id,
                    or_(
                        SkillRow.scope == "project",
                        and_(
                            SkillRow.scope == "system",
                            SkillVersionRow.workflow_status == "published",
                        ),
                    ),
                ),
            )
            .where(
                SkillRow.id == asset_id,
                or_(
                    and_(
                        SkillRow.scope == "project",
                        SkillRow.project_id == context.project_id,
                    ),
                    and_(
                        SkillRow.scope == "system",
                        SkillRow.project_id.is_(None),
                    ),
                ),
                self._project_context_exists(context),
            )
            .order_by(SkillVersionRow.version_number.desc())
        )
        scoped_rows = tuple((await self.session.execute(statement)).all())
        if not scoped_rows:
            raise AssetNotFound(context.request_id)
        rows = tuple(row[1] for row in scoped_rows if row[1] is not None)
        files = await self._load_file_map(tuple(row.id for row in rows))
        return tuple(SkillVersionRecord(row, files.get(row.id, ())) for row in rows)

    async def get_system_version_history(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> tuple[SkillVersionRecord, ...]:
        self._require_system_actor(context)
        await self.get_system_asset(context, asset_id)
        statement = (
            select(SkillVersionRow)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.skill_id == asset_id,
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
            )
            .order_by(SkillVersionRow.version_number.desc())
        )
        return await self._history(statement)

    async def get_override_version_history(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
    ) -> tuple[SkillVersionRecord, ...]:
        self._require_system_actor(context)
        await self.get_override_asset(context, asset_id)
        statement = (
            select(SkillVersionRow)
            .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
            .where(
                SkillVersionRow.skill_id == asset_id,
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
            )
            .order_by(SkillVersionRow.version_number.desc())
        )
        return await self._history(statement)

    async def _history(self, statement) -> tuple[SkillVersionRecord, ...]:
        rows = tuple((await self.session.execute(statement)).scalars().all())
        files = await self._load_file_map(tuple(row.id for row in rows))
        return tuple(SkillVersionRecord(row, files.get(row.id, ())) for row in rows)

    async def _load_files(
        self,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> tuple[SkillVersionFileRow, ...]:
        statement = select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == version_id).order_by(SkillVersionFileRow.path)
        if for_update:
            statement = statement.with_for_update(of=SkillVersionFileRow)
        return tuple((await self.session.execute(statement)).scalars().all())

    async def _load_file_metadata(
        self,
        version_id: uuid.UUID,
    ) -> tuple[SkillVersionFileMetadataRecord, ...]:
        statement = (
            select(
                SkillVersionFileRow.skill_version_id,
                SkillVersionFileRow.path,
                SkillVersionFileRow.media_type,
                SkillVersionFileRow.size_bytes,
                SkillVersionFileRow.sha256,
            )
            .where(SkillVersionFileRow.skill_version_id == version_id)
            .order_by(SkillVersionFileRow.path)
        )
        return tuple(
            SkillVersionFileMetadataRecord(
                skill_version_id=row.skill_version_id,
                path=row.path,
                media_type=row.media_type,
                size_bytes=row.size_bytes,
                sha256=row.sha256,
            )
            for row in (await self.session.execute(statement)).all()
        )

    async def _load_file_map(
        self,
        version_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[SkillVersionFileMetadataRecord, ...]]:
        if not version_ids:
            return {}
        statement = (
            select(
                SkillVersionFileRow.skill_version_id,
                SkillVersionFileRow.path,
                SkillVersionFileRow.media_type,
                SkillVersionFileRow.size_bytes,
                SkillVersionFileRow.sha256,
            )
            .where(SkillVersionFileRow.skill_version_id.in_(version_ids))
            .order_by(SkillVersionFileRow.skill_version_id, SkillVersionFileRow.path)
        )
        grouped: dict[uuid.UUID, list[SkillVersionFileMetadataRecord]] = {}
        for row in (await self.session.execute(statement)).all():
            grouped.setdefault(row.skill_version_id, []).append(
                SkillVersionFileMetadataRecord(
                    skill_version_id=row.skill_version_id,
                    path=row.path,
                    media_type=row.media_type,
                    size_bytes=row.size_bytes,
                    sha256=row.sha256,
                )
            )
        return {key: tuple(value) for key, value in grouped.items()}

    async def list_project_visible(self, context: ProjectContext) -> tuple[SkillRow, ...]:
        self._require_project_actor(context)
        await self._lock_project_context(context)
        project_statement = select(SkillRow).where(
            SkillRow.scope == "project",
            SkillRow.project_id == context.project_id,
            self._project_context_exists(context),
        )
        system_statement = select(SkillRow).where(
            SkillRow.scope == "system",
            SkillRow.project_id.is_(None),
            self._project_context_exists(context),
        )
        project_rows = (await self.session.execute(project_statement)).scalars().all()
        system_rows = (await self.session.execute(system_statement)).scalars().all()
        return tuple(sorted((*project_rows, *system_rows), key=lambda row: (row.created_at, row.id)))

    async def list_system_visible(
        self,
        context: SystemAssetGovernanceContext | SystemAssetReadContext,
    ) -> tuple[SkillRow, ...]:
        self._require_system_catalog_reader(context)
        statement = (
            select(SkillRow)
            .where(
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
            )
            .order_by(SkillRow.created_at, SkillRow.id)
        )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def list_override_visible(
        self,
        context: SystemAssetGovernanceContext,
    ) -> tuple[SkillRow, ...]:
        await self._lock_override_project(context)
        statement = (
            select(SkillRow)
            .where(
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
            )
            .order_by(SkillRow.created_at, SkillRow.id)
        )
        return tuple((await self.session.execute(statement)).scalars().all())
