from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectDeletionStateConflict,
    ProjectForbidden,
    ProjectNotFound,
)
from app.projects.models import ProjectRole, ProjectView
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow


class ProjectLifecycleRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        try:
            async with self.session.begin():
                yield
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def mark_pending(
        self,
        context: ProjectContext,
        *,
        requested_at: datetime,
        effective_at: datetime,
    ) -> ProjectView:
        async with self.transaction():
            project, actor = await self.lock_pending_deletion(context)
            return await self.mark_pending_locked(
                project,
                actor,
                requested_at=requested_at,
                effective_at=effective_at,
                requested_by_user_id=context.user_id,
                request_id=context.request_id,
            )

    async def restore(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        request_id: str,
        now: datetime,
    ) -> ProjectView:
        async with self.transaction():
            project, actor = await self.lock_restore(user_id, project_id, now)
            return await self.restore_locked(project, actor, request_id=request_id)

    async def lock_pending_deletion(
        self,
        context: ProjectContext,
    ) -> tuple[ProjectRow, ProjectMembershipRow]:
        project = await self._lock_project(context.project_id, not_found=ProjectNotFound)
        actor = await self._lock_current_actor(context)
        self._require_lifecycle_capability(actor)
        if project.status != "active":
            raise ProjectDeletionStateConflict()
        if project.is_suspended:
            raise ProjectNotFound()
        return project, actor

    async def lock_restore(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        now: datetime,
    ) -> tuple[ProjectRow, ProjectMembershipRow]:
        return await self.lock_recoverable_admin_project(user_id, project_id, now)

    async def lock_active_members(
        self,
        project_id: uuid.UUID,
    ) -> tuple[ProjectMembershipRow, ...]:
        rows = (
            await self.session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.status == "active",
                )
                .order_by(ProjectMembershipRow.id)
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalars()
        return tuple(rows)

    async def mark_pending_locked(
        self,
        project: ProjectRow,
        actor: ProjectMembershipRow,
        *,
        requested_at: datetime,
        effective_at: datetime,
        requested_by_user_id: uuid.UUID,
        request_id: str,
    ) -> ProjectView:
        project.status = "pending_deletion"
        project.deletion_requested_at = requested_at
        project.deletion_effective_at = effective_at
        project.deletion_requested_by_user_id = str(requested_by_user_id)
        project.membership_version += 1
        await self.session.flush()
        return await self._view(project, actor, request_id)

    async def restore_locked(
        self,
        project: ProjectRow,
        actor: ProjectMembershipRow,
        *,
        request_id: str,
    ) -> ProjectView:
        project.status = "active"
        project.deletion_requested_at = None
        project.deletion_effective_at = None
        project.deletion_requested_by_user_id = None
        project.membership_version += 1
        await self.session.flush()
        return await self._view(project, actor, request_id)

    async def lock_suspend(
        self,
        context: ProjectContext,
    ) -> tuple[ProjectRow, ProjectMembershipRow]:
        project, actor = await self.lock_pending_deletion(context)
        if project.is_suspended:
            raise ProjectDeletionStateConflict()
        return project, actor

    async def suspend_locked(
        self,
        project: ProjectRow,
        actor: ProjectMembershipRow,
        *,
        request_id: str,
    ) -> ProjectView:
        project.is_suspended = True
        project.membership_version += 1
        await self.session.flush()
        return await self._view(project, actor, request_id)

    async def lock_resume(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> tuple[ProjectRow, ProjectMembershipRow]:
        project = await self._lock_project(project_id, not_found=ProjectNotFound)
        actor = (
            await self.session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == str(user_id),
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.role == ProjectRole.ADMIN.value,
                )
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if actor is None or project.status != "active" or not project.is_suspended:
            raise ProjectDeletionStateConflict()
        return project, actor

    async def resume_locked(
        self,
        project: ProjectRow,
        actor: ProjectMembershipRow,
        *,
        request_id: str,
    ) -> ProjectView:
        project.is_suspended = False
        project.membership_version += 1
        await self.session.flush()
        return await self._view(project, actor, request_id)

    async def lock_recoverable_admin_project(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        now: datetime,
    ) -> tuple[ProjectRow, ProjectMembershipRow]:
        project = await self._lock_project(
            project_id,
            not_found=ProjectDeletionStateConflict,
        )
        actor = (
            await self.session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == str(user_id),
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.role == ProjectRole.ADMIN.value,
                )
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if actor is None or project.is_suspended or project.status != "pending_deletion" or project.deletion_effective_at is None or now >= project.deletion_effective_at:
            raise ProjectDeletionStateConflict()
        return project, actor

    async def _lock_project(
        self,
        project_id: uuid.UUID,
        *,
        not_found: type[Exception],
    ) -> ProjectRow:
        project = (await self.session.execute(select(ProjectRow).where(ProjectRow.id == project_id).with_for_update(of=ProjectRow))).scalar_one_or_none()
        if project is None:
            raise not_found()
        return project

    async def _lock_current_actor(
        self,
        context: ProjectContext,
    ) -> ProjectMembershipRow:
        actor = (
            await self.session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.id == context.membership_id,
                    ProjectMembershipRow.project_id == context.project_id,
                    ProjectMembershipRow.user_id == str(context.user_id),
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.version == context.membership_version,
                )
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if actor is None:
            raise ProjectNotFound()
        return actor

    @staticmethod
    def _require_lifecycle_capability(actor: ProjectMembershipRow) -> None:
        try:
            role = ProjectRole(actor.role)
        except ValueError:
            raise ProjectNotFound() from None
        if Capability.PROJECT_LIFECYCLE_MANAGE not in capabilities_for(role):
            raise ProjectForbidden(Capability.PROJECT_LIFECYCLE_MANAGE)

    async def _view(
        self,
        project: ProjectRow,
        membership: ProjectMembershipRow,
        request_id: str,
    ) -> ProjectView:
        member_count = (
            await self.session.execute(
                select(func.count()).where(
                    ProjectMembershipRow.project_id == project.id,
                    ProjectMembershipRow.status == "active",
                )
            )
        ).scalar_one()
        try:
            role = ProjectRole(membership.role)
        except ValueError:
            raise ProjectNotFound() from None
        return ProjectView(
            id=project.id,
            slug=project.slug,
            display_name=project.display_name,
            description=project.description,
            icon=project.icon,
            role=role,
            capabilities=capabilities_for(role),
            is_pinned=membership.is_pinned,
            last_entered_at=membership.last_entered_at,
            member_count=member_count,
            agent_count=0,
            skill_count=0,
            mcp_count=0,
            status=project.status,
            is_suspended=project.is_suspended,
            membership_version=membership.version,
            request_id=request_id,
            deletion_effective_at=project.deletion_effective_at,
        )
