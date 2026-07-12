from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.models import ProjectView


class ProjectLifecycleService:
    def __init__(self, repository: ProjectLifecycleRepository):
        self.repository = repository

    async def request_deletion(
        self,
        context: ProjectContext,
        now: datetime,
    ) -> ProjectView:
        context.require(Capability.PROJECT_LIFECYCLE_MANAGE)
        return await self.repository.mark_pending(
            context,
            requested_at=now,
            effective_at=now + timedelta(days=30),
        )

    async def restore(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
        now: datetime,
    ) -> ProjectView:
        return await self.repository.restore(
            user_id,
            project_id,
            request_id=request_id,
            now=now,
        )
