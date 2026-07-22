from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.asset_summary import project_asset_summary_columns
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound, ProjectSlugConflict, ProjectValidationFailed
from app.projects.models import CreateProject, ProjectChanges, ProjectPage, ProjectQuotaSummary, ProjectRole, ProjectView
from app.projects.quota_summary import project_quota_summary_columns, project_quota_summary_from_row
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow

if TYPE_CHECKING:
    from app.private_work.context import PrivateWorkContext


class ProjectCreateQuotaPort(Protocol):
    async def reserve_member(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        membership_id: uuid.UUID,
        membership_version: int,
    ) -> None: ...


class ProjectMutationAuditPort(Protocol):
    async def project_created(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None: ...

    async def project_updated(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None: ...


class _NoopProjectCreateQuota:
    async def reserve_member(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        membership_id: uuid.UUID,
        membership_version: int,
    ) -> None:
        del session, context, membership_id, membership_version


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
    def __init__(
        self,
        session: AsyncSession,
        *,
        quota: ProjectCreateQuotaPort | None = None,
        quota_config: QuotaConfig | None = None,
    ):
        self.session = session
        self._quota = quota or _NoopProjectCreateQuota()
        self._quota_config = quota_config or QuotaConfig()

    async def create_with_admin(
        self,
        user_id: uuid.UUID,
        command: CreateProject,
        request_id: str,
        *,
        audit: ProjectMutationAuditPort | None = None,
    ) -> ProjectContext:
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
                context = ProjectContext(
                    user_id,
                    project.id,
                    membership.id,
                    ProjectRole.ADMIN,
                    capabilities_for(ProjectRole.ADMIN),
                    membership.version,
                    request_id,
                )
                from app.private_work.context import PrivateWorkContext

                await self._quota.reserve_member(
                    self.session,
                    PrivateWorkContext.from_project(context),
                    membership_id=membership.id,
                    membership_version=membership.version,
                )
                if audit is not None:
                    await audit.project_created(self.session, context)
        except IntegrityError as exc:
            if _constraint_name(exc) == "uq_projects_slug":
                raise ProjectSlugConflict() from None
            raise ProjectDatabaseUnavailable() from None
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None
        return context

    def _scope(self, context: ProjectContext):
        if not isinstance(context, ProjectContext):
            raise ProjectNotFound()
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
        try:
            async with self.session.begin():
                return await self._get_in_transaction(context)
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def _get_in_transaction(self, context: ProjectContext) -> ProjectView:
        member_count = select(func.count()).where(ProjectMembershipRow.project_id == ProjectRow.id, ProjectMembershipRow.status == "active").correlate(ProjectRow).scalar_subquery()
        asset_counts = project_asset_summary_columns(ProjectRow.id)
        quota_summary = project_quota_summary_columns(ProjectRow.id, self._quota_config)
        statement = select(ProjectRow, ProjectMembershipRow, member_count.label("member_count"), *asset_counts, *quota_summary).join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id).where(self._scope(context))
        rows = (await self.session.execute(statement)).all()
        if len(rows) != 1:
            raise ProjectNotFound()
        row = rows[0]
        return self._view(
            row.ProjectRow,
            row.ProjectMembershipRow,
            row.member_count,
            row.agent_count,
            row.skill_count,
            row.mcp_count,
            project_quota_summary_from_row(row),
            context.request_id,
        )

    def _view(
        self,
        project,
        membership,
        member_count: int,
        agent_count: int,
        skill_count: int,
        mcp_count: int,
        quota_summary: ProjectQuotaSummary,
        request_id: str,
    ) -> ProjectView:
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
            agent_count,
            skill_count,
            mcp_count,
            quota_summary,
            project.status,
            project.is_suspended,
            membership.version,
            request_id,
            project.deletion_effective_at,
        )

    async def update(
        self,
        context: ProjectContext,
        changes: ProjectChanges,
        *,
        audit: ProjectMutationAuditPort | None = None,
    ) -> ProjectView:
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
            async with self.session.begin():
                result = await self.session.execute(update(ProjectRow).where(ProjectRow.id == context.project_id, ProjectRow.status == "active", ProjectRow.is_suspended.is_(False), membership_scope).values(**values))
                if result.rowcount != 1:
                    raise ProjectNotFound()
                view = await self._get_in_transaction(context)
                if audit is not None:
                    await audit.project_updated(self.session, context)
                return view
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def enter(self, context: ProjectContext, entered_at: datetime) -> ProjectView:
        return await self._mutate_membership(context, {"last_entered_at": entered_at})

    async def pin(self, context: ProjectContext, pinned: bool) -> ProjectView:
        return await self._mutate_membership(context, {"is_pinned": pinned})

    async def _mutate_membership(self, context: ProjectContext, values: dict) -> ProjectView:
        project_exists = exists(select(1).where(ProjectRow.id == context.project_id, ProjectRow.status == "active", ProjectRow.is_suspended.is_(False)))
        try:
            async with self.session.begin():
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
                    raise ProjectNotFound()
                return await self._get_in_transaction(context)
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        query: str | None,
        pinned: bool | None,
        cursor: str | None,
        limit: int,
        request_id: str,
        include_recoverable: bool = False,
    ) -> ProjectPage:
        try:
            async with self.session.begin():
                return await self._list_in_transaction(
                    user_id,
                    query,
                    pinned,
                    cursor,
                    limit,
                    request_id,
                    include_recoverable,
                )
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def _list_in_transaction(
        self,
        user_id: uuid.UUID,
        query: str | None,
        pinned: bool | None,
        cursor: str | None,
        limit: int,
        request_id: str,
        include_recoverable: bool,
    ) -> ProjectPage:
        member_count = select(func.count()).where(ProjectMembershipRow.project_id == ProjectRow.id, ProjectMembershipRow.status == "active").correlate(ProjectRow).scalar_subquery()
        asset_counts = project_asset_summary_columns(ProjectRow.id)
        quota_summary = project_quota_summary_columns(ProjectRow.id, self._quota_config)
        project_visibility = ProjectRow.status == "active"
        if include_recoverable:
            project_visibility = or_(
                project_visibility,
                and_(
                    ProjectRow.status == "pending_deletion",
                    ProjectRow.deletion_effective_at.is_not(None),
                    ProjectRow.deletion_effective_at > func.now(),
                    ProjectMembershipRow.role == ProjectRole.ADMIN.value,
                ),
            )
        statement = (
            select(ProjectRow, ProjectMembershipRow, member_count.label("member_count"), *asset_counts, *quota_summary)
            .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
            .where(
                ProjectMembershipRow.user_id == str(user_id),
                ProjectMembershipRow.status == "active",
                project_visibility,
                ProjectRow.is_suspended.is_(False),
            )
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
        rows = (await self.session.execute(statement)).all()
        items = tuple(
            self._view(
                row.ProjectRow,
                row.ProjectMembershipRow,
                row.member_count,
                row.agent_count,
                row.skill_count,
                row.mcp_count,
                project_quota_summary_from_row(row),
                request_id,
            )
            for row in rows[:limit]
        )
        next_cursor = _encode_cursor(SimpleCursor(rows[limit - 1])) if len(rows) > limit else None
        return ProjectPage(items, next_cursor)


class SimpleCursor:
    def __init__(self, row):
        self.id = row.ProjectRow.id
        self.created_at = row.ProjectRow.created_at
        self.is_pinned = row.ProjectMembershipRow.is_pinned
        self.last_entered_at = row.ProjectMembershipRow.last_entered_at
