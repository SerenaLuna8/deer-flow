from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkDatabaseUnavailable,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound


class PrivateWorkRevalidator:
    async def require(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *capabilities: Capability,
        lock: bool | None = None,
        lock_mode: Literal["none", "share", "update"] = "none",
    ) -> ProjectContext:
        context = require_issued_private_work_context(context)
        try:
            current = await resolve_project_context_in_transaction(
                session,
                context.user_id,
                context.project_id,
                context.request_id,
                lock=lock,
                lock_mode=lock_mode,
            )
        except ProjectNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except ProjectDatabaseUnavailable:
            raise PrivateWorkDatabaseUnavailable(context.request_id) from None
        if type(current) is not ProjectContext or current.user_id != context.user_id or current.project_id != context.project_id or current.membership_id != context.membership_id or current.membership_version != context.membership_version:
            raise PrivateWorkNotFound(context.request_id)
        for capability in capabilities:
            if capability not in current.capabilities:
                raise PrivateWorkForbidden(context.request_id)
        return current
