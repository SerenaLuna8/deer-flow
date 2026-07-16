from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkMcpQuotaExceeded,
    PrivateWorkRunQuotaExceeded,
    PrivateWorkStorageQuotaExceeded,
    PrivateWorkUnavailable,
)
from app.private_work.run_repository import PrivateRunRecord
from app.projects.errors import ProjectDatabaseUnavailable, ProjectMemberQuotaExceeded
from app.quotas.models import (
    QuotaError,
    QuotaExceeded,
    _issue_quota_compensation_authority,
)
from app.quotas.service import QuotaService
from deerflow.runtime.private_scope import PrivateResourceScope


class ProjectQuotaEnforcer:
    """Map domain mutations to the atomic project quota service."""

    def __init__(self, quotas: QuotaService) -> None:
        if type(quotas) is not QuotaService:
            raise TypeError("ProjectQuotaEnforcer requires QuotaService")
        self._quotas = quotas

    @staticmethod
    def _member_key(membership_id: uuid.UUID, membership_version: int) -> str:
        return f"member:{membership_id}:version:{membership_version}"

    @staticmethod
    def _run_key(run_id: str) -> str:
        return f"run:{run_id}"

    @staticmethod
    def _file_key(file_id: uuid.UUID) -> str:
        return f"file:{file_id}"

    async def reserve_member(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        membership_id: uuid.UUID,
        membership_version: int,
    ) -> None:
        try:
            await self._quotas.reserve(
                session,
                context,
                "members",
                1,
                self._member_key(membership_id, membership_version),
            )
        except QuotaExceeded:
            raise ProjectMemberQuotaExceeded() from None
        except QuotaError:
            raise ProjectDatabaseUnavailable() from None

    async def release_member(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        membership_id: uuid.UUID,
        membership_version: int,
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
                self._member_key(membership_id, membership_version),
            )
        except QuotaError:
            raise ProjectDatabaseUnavailable() from None

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
