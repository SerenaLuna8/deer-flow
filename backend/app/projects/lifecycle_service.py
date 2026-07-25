from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.authorization import (
    AUTHORIZATION_REVOKED_REASON,
    PrivateRunAuthorizationService,
)
from app.private_work.retention import PrivateWorkRetentionService
from app.private_work.retention_jobs import RetentionJobAdmission
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.models import ProjectView


class ProjectLifecycleAuditPort(Protocol):
    async def project_deletion_requested(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None: ...

    async def project_recovered(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
    ) -> None: ...

    async def project_suspended(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None: ...

    async def project_resumed(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
    ) -> None: ...


class _NoopProjectLifecycleAudit:
    async def project_deletion_requested(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None:
        del session, context

    async def project_recovered(self, session: AsyncSession, **kwargs) -> None:
        del session, kwargs

    async def project_suspended(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None:
        del session, context

    async def project_resumed(self, session: AsyncSession, **kwargs) -> None:
        del session, kwargs


class ProjectLifecycleService:
    def __init__(
        self,
        repository: ProjectLifecycleRepository,
        *,
        authorization: object = PrivateRunAuthorizationService,
        retention: object = PrivateWorkRetentionService,
        retention_jobs: object = RetentionJobAdmission,
        audit: ProjectLifecycleAuditPort | None = None,
    ):
        self.repository = repository
        self._authorization = authorization
        self._retention = retention
        self._retention_jobs = retention_jobs
        self._audit = audit or _NoopProjectLifecycleAudit()

    async def request_deletion(
        self,
        context: ProjectContext,
        now: datetime,
    ) -> ProjectView:
        context.require(Capability.PROJECT_LIFECYCLE_MANAGE)
        async with self.repository.transaction():
            project, actor = await self.repository.lock_pending_deletion(context)
            members = await self.repository.lock_active_members(project.id)
            for member in members:
                await self._retention.freeze_owner(
                    self.repository.session,
                    project_id=project.id,
                    owner_user_id=member.user_id,
                    now=now,
                )
                await self._authorization.mark_revoked(
                    self.repository.session,
                    project_id=project.id,
                    owner_user_id=member.user_id,
                    reason=AUTHORIZATION_REVOKED_REASON,
                    now=now,
                )
            deletion_effective_at = now + timedelta(days=30)
            result = await self.repository.mark_pending_locked(
                project,
                actor,
                requested_at=now,
                effective_at=deletion_effective_at,
                requested_by_user_id=context.user_id,
                request_id=context.request_id,
            )
            await self._retention_jobs.admit_project(
                self.repository.session,
                project_id=project.id,
                deletion_effective_at=deletion_effective_at,
                now=now,
            )
            await self._audit.project_deletion_requested(
                self.repository.session,
                context,
            )
        return result

    async def restore(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
        now: datetime,
    ) -> ProjectView:
        async with self.repository.transaction():
            project, actor = await self.repository.lock_restore(user_id, project_id, now)
            members = await self.repository.lock_active_members(project.id)
            result = await self.repository.restore_locked(
                project,
                actor,
                request_id=request_id,
            )
            await self._retention_jobs.restore_project(
                self.repository.session,
                project_id=project.id,
                now=now,
            )
            await self._retention.restore_owners(
                self.repository.session,
                project_id=project.id,
                owner_user_ids=tuple(member.user_id for member in members),
                now=now,
            )
            await self._audit.project_recovered(
                self.repository.session,
                user_id=user_id,
                project_id=project_id,
                request_id=request_id,
            )
        return result

    async def suspend(
        self,
        context: ProjectContext,
        now: datetime,
    ) -> ProjectView:
        context.require(Capability.PROJECT_LIFECYCLE_MANAGE)
        async with self.repository.transaction():
            project, actor = await self.repository.lock_suspend(context)
            members = await self.repository.lock_active_members(project.id)
            for member in members:
                await self._retention.freeze_owner(
                    self.repository.session,
                    project_id=project.id,
                    owner_user_id=member.user_id,
                    now=now,
                )
                await self._authorization.mark_revoked(
                    self.repository.session,
                    project_id=project.id,
                    owner_user_id=member.user_id,
                    reason=AUTHORIZATION_REVOKED_REASON,
                    now=now,
                )
            result = await self.repository.suspend_locked(
                project,
                actor,
                request_id=context.request_id,
            )
            await self._audit.project_suspended(
                self.repository.session,
                context,
            )
        return result

    async def resume(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
        now: datetime,
    ) -> ProjectView:
        async with self.repository.transaction():
            project, actor = await self.repository.lock_resume(user_id, project_id)
            members = await self.repository.lock_active_members(project.id)
            result = await self.repository.resume_locked(project, actor, request_id=request_id)
            await self._retention.restore_owners(
                self.repository.session,
                project_id=project.id,
                owner_user_ids=tuple(member.user_id for member in members),
                now=now,
            )
            await self._audit.project_resumed(
                self.repository.session,
                user_id=user_id,
                project_id=project_id,
                request_id=request_id,
            )
        return result
