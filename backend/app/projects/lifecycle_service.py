from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from app.private_work.authorization import (
    AUTHORIZATION_REVOKED_REASON,
    PrivateRunAuthorizationService,
)
from app.private_work.retention import PrivateWorkRetentionService
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.models import ProjectView

logger = logging.getLogger(__name__)


class ProjectLifecycleService:
    def __init__(
        self,
        repository: ProjectLifecycleRepository,
        *,
        authorization: object = PrivateRunAuthorizationService,
        retention: object = PrivateWorkRetentionService,
        notify_local_cancellation: Callable[[tuple[str, ...], str], object] | None = None,
    ):
        self.repository = repository
        self._authorization = authorization
        self._retention = retention
        self._notifier = notify_local_cancellation

    async def request_deletion(
        self,
        context: ProjectContext,
        now: datetime,
    ) -> ProjectView:
        context.require(Capability.PROJECT_LIFECYCLE_MANAGE)
        run_ids: list[str] = []
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
                run_ids.extend(
                    await self._authorization.mark_revoked(
                        self.repository.session,
                        project_id=project.id,
                        owner_user_id=member.user_id,
                        reason=AUTHORIZATION_REVOKED_REASON,
                        now=now,
                    )
                )
            result = await self.repository.mark_pending_locked(
                project,
                actor,
                requested_at=now,
                effective_at=now + timedelta(days=30),
                requested_by_user_id=context.user_id,
                request_id=context.request_id,
            )
        await self._notify(tuple(run_ids))
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
            await self._retention.restore_owners(
                self.repository.session,
                project_id=project.id,
                owner_user_ids=tuple(member.user_id for member in members),
                now=now,
            )
        return result

    async def suspend(
        self,
        context: ProjectContext,
        now: datetime,
    ) -> ProjectView:
        context.require(Capability.PROJECT_LIFECYCLE_MANAGE)
        run_ids: list[str] = []
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
                run_ids.extend(
                    await self._authorization.mark_revoked(
                        self.repository.session,
                        project_id=project.id,
                        owner_user_id=member.user_id,
                        reason=AUTHORIZATION_REVOKED_REASON,
                        now=now,
                    )
                )
            result = await self.repository.suspend_locked(
                project,
                actor,
                request_id=context.request_id,
            )
        await self._notify(tuple(run_ids))
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
        return result

    async def _notify(self, run_ids: tuple[str, ...]) -> None:
        if not run_ids or self._notifier is None:
            return
        try:
            result = self._notifier(run_ids, AUTHORIZATION_REVOKED_REASON)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.warning("Local authorization cancellation notification failed", exc_info=True)
