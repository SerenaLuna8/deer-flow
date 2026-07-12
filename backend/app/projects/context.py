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
    identifier_filter = ProjectRow.id == project_identifier if isinstance(project_identifier, uuid.UUID) else ProjectRow.slug == project_identifier
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
        async with session.begin():
            rows = (await session.execute(statement)).all()
    except DBAPIError:
        raise ProjectDatabaseUnavailable() from None
    if len(rows) != 1:
        raise ProjectNotFound()
    row = rows[0]
    try:
        role = ProjectRole(row.role)
    except ValueError:
        raise ProjectNotFound() from None
    return ProjectContext(
        user_id=user_id,
        project_id=row.project_id,
        membership_id=row.membership_id,
        role=role,
        capabilities=capabilities_for(role),
        membership_version=row.membership_version,
        request_id=request_id,
    )
