from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.errors import ProjectMembershipVersionConflict, ProjectNotFound
from app.projects.membership_models import MembershipView
from app.projects.membership_repository import MembershipRepository
from app.projects.models import ProjectRole


class MembershipService:
    def __init__(self, repository: MembershipRepository, *, clock: Callable[[], datetime] | None = None):
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def list_members(self, context: ProjectContext) -> tuple[MembershipView, ...]:
        context.require(Capability.PROJECT_READ)
        return await self.repository.list_members(context)

    async def change_role(
        self,
        context: ProjectContext,
        membership_id: uuid.UUID,
        role: ProjectRole,
        expected_version: int,
    ) -> MembershipView:
        context.require(Capability.PROJECT_MEMBERS_MANAGE)
        async with self.repository.transaction():
            project, target = await self.repository.lock_project_and_member(context, membership_id)
            self._require_version(target.version, expected_version)
            target_role = self._role(target.role)
            if target_role is ProjectRole.ADMIN and role is not ProjectRole.ADMIN:
                await self.repository.require_another_active_admin(project.id, target.id)
            return await self.repository.set_role(project, target, role)

    async def remove(
        self,
        context: ProjectContext,
        membership_id: uuid.UUID,
        expected_version: int,
    ) -> MembershipView:
        context.require(Capability.PROJECT_MEMBERS_MANAGE)
        async with self.repository.transaction():
            project, target = await self.repository.lock_project_and_member(context, membership_id)
            self._require_version(target.version, expected_version)
            if self._role(target.role) is ProjectRole.ADMIN:
                await self.repository.require_another_active_admin(project.id, target.id)
            return await self._end(context, project, target, status="removed")

    async def leave(self, context: ProjectContext, expected_version: int) -> MembershipView:
        context.require(Capability.PROJECT_READ)
        async with self.repository.transaction():
            project, target = await self.repository.lock_project_and_member(context, context.membership_id)
            self._require_version(target.version, expected_version)
            if self._role(target.role) is ProjectRole.ADMIN:
                await self.repository.require_another_active_admin(project.id, target.id)
            return await self._end(context, project, target, status="left")

    async def _end(self, context: ProjectContext, project, target, *, status: str) -> MembershipView:
        ended_at = self._clock()
        return await self.repository.end_membership(
            project,
            target,
            status=status,
            ended_at=ended_at,
            retention_until=ended_at + timedelta(days=30),
            ended_by_user_id=context.user_id,
        )

    @staticmethod
    def _require_version(actual: int, expected: int) -> None:
        if actual != expected:
            raise ProjectMembershipVersionConflict()

    @staticmethod
    def _role(value: str | ProjectRole) -> ProjectRole:
        try:
            return ProjectRole(value)
        except ValueError:
            raise ProjectNotFound() from None
