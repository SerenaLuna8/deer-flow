from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectBootstrapFailed, ProjectDatabaseUnavailable
from app.projects.models import BootstrapResult, BootstrapStatus, ProjectRole
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.user.model import UserRow

if TYPE_CHECKING:
    from app.private_work.context import PrivateWorkContext

DEFAULT_PROJECT_SLUG = "default-project"
_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_5052_4F4A
_BOOTSTRAP_REQUEST_ID = "default-project-bootstrap"


class BootstrapQuotaPort(Protocol):
    async def reserve_member(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        membership_id: uuid.UUID,
        membership_version: int,
    ) -> None: ...


class _NoopBootstrapQuota:
    async def reserve_member(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        membership_id: uuid.UUID,
        membership_version: int,
    ) -> None:
        del session, context, membership_id, membership_version


async def bootstrap_default_project(
    session: AsyncSession,
    *,
    quota: BootstrapQuotaPort | None = None,
) -> BootstrapResult:
    quota_port = quota or _NoopBootstrapQuota()
    try:
        async with session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY})
            total, admin_count = (await session.execute(select(func.count(UserRow.id), func.count(UserRow.id).filter(UserRow.system_role == "system_admin")))).one()
            if total == 0:
                return BootstrapResult(BootstrapStatus.NO_USERS)
            if admin_count == 0:
                if total <= 1:
                    return BootstrapResult(BootstrapStatus.WAITING_FOR_ADMIN)
                raise ProjectBootstrapFailed("AMBIGUOUS_BOOTSTRAP_ADMIN")
            if admin_count != 1:
                raise ProjectBootstrapFailed("AMBIGUOUS_BOOTSTRAP_ADMIN")
            admin_id = (await session.execute(select(UserRow.id).where(UserRow.system_role == "system_admin"))).scalar_one()
            project = (await session.execute(select(ProjectRow).where(ProjectRow.slug == DEFAULT_PROJECT_SLUG))).scalar_one_or_none()
            if project is None:
                project = ProjectRow(slug=DEFAULT_PROJECT_SLUG, display_name="默认项目", created_by_user_id=admin_id)
                session.add(project)
                await session.flush()
                membership = ProjectMembershipRow(project_id=project.id, user_id=admin_id, role="admin")
                session.add(membership)
                await session.flush()
                context = ProjectContext(
                    user_id=uuid.UUID(str(admin_id)),
                    project_id=project.id,
                    membership_id=membership.id,
                    role=ProjectRole.ADMIN,
                    capabilities=capabilities_for(ProjectRole.ADMIN),
                    membership_version=membership.version,
                    request_id=_BOOTSTRAP_REQUEST_ID,
                )
                from app.private_work.context import PrivateWorkContext

                await quota_port.reserve_member(
                    session,
                    PrivateWorkContext.from_project(context),
                    membership_id=membership.id,
                    membership_version=membership.version,
                )
                return BootstrapResult(BootstrapStatus.CREATED, project.id)
            membership = (
                await session.execute(
                    select(ProjectMembershipRow).where(
                        ProjectMembershipRow.project_id == project.id,
                        ProjectMembershipRow.user_id == admin_id,
                    )
                )
            ).scalar_one_or_none()
            if project.status != "active" or project.is_suspended or project.created_by_user_id != admin_id or membership is None or membership.status != "active" or membership.role != "admin":
                raise ProjectBootstrapFailed("DEFAULT_PROJECT_CONFLICT")
            return BootstrapResult(BootstrapStatus.EXISTING, project.id)
    except DBAPIError:
        raise ProjectDatabaseUnavailable() from None
