from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound, ProjectSlugConflict, ProjectValidationFailed
from app.projects.models import CreateProject, ProjectChanges, ProjectPage, ProjectRole, ProjectView
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


def _encode_cursor(row) -> str:
    payload = {
        "v": 1,
        "p": bool(row.is_pinned),
        "l": row.last_entered_at.isoformat() if row.last_entered_at else None,
        "c": row.created_at.isoformat(),
        "i": str(row.id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[bool, datetime | None, datetime, uuid.UUID]:
    try:
        if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
            raise ValueError
        data = json.loads(raw)
        if set(data) != {"v", "p", "l", "c", "i"} or data["v"] != 1 or type(data["p"]) is not bool:
            raise ValueError
        last = datetime.fromisoformat(data["l"]) if data["l"] is not None else None
        created = datetime.fromisoformat(data["c"])
        if created.tzinfo is None or (last is not None and last.tzinfo is None):
            raise ValueError
        return data["p"], last, created, uuid.UUID(data["i"])
    except Exception:
        raise ProjectValidationFailed("invalid_cursor") from None


class ProjectRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_with_admin(self, user_id: uuid.UUID, command: CreateProject, request_id: str) -> ProjectContext:
        project = ProjectRow(
            slug=command.slug,
            display_name=command.display_name,
            description=command.description,
            icon=command.icon,
            created_by_user_id=str(user_id),
        )
        membership = ProjectMembershipRow(project_id=project.id, user_id=str(user_id), role="admin")
        try:
            async with self.session.begin():
                self.session.add(project)
                await self.session.flush()
                membership.project_id = project.id
                self.session.add(membership)
                await self.session.flush()
        except IntegrityError as exc:
            if _constraint_name(exc) == "uq_projects_slug":
                raise ProjectSlugConflict() from None
            raise ProjectDatabaseUnavailable() from None
        except SQLAlchemyError:
            raise ProjectDatabaseUnavailable() from None
        return ProjectContext(user_id, project.id, membership.id, ProjectRole.ADMIN, capabilities_for(ProjectRole.ADMIN), membership.version, request_id)

    def _scope(self, context: ProjectContext):
        return and_(
            ProjectMembershipRow.id == context.membership_id,
            ProjectMembershipRow.project_id == context.project_id,
            ProjectMembershipRow.user_id == str(context.user_id),
            ProjectMembershipRow.status == "active",
            ProjectMembershipRow.version == context.membership_version,
            ProjectRow.id == context.project_id,
            ProjectRow.status == "active",
            ProjectRow.is_suspended.is_(False),
        )

    async def get(self, context: ProjectContext) -> ProjectView:
        member_count = select(func.count()).where(ProjectMembershipRow.project_id == ProjectRow.id, ProjectMembershipRow.status == "active").correlate(ProjectRow).scalar_subquery()
        statement = select(ProjectRow, ProjectMembershipRow, member_count.label("member_count")).join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id).where(self._scope(context))
        try:
            rows = (await self.session.execute(statement)).all()
        except SQLAlchemyError:
            raise ProjectDatabaseUnavailable() from None
        if len(rows) != 1:
            raise ProjectNotFound()
        row = rows[0]
        return self._view(row.ProjectRow, row.ProjectMembershipRow, row.member_count, context.request_id)

    def _view(self, project, membership, member_count: int, request_id: str) -> ProjectView:
        try:
            role = ProjectRole(membership.role)
        except ValueError:
            raise ProjectNotFound() from None
        return ProjectView(
            project.id,
            project.slug,
            project.display_name,
            project.description,
            project.icon,
            role,
            capabilities_for(role),
            membership.is_pinned,
            membership.last_entered_at,
            member_count,
            0,
            0,
            0,
            project.status,
            project.is_suspended,
            membership.version,
            request_id,
        )

    async def update(self, context: ProjectContext, changes: ProjectChanges) -> ProjectView:
        values = {key: value for key, value in vars(changes).items() if value is not None}
        membership_scope = exists(
            select(1).where(
                ProjectMembershipRow.project_id == ProjectRow.id,
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
        )
        try:
            result = await self.session.execute(update(ProjectRow).where(ProjectRow.id == context.project_id, ProjectRow.status == "active", ProjectRow.is_suspended.is_(False), membership_scope).values(**values))
            if result.rowcount != 1:
                await self.session.rollback()
                raise ProjectNotFound()
            await self.session.commit()
        except ProjectNotFound:
            raise
        except SQLAlchemyError:
            await self.session.rollback()
            raise ProjectDatabaseUnavailable() from None
        return await self.get(context)

    async def enter(self, context: ProjectContext, entered_at: datetime) -> ProjectView:
        await self._update_membership(context, {"last_entered_at": entered_at})
        return await self.get(context)

    async def pin(self, context: ProjectContext, pinned: bool) -> ProjectView:
        await self._update_membership(context, {"is_pinned": pinned})
        return await self.get(context)

    async def _update_membership(self, context: ProjectContext, values: dict) -> None:
        project_exists = exists(select(1).where(ProjectRow.id == context.project_id, ProjectRow.status == "active", ProjectRow.is_suspended.is_(False)))
        try:
            result = await self.session.execute(
                update(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.id == context.membership_id,
                    ProjectMembershipRow.project_id == context.project_id,
                    ProjectMembershipRow.user_id == str(context.user_id),
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.version == context.membership_version,
                    project_exists,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                await self.session.rollback()
                raise ProjectNotFound()
            await self.session.commit()
        except ProjectNotFound:
            raise
        except SQLAlchemyError:
            await self.session.rollback()
            raise ProjectDatabaseUnavailable() from None

    async def list_for_user(self, user_id: uuid.UUID, query: str | None, pinned: bool | None, cursor: str | None, limit: int, request_id: str) -> ProjectPage:
        member_count = select(func.count()).where(ProjectMembershipRow.project_id == ProjectRow.id, ProjectMembershipRow.status == "active").correlate(ProjectRow).scalar_subquery()
        statement = (
            select(ProjectRow, ProjectMembershipRow, member_count.label("member_count"))
            .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
            .where(ProjectMembershipRow.user_id == str(user_id), ProjectMembershipRow.status == "active", ProjectRow.status == "active", ProjectRow.is_suspended.is_(False))
        )
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(or_(ProjectRow.display_name.ilike(pattern, escape="\\"), ProjectRow.slug.ilike(pattern, escape="\\")))
        if pinned is not None:
            statement = statement.where(ProjectMembershipRow.is_pinned.is_(pinned))
        if cursor is not None:
            cp, cl, cc, ci = _decode_cursor(cursor)
            tail = or_(ProjectRow.created_at < cc, and_(ProjectRow.created_at == cc, ProjectRow.id < ci))
            last_tail = (
                and_(ProjectMembershipRow.last_entered_at.is_(None), tail)
                if cl is None
                else or_(ProjectMembershipRow.last_entered_at < cl, ProjectMembershipRow.last_entered_at.is_(None), and_(ProjectMembershipRow.last_entered_at == cl, tail))
            )
            pinned_after = ProjectMembershipRow.is_pinned.is_(False) if cp else False
            statement = statement.where(or_(pinned_after, and_(ProjectMembershipRow.is_pinned.is_(cp), last_tail)))
        statement = statement.order_by(ProjectMembershipRow.is_pinned.desc(), ProjectMembershipRow.last_entered_at.desc().nulls_last(), ProjectRow.created_at.desc(), ProjectRow.id.desc()).limit(limit + 1)
        try:
            rows = (await self.session.execute(statement)).all()
        except SQLAlchemyError:
            raise ProjectDatabaseUnavailable() from None
        items = tuple(self._view(row.ProjectRow, row.ProjectMembershipRow, row.member_count, request_id) for row in rows[:limit])
        next_cursor = _encode_cursor(SimpleCursor(rows[limit - 1])) if len(rows) > limit else None
        return ProjectPage(items, next_cursor)


class SimpleCursor:
    def __init__(self, row):
        self.id = row.ProjectRow.id
        self.created_at = row.ProjectRow.created_at
        self.is_pinned = row.ProjectMembershipRow.is_pinned
        self.last_entered_at = row.ProjectMembershipRow.last_entered_at
