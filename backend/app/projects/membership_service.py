from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.private_work.authorization import (
    AUTHORIZATION_REVOKED_REASON,
    PrivateRunAuthorizationService,
)
from app.private_work.retention import PrivateWorkRetentionService
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.errors import ProjectMembershipVersionConflict, ProjectNotFound
from app.projects.membership_models import MembershipView
from app.projects.membership_repository import MembershipRepository
from app.projects.models import ProjectRole

logger = logging.getLogger(__name__)


class MembershipService:
    def __init__(
        self,
        repository: MembershipRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        authorization: object = PrivateRunAuthorizationService,
        retention: object = PrivateWorkRetentionService,
        notify_local_cancellation: Callable[[tuple[str, ...], str], object] | None = None,
    ):
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._authorization = authorization
        self._retention = retention
        self._notifier = notify_local_cancellation

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
        run_ids: tuple[str, ...] = ()
        async with self.repository.transaction():
            project, target = await self.repository.lock_project_and_member(context, membership_id)
            self._require_version(target.version, expected_version)
            target_role = self._role(target.role)
            if target_role is ProjectRole.ADMIN and role is not ProjectRole.ADMIN:
                await self.repository.require_another_active_admin(project.id, target.id)
            if target_role is not ProjectRole.VIEWER and role is ProjectRole.VIEWER:
                revoked_at = self._clock()
                run_ids = await self._authorization.mark_revoked(
                    self.repository.session,
                    project_id=project.id,
                    owner_user_id=target.user_id,
                    reason=AUTHORIZATION_REVOKED_REASON,
                    now=revoked_at,
                )
                await self._retention.freeze_owner(
                    self.repository.session,
                    project_id=project.id,
                    owner_user_id=target.user_id,
                    now=revoked_at,
                )
            result = await self.repository.set_role(project, target, role)
        await self._notify(run_ids)
        return result

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
            result, run_ids = await self._end(context, project, target, status="removed")
        await self._notify(run_ids)
        return result

    async def leave(self, context: ProjectContext, expected_version: int) -> MembershipView:
        context.require(Capability.PROJECT_READ)
        async with self.repository.transaction():
            project, target = await self.repository.lock_project_and_member(context, context.membership_id)
            self._require_version(target.version, expected_version)
            if self._role(target.role) is ProjectRole.ADMIN:
                await self.repository.require_another_active_admin(project.id, target.id)
            result, run_ids = await self._end(context, project, target, status="left")
        await self._notify(run_ids)
        return result

    async def _end(self, context: ProjectContext, project, target, *, status: str) -> tuple[MembershipView, tuple[str, ...]]:
        ended_at = self._clock()
        run_ids = await self._authorization.mark_revoked(
            self.repository.session,
            project_id=project.id,
            owner_user_id=target.user_id,
            reason=AUTHORIZATION_REVOKED_REASON,
            now=ended_at,
        )
        await self._retention.freeze_owner(
            self.repository.session,
            project_id=project.id,
            owner_user_id=target.user_id,
            now=ended_at,
        )
        result = await self.repository.end_membership(
            project,
            target,
            status=status,
            ended_at=ended_at,
            retention_until=ended_at + timedelta(days=30),
            ended_by_user_id=context.user_id,
        )
        return result, run_ids

    async def _notify(self, run_ids: tuple[str, ...]) -> None:
        if not run_ids or self._notifier is None:
            return
        try:
            result = self._notifier(run_ids, AUTHORIZATION_REVOKED_REASON)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.warning("Local authorization cancellation notification failed", exc_info=True)

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
