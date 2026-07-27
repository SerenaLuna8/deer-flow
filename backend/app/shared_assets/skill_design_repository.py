from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import SkillArchiveFile
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    SkillDesignDraftFileRow,
    SkillDesignOperationRow,
    SkillDesignSessionRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)

_SKILL_CREATOR_SLUG = "skill-creator"
_SKILL_CREATOR_MAX_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class PinnedSkillCreator:
    skill_id: uuid.UUID
    version_id: uuid.UUID
    payload_checksum: str
    skill_md_content: str


def _metadata_checksum(files: tuple[SkillVersionFileRow, ...]) -> str:
    canonical = json.dumps(
        [
            {
                "path": item.path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in files
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class SkillDesignRepository:
    """Owner-scoped persistence for conversational Skill Builder sessions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _require_context(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _context_exists(context: ProjectContext):
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

    async def lock_context(self, context: ProjectContext) -> None:
        self._require_context(context)
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
            .with_for_update(read=True, of=[ProjectRow, ProjectMembershipRow])
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def lock_session_create_scope(
        self,
        context: ProjectContext,
    ) -> None:
        """Serialize per-project Builder admission before counting sessions."""

        self._require_context(context)
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

    async def count_incomplete(
        self,
        context: ProjectContext,
    ) -> int:
        self._require_context(context)
        value = await self.session.scalar(
            select(func.count())
            .select_from(SkillDesignSessionRow)
            .where(
                SkillDesignSessionRow.project_id == context.project_id,
                SkillDesignSessionRow.owner_user_id == str(context.user_id),
                SkillDesignSessionRow.status.not_in(("completed", "cancelled")),
                self._context_exists(context),
            )
        )
        return int(value or 0)

    async def create(
        self,
        context: ProjectContext,
        row: SkillDesignSessionRow,
    ) -> SkillDesignSessionRow:
        await self.lock_context(context)
        if row.project_id != context.project_id or row.owner_user_id != str(context.user_id):
            raise AssetForbidden(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_by_create_idempotency(
        self,
        context: ProjectContext,
        idempotency_key_hash: str,
        *,
        for_update: bool = False,
    ) -> SkillDesignSessionRow | None:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(SkillDesignSessionRow).where(
            SkillDesignSessionRow.project_id == context.project_id,
            SkillDesignSessionRow.owner_user_id == str(context.user_id),
            SkillDesignSessionRow.create_idempotency_key_hash == idempotency_key_hash,
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=SkillDesignSessionRow)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillDesignSessionRow:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(SkillDesignSessionRow).where(
            SkillDesignSessionRow.id == session_id,
            SkillDesignSessionRow.project_id == context.project_id,
            SkillDesignSessionRow.owner_user_id == str(context.user_id),
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=SkillDesignSessionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def list_incomplete(
        self,
        context: ProjectContext,
        *,
        limit: int = 20,
    ) -> tuple[SkillDesignSessionRow, ...]:
        self._require_context(context)
        statement = (
            select(SkillDesignSessionRow)
            .where(
                SkillDesignSessionRow.project_id == context.project_id,
                SkillDesignSessionRow.owner_user_id == str(context.user_id),
                SkillDesignSessionRow.status.notin_(("completed", "cancelled")),
                self._context_exists(context),
            )
            .order_by(
                SkillDesignSessionRow.updated_at.desc(),
                SkillDesignSessionRow.id.desc(),
            )
            .limit(limit)
        )
        return tuple((await self.session.execute(statement)).scalars())

    async def get_operation(
        self,
        context: ProjectContext,
        *,
        operation_kind: str,
        idempotency_key_hash: str,
        for_update: bool = False,
    ) -> SkillDesignOperationRow | None:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(SkillDesignOperationRow).where(
            SkillDesignOperationRow.project_id == context.project_id,
            SkillDesignOperationRow.owner_user_id == str(context.user_id),
            SkillDesignOperationRow.operation_kind == operation_kind,
            SkillDesignOperationRow.idempotency_key_hash == idempotency_key_hash,
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=SkillDesignOperationRow)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def create_operation(
        self,
        context: ProjectContext,
        row: SkillDesignOperationRow,
    ) -> SkillDesignOperationRow:
        await self.lock_context(context)
        if row.project_id != context.project_id or row.owner_user_id != str(context.user_id):
            raise AssetForbidden(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def load_draft_files(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> tuple[SkillArchiveFile, ...]:
        self._require_context(context)
        statement = (
            select(SkillDesignDraftFileRow)
            .where(
                SkillDesignDraftFileRow.project_id == context.project_id,
                SkillDesignDraftFileRow.owner_user_id == str(context.user_id),
                SkillDesignDraftFileRow.session_id == session_id,
                self._context_exists(context),
            )
            .order_by(SkillDesignDraftFileRow.path)
        )
        if for_update:
            statement = statement.with_for_update(of=SkillDesignDraftFileRow)
        rows = tuple((await self.session.execute(statement)).scalars())
        files: list[SkillArchiveFile] = []
        for row in rows:
            if len(row.content) != row.size_bytes or hashlib.sha256(row.content).hexdigest() != row.sha256:
                raise AssetValidationFailed(context.request_id)
            files.append(
                SkillArchiveFile(
                    path=row.path,
                    content=row.content,
                    media_type=row.media_type,
                )
            )
        return tuple(files)

    async def replace_draft_files(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        files: tuple[SkillArchiveFile, ...],
    ) -> None:
        self._require_context(context)
        await self.get(context, session_id, for_update=True)
        await self.session.execute(
            delete(SkillDesignDraftFileRow).where(
                SkillDesignDraftFileRow.project_id == context.project_id,
                SkillDesignDraftFileRow.owner_user_id == str(context.user_id),
                SkillDesignDraftFileRow.session_id == session_id,
            )
        )
        for item in files:
            if not isinstance(item, SkillArchiveFile) or not isinstance(item.path, str) or not isinstance(item.media_type, str) or not isinstance(item.content, bytes):
                raise AssetValidationFailed(context.request_id)
            self.session.add(
                SkillDesignDraftFileRow(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    session_id=session_id,
                    path=item.path,
                    media_type=item.media_type,
                    size_bytes=len(item.content),
                    sha256=hashlib.sha256(item.content).hexdigest(),
                    content=item.content,
                )
            )
        await self.session.flush()

    async def clear_draft_files(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
    ) -> None:
        self._require_context(context)
        await self.get(context, session_id, for_update=True)
        await self.session.execute(
            delete(SkillDesignDraftFileRow).where(
                SkillDesignDraftFileRow.project_id == context.project_id,
                SkillDesignDraftFileRow.owner_user_id == str(context.user_id),
                SkillDesignDraftFileRow.session_id == session_id,
            )
        )
        await self.session.flush()

    async def project_skill_name_exists(
        self,
        context: ProjectContext,
        *,
        slug: str,
        display_name: str,
    ) -> bool:
        """Check existing project assets after locking current authority."""

        self._require_context(context)
        await self.lock_context(context)
        return bool(
            await self.session.scalar(
                select(
                    exists().where(
                        SkillRow.scope == "project",
                        SkillRow.project_id == context.project_id,
                        ((func.lower(SkillRow.slug) == slug.casefold()) | (func.lower(SkillRow.display_name) == display_name.casefold())),
                    )
                )
            )
        )

    async def resolve_current_skill_creator(
        self,
        context: ProjectContext,
    ) -> PinnedSkillCreator:
        """Resolve current packaged skill-creator without a project binding."""

        self._require_context(context)
        await self.lock_context(context)
        statement = (
            select(SkillRow, SkillVersionRow)
            .join(
                SkillVersionRow,
                SkillVersionRow.id == SkillRow.current_published_version_id,
            )
            .where(
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                SkillRow.status == "active",
                func.lower(SkillRow.slug) == _SKILL_CREATOR_SLUG,
                SkillVersionRow.skill_id == SkillRow.id,
                SkillVersionRow.workflow_status == "published",
            )
        )
        record = (await self.session.execute(statement)).one_or_none()
        if record is None:
            raise AssetStorageUnavailable(context.request_id)
        skill, version = record
        return await self._load_skill_creator_content(
            context,
            skill=skill,
            version=version,
            expected_checksum=version.payload_checksum,
        )

    async def load_pinned_skill_creator(
        self,
        context: ProjectContext,
        row: SkillDesignSessionRow,
    ) -> PinnedSkillCreator:
        """Load only the exact immutable version/checksum pinned at create."""

        self._require_context(context)
        if row.project_id != context.project_id or row.owner_user_id != str(context.user_id):
            raise AssetForbidden(context.request_id)
        statement = (
            select(SkillRow, SkillVersionRow)
            .join(
                SkillVersionRow,
                SkillVersionRow.skill_id == SkillRow.id,
            )
            .where(
                SkillRow.id == row.skill_creator_skill_id,
                SkillRow.scope == "system",
                SkillRow.project_id.is_(None),
                func.lower(SkillRow.slug) == _SKILL_CREATOR_SLUG,
                SkillVersionRow.id == row.skill_creator_version_id,
                SkillVersionRow.skill_id == row.skill_creator_skill_id,
                SkillVersionRow.workflow_status == "published",
                SkillVersionRow.payload_checksum == row.skill_creator_payload_checksum,
            )
        )
        record = (await self.session.execute(statement)).one_or_none()
        if record is None:
            raise AssetStorageUnavailable(context.request_id)
        skill, version = record
        return await self._load_skill_creator_content(
            context,
            skill=skill,
            version=version,
            expected_checksum=row.skill_creator_payload_checksum,
        )

    async def _load_skill_creator_content(
        self,
        context: ProjectContext,
        *,
        skill: SkillRow,
        version: SkillVersionRow,
        expected_checksum: str,
    ) -> PinnedSkillCreator:
        files = tuple(
            (
                await self.session.execute(
                    select(SkillVersionFileRow)
                    .where(
                        SkillVersionFileRow.skill_version_id == version.id,
                    )
                    .order_by(SkillVersionFileRow.path)
                )
            ).scalars()
        )
        if not files or _metadata_checksum(files) != expected_checksum:
            raise AssetValidationFailed(context.request_id)
        selected: SkillVersionFileRow | None = None
        for item in files:
            if len(item.content) != item.size_bytes or hashlib.sha256(item.content).hexdigest() != item.sha256:
                raise AssetValidationFailed(context.request_id)
            if item.path == "SKILL.md":
                selected = item
        if selected is None or selected.size_bytes > _SKILL_CREATOR_MAX_BYTES:
            raise AssetValidationFailed(context.request_id)
        try:
            content = selected.content.decode("utf-8")
        except UnicodeDecodeError:
            raise AssetValidationFailed(context.request_id) from None
        if not content.strip() or "\x00" in content:
            raise AssetValidationFailed(context.request_id)
        return PinnedSkillCreator(
            skill_id=skill.id,
            version_id=version.id,
            payload_checksum=expected_checksum,
            skill_md_content=content,
        )


__all__ = ["PinnedSkillCreator", "SkillDesignRepository"]
