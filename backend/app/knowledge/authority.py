"""Host adapters for the Knowledge Package project-authority seam."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from actweave_knowledge import (
    KNOWLEDGE_FORBIDDEN,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkDatabaseUnavailable,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound


@dataclass(frozen=True, slots=True)
class ProjectKnowledgeAuthority:
    """Request authority revalidated under Project → Membership share locks."""

    _context: ProjectContext
    _capability: Capability

    def __init__(self, context: ProjectContext, capability: Capability) -> None:
        if type(context) is not ProjectContext:
            raise TypeError("context must be a server-issued ProjectContext")
        if type(capability) is not Capability:
            raise TypeError("capability must be a Capability")
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_capability", capability)

    @property
    def project_id(self) -> UUID:
        return self._context.project_id

    @property
    def actor_user_id(self) -> UUID:
        return self._context.user_id

    async def revalidate(self, session: AsyncSession) -> None:
        try:
            current = await resolve_project_context_in_transaction(
                session,
                self._context.user_id,
                self._context.project_id,
                self._context.request_id,
                lock_mode="share",
            )
        except ProjectNotFound:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在") from None
        except ProjectDatabaseUnavailable:
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "Knowledge 存储暂时不可用",
            ) from None
        if current.user_id != self._context.user_id or current.project_id != self._context.project_id or current.membership_id != self._context.membership_id or current.membership_version != self._context.membership_version:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")
        if self._capability not in current.capabilities:
            raise KnowledgeError(KNOWLEDGE_FORBIDDEN, "没有权限执行此操作")


@dataclass(frozen=True, slots=True)
class PrivateWorkKnowledgeAuthority:
    """Run-owner authority revalidated through the private-work seam."""

    _context: PrivateWorkContext
    _capability: Capability

    def __init__(self, context: PrivateWorkContext, capability: Capability) -> None:
        context = require_issued_private_work_context(context)
        if type(capability) is not Capability:
            raise TypeError("capability must be a Capability")
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_capability", capability)

    @property
    def project_id(self) -> UUID:
        return self._context.project_id

    @property
    def actor_user_id(self) -> UUID:
        return self._context.user_id

    async def revalidate(self, session: AsyncSession) -> None:
        try:
            await PrivateWorkRevalidator().require(
                session,
                self._context,
                self._capability,
                lock_mode="share",
            )
        except PrivateWorkNotFound:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在") from None
        except PrivateWorkForbidden:
            raise KnowledgeError(KNOWLEDGE_FORBIDDEN, "没有权限执行此操作") from None
        except PrivateWorkDatabaseUnavailable:
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "Knowledge 存储暂时不可用",
            ) from None
