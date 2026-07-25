from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import SystemAuditContext
from app.audit.sinks import SystemProjectLifecycleAuditSink
from app.private_work.authorization import (
    AUTHORIZATION_REVOKED_REASON,
    PrivateRunAuthorizationService,
)
from app.private_work.retention import PrivateWorkRetentionService
from app.reliability.errors import ReliabilityConflict, ReliabilityNotFound
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow


@dataclass(frozen=True, slots=True)
class SystemProjectLifecycleView:
    project_id: uuid.UUID
    slug: str
    display_name: str
    status: str
    is_suspended: bool
    state_version: int
    created_at: datetime
    updated_at: datetime
    deletion_effective_at: datetime | None


class SystemProjectLifecycleService:
    """Platform pause/resume without constructing project-member authority."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        audit: SystemProjectLifecycleAuditSink,
    ) -> None:
        self._session = session
        self._audit = audit

    async def suspend(
        self,
        context: SystemAuditContext,
        project_id: uuid.UUID,
        *,
        now: datetime,
    ) -> SystemProjectLifecycleView:
        project, owners = await self._lock_active_project(
            context,
            project_id,
            expected_suspended=False,
        )
        for owner_user_id in owners:
            await PrivateWorkRetentionService.freeze_owner(
                self._session,
                project_id=project_id,
                owner_user_id=owner_user_id,
                now=now,
            )
            await PrivateRunAuthorizationService.mark_revoked(
                self._session,
                project_id=project_id,
                owner_user_id=owner_user_id,
                reason=AUTHORIZATION_REVOKED_REASON,
                now=now,
            )
        project.is_suspended = True
        project.membership_version += 1
        project.updated_at = now
        await self._session.flush()
        await self._audit.project_suspended(
            self._session,
            project_id=project_id,
        )
        return self._view(project)

    async def resume(
        self,
        context: SystemAuditContext,
        project_id: uuid.UUID,
        *,
        now: datetime,
    ) -> SystemProjectLifecycleView:
        project, owners = await self._lock_active_project(
            context,
            project_id,
            expected_suspended=True,
        )
        await PrivateWorkRetentionService.restore_owners(
            self._session,
            project_id=project_id,
            owner_user_ids=owners,
            now=now,
        )
        project.is_suspended = False
        project.membership_version += 1
        project.updated_at = now
        await self._session.flush()
        await self._audit.project_resumed(
            self._session,
            project_id=project_id,
        )
        return self._view(project)

    async def _lock_active_project(
        self,
        context: SystemAuditContext,
        project_id: uuid.UUID,
        *,
        expected_suspended: bool,
    ) -> tuple[ProjectRow, tuple[str, ...]]:
        if type(project_id) is not uuid.UUID:
            raise ReliabilityNotFound(context.request_id)
        project = (await self._session.execute(select(ProjectRow).where(ProjectRow.id == project_id).with_for_update(of=ProjectRow))).scalar_one_or_none()
        if project is None:
            raise ReliabilityNotFound(context.request_id)
        if project.status != "active" or project.is_suspended is not expected_suspended:
            raise ReliabilityConflict(context.request_id)
        owners = tuple(
            (
                await self._session.execute(
                    select(ProjectMembershipRow.user_id)
                    .where(
                        ProjectMembershipRow.project_id == project_id,
                        ProjectMembershipRow.status == "active",
                    )
                    .order_by(ProjectMembershipRow.user_id)
                    .with_for_update(of=ProjectMembershipRow)
                )
            )
            .scalars()
            .all()
        )
        return project, owners

    @staticmethod
    def _view(project: ProjectRow) -> SystemProjectLifecycleView:
        return SystemProjectLifecycleView(
            project_id=project.id,
            slug=project.slug,
            display_name=project.display_name,
            status=project.status,
            is_suspended=project.is_suspended,
            state_version=project.membership_version,
            created_at=project.created_at,
            updated_at=project.updated_at,
            deletion_effective_at=project.deletion_effective_at,
        )


__all__ = [
    "SystemProjectLifecycleService",
    "SystemProjectLifecycleView",
]
