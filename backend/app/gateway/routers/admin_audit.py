from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditPage, AuditRecord
from app.gateway.deps import get_project_audit_service, project_session
from app.gateway.routers.admin_operations import (
    AdminOperationsRoute,
    authenticated_system_identity,
    current_system_context,
    map_admin_operations_errors,
)
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.user import UserRow

router = APIRouter(
    prefix="/api/admin/audit",
    tags=["admin-audit"],
    route_class=AdminOperationsRoute,
)


class AdminAuditItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: uuid.UUID
    occurred_at: str
    actor: str
    actor_user_id: str | None
    actor_email: str | None = Field(default=None, max_length=320)
    action: str
    target_kind: str
    outcome: str
    public_error_code: str | None
    metadata: dict[str, object]
    project_id: uuid.UUID | None
    project_slug: str | None = Field(default=None, min_length=3, max_length=63)
    project_display_name: str | None = Field(default=None, min_length=1, max_length=120)


class AdminAuditPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[AdminAuditItemResponse]
    next_cursor: str | None


def _actor(record: AuditRecord) -> str:
    if record.actor_process is not None:
        return record.actor_process.value
    if record.actor_platform_role is not None:
        return record.actor_platform_role.value
    return "user"


def _response(
    page: AuditPage,
    projects: dict[uuid.UUID, tuple[str, str]],
    actors: dict[str, str | None],
) -> AdminAuditPageResponse:
    items: list[AdminAuditItemResponse] = []
    for item in page.items:
        project = None if item.project_id is None else projects.get(item.project_id)
        actor_user_id = None if item.actor_user_id is None else str(item.actor_user_id)
        items.append(
            AdminAuditItemResponse(
                id=item.id,
                occurred_at=item.occurred_at.isoformat(),
                actor=_actor(item),
                actor_user_id=actor_user_id,
                actor_email=None if actor_user_id is None else actors.get(actor_user_id),
                action=item.action.value,
                target_kind=item.target_kind.value,
                outcome=item.outcome.value,
                public_error_code=item.public_error_code,
                metadata=item.metadata,
                project_id=item.project_id,
                project_slug=None if project is None else project[0],
                project_display_name=None if project is None else project[1],
            )
        )
    return AdminAuditPageResponse(items=items, next_cursor=page.next_cursor)


@router.get("", response_model=AdminAuditPageResponse)
@map_admin_operations_errors
async def list_admin_audit(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    project_id: uuid.UUID | None = None,
    project_query: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    platform_only: bool = False,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_project_audit_service),
) -> AdminAuditPageResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        page = await audit.list_platform(
            session,
            context,
            limit=limit,
            cursor=cursor,
            project_id=project_id,
            project_query=project_query,
            platform_only=platform_only,
        )
        project_ids = {item.project_id for item in page.items if item.project_id is not None}
        projects: dict[uuid.UUID, tuple[str, str]] = {}
        if project_ids:
            rows = (await session.execute(select(ProjectRow.id, ProjectRow.slug, ProjectRow.display_name).where(ProjectRow.id.in_(project_ids)))).all()
            projects = {row.id: (row.slug, row.display_name) for row in rows}
        actor_ids = {str(item.actor_user_id) for item in page.items if item.actor_user_id is not None}
        actors: dict[str, str | None] = {}
        if actor_ids:
            rows = (await session.execute(select(UserRow.id, UserRow.email).where(UserRow.id.in_(actor_ids)))).all()
            actors = {row.id: row.email for row in rows}
        return _response(page, projects, actors)


__all__ = ["router"]
