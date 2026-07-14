from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability, capabilities_for
from app.projects.errors import ProjectDatabaseUnavailable, ProjectForbidden, ProjectNotFound
from app.projects.models import ProjectRole
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow


@dataclass(frozen=True)
class ProjectContext:
    user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID
    role: ProjectRole
    capabilities: frozenset[Capability]
    membership_version: int
    request_id: str

    def require(self, capability: Capability) -> None:
        if capability not in self.capabilities:
            raise ProjectForbidden(capability)


async def resolve_project_context(
    session: AsyncSession,
    user_id: uuid.UUID,
    project_identifier: uuid.UUID | str,
    request_id: str,
) -> ProjectContext:
    """Resolve trusted project authorization in one statement.

    ``UUID`` identifiers address a project ID. Strings always address a slug,
    even when the string itself is UUID-shaped, so caller intent is unambiguous.
    """
    async with session.begin():
        return await resolve_project_context_in_transaction(
            session,
            user_id,
            project_identifier,
            request_id,
        )


async def resolve_project_context_in_transaction(
    session: AsyncSession,
    user_id: uuid.UUID,
    project_identifier: uuid.UUID | str,
    request_id: str,
    *,
    lock: bool = False,
) -> ProjectContext:
    """Resolve project authority without changing the caller-owned transaction."""

    identifier_filter = ProjectRow.id == project_identifier if isinstance(project_identifier, uuid.UUID) else ProjectRow.slug == project_identifier
    if lock:
        project_statement = (
            select(ProjectRow.id)
            .where(
                identifier_filter,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
            .with_for_update(of=ProjectRow)
        )
        try:
            project_id = (await session.execute(project_statement)).scalar_one_or_none()
            if project_id is None:
                raise ProjectNotFound()
            membership_statement = (
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == str(user_id),
                    ProjectMembershipRow.status == "active",
                )
                .with_for_update(of=ProjectMembershipRow)
            )
            membership = (await session.execute(membership_statement)).scalar_one_or_none()
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None
        if membership is None:
            raise ProjectNotFound()
        return _project_context_from_values(
            user_id=user_id,
            project_id=project_id,
            membership_id=membership.id,
            role_value=membership.role,
            membership_version=membership.version,
            request_id=request_id,
        )

    statement = (
        select(
            ProjectRow.id.label("project_id"),
            ProjectMembershipRow.id.label("membership_id"),
            ProjectMembershipRow.role,
            ProjectMembershipRow.version.label("membership_version"),
        )
        .join(
            ProjectMembershipRow,
            and_(
                ProjectMembershipRow.project_id == ProjectRow.id,
                ProjectMembershipRow.user_id == str(user_id),
                ProjectMembershipRow.status == "active",
            ),
        )
        .where(
            identifier_filter,
            ProjectRow.status == "active",
            ProjectRow.is_suspended.is_(False),
        )
    )
    try:
        rows = (await session.execute(statement)).all()
    except DBAPIError:
        raise ProjectDatabaseUnavailable() from None
    if len(rows) != 1:
        raise ProjectNotFound()
    row = rows[0]
    return _project_context_from_values(
        user_id=user_id,
        project_id=row.project_id,
        membership_id=row.membership_id,
        role_value=row.role,
        membership_version=row.membership_version,
        request_id=request_id,
    )


def _project_context_from_values(
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    membership_id: uuid.UUID,
    role_value: str,
    membership_version: int,
    request_id: str,
) -> ProjectContext:
    try:
        role = ProjectRole(role_value)
    except ValueError:
        raise ProjectNotFound() from None
    return ProjectContext(
        user_id=user_id,
        project_id=project_id,
        membership_id=membership_id,
        role=role,
        capabilities=capabilities_for(role),
        membership_version=membership_version,
        request_id=request_id,
    )
