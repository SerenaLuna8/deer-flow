from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.projects.context import ProjectContext
from app.projects.errors import ProjectDatabaseUnavailable, ProjectLastAdmin, ProjectNotFound
from app.projects.membership_models import MembershipView
from app.projects.models import ProjectRole
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.user.model import UserRow


class MembershipRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        try:
            async with self.session.begin():
                yield
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    def _actor_scope(self, context: ProjectContext):
        actor = aliased(ProjectMembershipRow)
        return exists(
            select(1).where(
                actor.id == context.membership_id,
                actor.project_id == context.project_id,
                actor.user_id == str(context.user_id),
                actor.status == "active",
                actor.version == context.membership_version,
            )
        )

    async def list_members(self, context: ProjectContext) -> tuple[MembershipView, ...]:
        try:
            async with self.session.begin():
                statement = (
                    select(ProjectMembershipRow, UserRow)
                    .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
                    .join(UserRow, UserRow.id == ProjectMembershipRow.user_id)
                    .where(
                        ProjectRow.id == context.project_id,
                        ProjectRow.status == "active",
                        ProjectRow.is_suspended.is_(False),
                        ProjectMembershipRow.status == "active",
                        self._actor_scope(context),
                    )
                    .order_by(ProjectMembershipRow.created_at, ProjectMembershipRow.id)
                )
                rows = (await self.session.execute(statement)).all()
                if not rows:
                    raise ProjectNotFound()
                return tuple(self._view(row.ProjectMembershipRow, row.UserRow) for row in rows)
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def lock_project_and_member(
        self,
        context: ProjectContext,
        membership_id: uuid.UUID,
    ) -> tuple[ProjectRow, ProjectMembershipRow]:
        project_statement = (
            select(ProjectRow)
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
            .with_for_update(of=ProjectRow)
        )
        project = (await self.session.execute(project_statement)).scalar_one_or_none()
        if project is None:
            raise ProjectNotFound()

        actor_is_current = (await self.session.execute(select(self._actor_scope(context)))).scalar_one()
        if not actor_is_current:
            raise ProjectNotFound()

        member_statement = (
            select(ProjectMembershipRow)
            .where(
                ProjectMembershipRow.id == membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.status == "active",
            )
            .with_for_update()
        )
        target = (await self.session.execute(member_statement)).scalar_one_or_none()
        if target is None:
            raise ProjectNotFound()
        return project, target

    async def require_another_active_admin(self, project_id: uuid.UUID, membership_id: uuid.UUID) -> None:
        statement = select(
            exists(
                select(1).where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.id != membership_id,
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.role == ProjectRole.ADMIN.value,
                )
            )
        )
        if not (await self.session.execute(statement)).scalar_one():
            raise ProjectLastAdmin()

    async def set_role(
        self,
        project: ProjectRow,
        target: ProjectMembershipRow,
        role: ProjectRole,
    ) -> MembershipView:
        if target.role != role.value:
            target.role = role.value
            target.version += 1
            project.membership_version += 1
            await self.session.flush()
        return await self._read_member(project.id, target.id)

    async def end_membership(
        self,
        project: ProjectRow,
        target: ProjectMembershipRow,
        *,
        status: str,
        ended_at: datetime,
        retention_until: datetime,
        ended_by_user_id: uuid.UUID,
    ) -> MembershipView:
        target.status = status
        target.ended_at = ended_at
        target.retention_until = retention_until
        target.ended_by_user_id = str(ended_by_user_id)
        target.end_reason = status
        target.version += 1
        project.membership_version += 1
        await self.session.flush()
        return await self._read_member(project.id, target.id)

    async def _read_member(self, project_id: uuid.UUID, membership_id: uuid.UUID) -> MembershipView:
        statement = (
            select(ProjectMembershipRow, UserRow)
            .join(UserRow, UserRow.id == ProjectMembershipRow.user_id)
            .where(
                ProjectMembershipRow.id == membership_id,
                ProjectMembershipRow.project_id == project_id,
            )
        )
        rows = (await self.session.execute(statement)).all()
        if len(rows) != 1:
            raise ProjectNotFound()
        row = rows[0]
        return self._view(row.ProjectMembershipRow, row.UserRow)

    @staticmethod
    def _view(membership: ProjectMembershipRow, user: UserRow) -> MembershipView:
        try:
            role = ProjectRole(membership.role)
            user_id = uuid.UUID(user.id)
        except (ValueError, TypeError):
            raise ProjectNotFound() from None
        if membership.status not in {"active", "left", "removed"}:
            raise ProjectNotFound()
        return MembershipView(
            membership_id=membership.id,
            user_id=user_id,
            account_email=user.email,
            role=role,
            status=membership.status,
            version=membership.version,
            joined_at=membership.created_at,
        )
