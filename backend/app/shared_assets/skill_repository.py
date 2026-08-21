from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetInUse,
    AssetNotFound,
    AssetValidationFailed,
    SkillRuntimeNameConflict,
)
from deerflow.persistence.private_work.model import RunAssetVersionRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentVersionSkillRefRow,
    ProjectSystemSkillBindingRow,
    SkillDesignOperationRow,
    SkillDesignSessionRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)

# Builder session states that may still progress; a deleted revision target
# collapses them to ``failed`` inside the delete transaction.
_LIVE_SKILL_DESIGN_STATUSES = (
    "interviewing",
    "generating",
    "awaiting_clarification",
    "draft_ready",
    "validated",
    "committing",
)
SKILL_DESIGN_TARGET_DELETED_ERROR_CODE = "SKILL_DESIGN_TARGET_DELETED"
_SKILL_DESIGN_TARGET_DELETED_MESSAGE = "修订目标 Skill 已被删除，本次修订会话已终止。"


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


@dataclass(frozen=True)
class SkillVersionContentRecord:
    asset: SkillRow
    version: SkillVersionRow
    files: tuple[SkillVersionFileRow, ...]


@dataclass(frozen=True)
class SkillVersionStorageRecord:
    version_id: uuid.UUID
    size_bytes: int


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

    async def lock_project_delete_scope(self, context: ProjectContext) -> None:
        """Serialize Skill deletion with every Builder mutation in a project.

        Builder writes take at least a Project-row SHARE lock before locking an
        Operation, Session, or target Skill.  Deletion takes this stronger gate
        first so its later Skill -> Session -> Operation cleanup cannot overlap
        the Builder's Operation -> Skill/Session paths.
        """

        self._require_project_actor(context)
        statement = (
            select(ProjectRow.id)
            .join(
                ProjectMembershipRow,
                ProjectMembershipRow.project_id == ProjectRow.id,
            )
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
            .with_for_update(of=ProjectRow)
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
            status="suspended",
            created_by_user_id=str(context.user_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def plan_project_asset_deletion(
        self,
        context: ProjectContext,
        asset: SkillRow,
    ) -> tuple[SkillVersionStorageRecord, ...]:
        """Lock a project Skill package and reject immutable external references."""

        self._require_project_actor(context)
        if asset.scope != "project" or asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        version_ids = tuple(
            (await self.session.execute(select(SkillVersionRow.id).where(SkillVersionRow.skill_id == asset.id).order_by(SkillVersionRow.version_number, SkillVersionRow.id).with_for_update(of=SkillVersionRow))).scalars().all()
        )
        if not version_ids:
            return ()
        agent_reference_exists = await self.session.scalar(
            select(
                exists().where(
                    AgentVersionSkillRefRow.skill_asset_id == asset.id,
                    AgentVersionSkillRefRow.skill_asset_scope == "project",
                )
            )
        )
        run_reference_exists = await self.session.scalar(
            select(
                exists().where(
                    RunAssetVersionRow.project_id == context.project_id,
                    RunAssetVersionRow.asset_kind == "skill",
                    RunAssetVersionRow.asset_scope == "project",
                    RunAssetVersionRow.asset_id == asset.id,
                    RunAssetVersionRow.version_id.in_(version_ids),
                )
            )
        )
        if agent_reference_exists or run_reference_exists:
            raise AssetInUse(context.request_id)
        size_rows = (
            await self.session.execute(
                select(
                    SkillVersionFileRow.skill_version_id,
                    func.coalesce(func.sum(SkillVersionFileRow.size_bytes), 0),
                )
                .where(SkillVersionFileRow.skill_version_id.in_(version_ids))
                .group_by(SkillVersionFileRow.skill_version_id)
            )
        ).all()
        sizes = {version_id: int(size_bytes) for version_id, size_bytes in size_rows}
        return tuple(
            SkillVersionStorageRecord(
                version_id=version_id,
                size_bytes=sizes.get(version_id, 0),
            )
            for version_id in version_ids
        )

    async def ensure_project_skill_runtime_name_available(
        self,
        context: ProjectContext | SystemAssetGovernanceContext,
        asset: SkillRow,
    ) -> None:
        """Reject activation when an enabled System Skill owns the same name.

        Project Skill activation already holds the project row lock before this
        check. System binding enable takes the same lock before its inverse
        check, which serializes the two state transitions.
        """

        project_id = getattr(context, "project_id", None)
        if not isinstance(project_id, uuid.UUID) or asset.scope != "project" or asset.project_id != project_id:
            raise AssetValidationFailed(_request_id(context))
        conflict = await self.session.scalar(
            select(
                exists().where(
                    ProjectSystemSkillBindingRow.project_id == project_id,
                    ProjectSystemSkillBindingRow.enabled.is_(True),
                    SkillRow.id == ProjectSystemSkillBindingRow.system_skill_id,
                    SkillRow.scope == "system",
                    SkillRow.project_id.is_(None),
                    func.lower(SkillRow.slug) == asset.slug.casefold(),
                )
            )
        )
        if conflict:
            raise SkillRuntimeNameConflict(_request_id(context))

    async def delete_project_asset(
        self,
        context: ProjectContext,
        asset: SkillRow,
        version_ids: Sequence[uuid.UUID],
    ) -> None:
        """Physically remove one already-locked project Skill package."""

        self._require_project_actor(context)
        selected_version_ids = tuple(version_ids)
        if asset.scope != "project" or asset.project_id != context.project_id or len(set(selected_version_ids)) != len(selected_version_ids):
            raise AssetNotFound(context.request_id)

        # Preserve owner-private Builder history while severing the deleted
        # shared-asset reference. Completed commit retries then fail closed.
        await self.session.execute(
            update(SkillDesignSessionRow)
            .where(
                SkillDesignSessionRow.project_id == context.project_id,
                SkillDesignSessionRow.created_skill_id == asset.id,
            )
            .values(
                created_skill_id=None,
                created_skill_version_id=None,
                created_skill_deleted=True,
            )
        )

        # Sever revision sessions that pinned this Skill as their target, and
        # fail-close the ones that could still progress. In-flight Builder
        # Runs observe the failed operation at their next tool-call boundary.
        revise_rows = (
            (
                await self.session.execute(
                    select(SkillDesignSessionRow)
                    .where(
                        SkillDesignSessionRow.project_id == context.project_id,
                        SkillDesignSessionRow.target_skill_id == asset.id,
                    )
                    .with_for_update(of=SkillDesignSessionRow)
                )
            )
            .scalars()
            .all()
        )
        for design_row in revise_rows:
            design_row.target_skill_id = None
            design_row.base_version_id = None
            design_row.base_version_number = None
            design_row.base_payload_checksum = None
            design_row.target_skill_deleted = True
            if design_row.status in _LIVE_SKILL_DESIGN_STATUSES:
                design_row.status = "failed"
                design_row.error_code = SKILL_DESIGN_TARGET_DELETED_ERROR_CODE
                design_row.error_message = _SKILL_DESIGN_TARGET_DELETED_MESSAGE
                design_row.active_clarification_json = None
                design_row.validation_json = None
                design_row.validated_draft_checksum = None
                design_row.revision += 1
                await self.session.execute(
                    update(SkillDesignOperationRow)
                    .where(
                        SkillDesignOperationRow.project_id == design_row.project_id,
                        SkillDesignOperationRow.owner_user_id == design_row.owner_user_id,
                        SkillDesignOperationRow.session_id == design_row.id,
                        SkillDesignOperationRow.status == "in_progress",
                    )
                    .values(
                        status="failed",
                        result_revision=design_row.revision,
                        public_error_code=SKILL_DESIGN_TARGET_DELETED_ERROR_CODE,
                    )
                )

        # This transient state is never committed: it combines with the cleared
        # pointer to authorize the immutable-child trigger added in revision 0002.
        asset.current_version_id = None
        asset.status = "archived"
        await self.session.flush()
        await self.session.scalar(
            select(
                func.set_config(
                    "deerflow.skill_hard_delete_asset_id",
                    str(asset.id),
                    True,
                )
            )
        )

        if selected_version_ids:
            await self.session.execute(
                delete(SkillVersionFileRow).where(
                    SkillVersionFileRow.skill_version_id.in_(
                        selected_version_ids,
                    )
                )
            )
            child = aliased(SkillVersionRow)
            remaining = set(selected_version_ids)
            while remaining:
                deleted_ids = set(
                    (
                        await self.session.execute(
                            delete(SkillVersionRow)
                            .where(
                                SkillVersionRow.id.in_(remaining),
                                SkillVersionRow.skill_id == asset.id,
                                ~exists().where(child.supersedes_version_id == SkillVersionRow.id),
                            )
                            .returning(SkillVersionRow.id)
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
        context: SystemAssetGovernanceContext | SystemAssetReadContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillRow:
        self._require_system_catalog_reader(context)
        if for_update and isinstance(context, SystemAssetReadContext):
            raise AssetForbidden(context.request_id)
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
        await self.session.scalar(
            select(
                func.set_config(
                    "deerflow.asset_version_assembly",
                    str(version.id),
                    True,
                )
            )
        )
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
                        SkillRow.current_version_id == SkillVersionRow.id,
                        SkillVersionRow.revoked_at.is_(None),
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

    async def get_project_visible_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillVersionContentRecord:
        """Load one exact authoring-visible Project or System Skill version."""

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
                        SkillRow.current_version_id == SkillVersionRow.id,
                        SkillVersionRow.revoked_at.is_(None),
                    ),
                ),
                self._project_context_exists(context),
            )
        )
        selected = (await self.session.execute(statement)).one_or_none()
        if selected is None:
            raise AssetNotFound(context.request_id)
        asset, version = selected
        return SkillVersionContentRecord(
            asset=asset,
            version=version,
            files=await self._load_files(version.id),
        )

    async def get_system_export_version(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillVersionContentRecord:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(SkillRow, SkillVersionRow)
            .join(SkillVersionRow, SkillVersionRow.skill_id == SkillRow.id)
            .where(
                SkillRow.id == asset_id,
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                SkillRow.current_version_id == SkillVersionRow.id,
                SkillVersionRow.revoked_at.is_(None),
            )
        )
        selected = (await self.session.execute(statement)).one_or_none()
        if selected is None:
            raise AssetNotFound(context.request_id)
        asset, version = selected
        return SkillVersionContentRecord(
            asset=asset,
            version=version,
            files=await self._load_files(version.id),
        )

    async def get_system_export_version_metadata(
        self,
        context: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillVersionMetadataRecord:
        self._require_system_actor(context)
        if context.project_id is not None:
            raise AssetNotFound(context.request_id)
        statement = (
            select(SkillRow, SkillVersionRow)
            .join(SkillVersionRow, SkillVersionRow.skill_id == SkillRow.id)
            .where(
                SkillRow.id == asset_id,
                SkillVersionRow.id == version_id,
                SkillVersionRow.skill_id == asset_id,
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                SkillRow.current_version_id == SkillVersionRow.id,
                SkillVersionRow.revoked_at.is_(None),
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
                        SkillRow.current_version_id == SkillVersionRow.id,
                        SkillVersionRow.revoked_at.is_(None),
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
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status == "active",
                SkillRow.current_version_id == SkillVersionRow.id,
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
                    ProjectSystemSkillBindingRow.system_skill_id == SkillRow.id,
                )
                .where(
                    SkillVersionRow.id == version_id,
                    SkillVersionRow.skill_id == asset_id,
                    SkillVersionRow.revoked_at.is_(None),
                    SkillRow.scope == "system",
                    SkillRow.project_id.is_(None),
                    SkillRow.status == "active",
                    SkillRow.current_version_id == SkillVersionRow.id,
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
                SkillVersionRow.revoked_at.is_(None),
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                SkillRow.status == "active",
                SkillRow.current_version_id == SkillVersionRow.id,
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
                SkillVersionRow.revoked_at.is_(None),
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status == "active",
                SkillRow.current_version_id == SkillVersionRow.id,
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
            .outerjoin(SkillVersionRow, SkillVersionRow.skill_id == SkillRow.id)
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
        context: SystemAssetGovernanceContext | SystemAssetReadContext,
        asset_id: uuid.UUID,
    ) -> tuple[SkillVersionRecord, ...]:
        self._require_system_catalog_reader(context)
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

    async def current_descriptions(
        self,
        asset_ids: Sequence[uuid.UUID],
    ) -> Mapping[uuid.UUID, str]:
        """Load current descriptions in one query for already-authorized rows."""

        ids = tuple(asset_ids)
        if not ids:
            return {}
        statement = (
            select(SkillRow.id, SkillVersionRow.description)
            .join(
                SkillVersionRow,
                SkillVersionRow.id == SkillRow.current_version_id,
            )
            .where(SkillRow.id.in_(ids))
        )
        return {asset_id: description for asset_id, description in (await self.session.execute(statement))}

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
