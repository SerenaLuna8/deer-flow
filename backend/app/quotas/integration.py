from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkMcpQuotaExceeded,
    PrivateWorkRunQuotaExceeded,
    PrivateWorkStorageQuotaExceeded,
    PrivateWorkUnavailable,
)
from app.private_work.run_repository import PrivateRunRecord
from app.projects.errors import ProjectMemberQuotaExceeded, ProjectQuotaStateConflict
from app.quotas.models import (
    QuotaError,
    QuotaExceeded,
    QuotaUnavailable,
    _issue_project_storage_quota_authority,
    _issue_quota_compensation_authority,
    _issue_quota_reconciliation_authority,
)
from app.quotas.service import QuotaService
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.shared_assets import (
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.runtime.private_scope import PrivateResourceScope


class ProjectQuotaEnforcer:
    """Map domain mutations to the atomic project quota service."""

    def __init__(self, quotas: QuotaService) -> None:
        if type(quotas) is not QuotaService:
            raise TypeError("ProjectQuotaEnforcer requires QuotaService")
        self._quotas = quotas

    @staticmethod
    def _member_key(membership_id: uuid.UUID, activation_generation: int) -> str:
        return f"member:{membership_id}:activation:{activation_generation}"

    @staticmethod
    def _run_key(run_id: str) -> str:
        return f"run:{run_id}"

    @staticmethod
    def _file_key(file_id: uuid.UUID) -> str:
        return f"file:{file_id}"

    @staticmethod
    def _skill_version_key(version_id: uuid.UUID) -> str:
        return f"skill-version:{version_id}"

    async def reserve_member(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        membership_id: uuid.UUID,
        activation_generation: int,
    ) -> None:
        try:
            await self._quotas.reserve(
                session,
                context,
                "members",
                1,
                self._member_key(membership_id, activation_generation),
            )
        except QuotaExceeded:
            raise ProjectMemberQuotaExceeded() from None
        except QuotaUnavailable:
            raise
        except QuotaError:
            raise ProjectQuotaStateConflict() from None

    async def release_member(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        membership_id: uuid.UUID,
        activation_generation: int,
    ) -> None:
        try:
            await self._quotas.release(
                session,
                _issue_quota_compensation_authority(
                    scope,
                    reason="membership_end",
                ),
                "members",
                1,
                self._member_key(membership_id, activation_generation),
            )
        except QuotaUnavailable:
            raise
        except QuotaError:
            raise ProjectQuotaStateConflict() from None

    async def reserve_concurrent_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
    ) -> None:
        try:
            await self._quotas.reserve(
                session,
                context,
                "concurrent_runs",
                1,
                self._run_key(run.run_id),
            )
        except QuotaExceeded:
            raise PrivateWorkRunQuotaExceeded(context.request_id) from None
        except QuotaError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        try:
            await self._quotas.release(
                session,
                _issue_quota_compensation_authority(
                    scope,
                    reason="run_terminal",
                ),
                "concurrent_runs",
                1,
                self._run_key(run_id),
            )
        except QuotaError:
            raise PrivateWorkUnavailable(request_id) from None

    async def reserve_file(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        file_id: uuid.UUID,
        size: int,
    ) -> None:
        if size == 0:
            return
        try:
            await self._quotas.reserve(
                session,
                context,
                "storage_bytes",
                size,
                self._file_key(file_id),
            )
        except QuotaExceeded:
            raise PrivateWorkStorageQuotaExceeded(context.request_id) from None
        except QuotaError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None:
        if size == 0:
            return
        try:
            await self._quotas.release(
                session,
                _issue_quota_compensation_authority(
                    scope,
                    reason="file_delete",
                ),
                "storage_bytes",
                size,
                self._file_key(file_id),
            )
        except QuotaError:
            raise PrivateWorkUnavailable(request_id) from None

    async def reserve_skill_version(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        version_id: uuid.UUID,
        size: int,
    ) -> None:
        if size == 0:
            return
        await self._quotas.mutate_project_storage(
            session,
            _issue_project_storage_quota_authority(
                project_id,
                operation="reserve",
            ),
            size,
            self._skill_version_key(version_id),
        )

    async def release_skill_version(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        version_id: uuid.UUID,
        size: int,
    ) -> None:
        if size == 0:
            return
        await self._quotas.mutate_project_storage(
            session,
            _issue_project_storage_quota_authority(
                project_id,
                operation="release",
            ),
            size,
            self._skill_version_key(version_id),
        )

    async def release_skill_version_if_reserved(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        version_id: uuid.UUID,
        size: int,
    ) -> bool:
        """Release a v2 reservation while allowing a reservation-less v1 row."""

        if size == 0:
            return False
        return await self._quotas.release_project_storage_if_reserved(
            session,
            _issue_project_storage_quota_authority(
                project_id,
                operation="release",
            ),
            size,
            self._skill_version_key(version_id),
        )

    async def reconcile_project_storage(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
    ) -> None:
        """Set storage usage to the authoritative post-mutation row total."""

        async def expected_storage_bytes() -> int:
            private_file_bytes = await session.scalar(
                select(func.coalesce(func.sum(PrivateFileRow.size), 0)).where(
                    PrivateFileRow.project_id == project_id,
                    PrivateFileRow.status == "ready",
                )
            )
            project_skill_bytes = await session.scalar(
                select(func.coalesce(func.sum(SkillVersionFileRow.size_bytes), 0))
                .select_from(SkillVersionFileRow)
                .join(
                    SkillVersionRow,
                    SkillVersionRow.id == SkillVersionFileRow.skill_version_id,
                )
                .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
                .where(
                    SkillRow.scope == "project",
                    SkillRow.project_id == project_id,
                )
            )
            return int(private_file_bytes or 0) + int(project_skill_bytes or 0)

        await self._quotas.reconcile_project_storage(
            session,
            _issue_quota_reconciliation_authority(
                project_id,
                operation="quota_repair",
            ),
            expected_loader=expected_storage_bytes,
        )

    async def consume_mcp_dispatch(
        self,
        context: PrivateWorkContext,
        *,
        dispatch_id: uuid.UUID,
        now: datetime | None = None,
    ) -> None:
        try:
            await self._quotas.consume_new_session(
                context,
                "mcp_calls_daily",
                1,
                f"mcp-dispatch:{dispatch_id}",
                now=now,
            )
        except QuotaExceeded:
            raise PrivateWorkMcpQuotaExceeded(context.request_id) from None
        except QuotaError:
            raise PrivateWorkUnavailable(context.request_id) from None


__all__ = ["ProjectQuotaEnforcer"]
