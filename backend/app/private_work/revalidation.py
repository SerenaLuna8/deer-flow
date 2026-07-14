from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkForbidden, PrivateWorkNotFound, PrivateWorkUnavailable
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound


class PrivateWorkRevalidator:
    async def require(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *capabilities: Capability,
        lock: bool = False,
    ) -> ProjectContext:
        request_id = getattr(context, "request_id", "unknown")
        if type(context) is not PrivateWorkContext:
            raise PrivateWorkNotFound(request_id if isinstance(request_id, str) else "unknown")
        try:
            current = await resolve_project_context_in_transaction(
                session,
                context.user_id,
                context.project_id,
                context.request_id,
                lock=lock,
            )
        except ProjectNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except ProjectDatabaseUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        if type(current) is not ProjectContext or current.user_id != context.user_id or current.project_id != context.project_id or current.membership_id != context.membership_id or current.membership_version != context.membership_version:
            raise PrivateWorkNotFound(context.request_id)
        for capability in capabilities:
            if capability not in current.capabilities:
                raise PrivateWorkForbidden(context.request_id)
        return current
