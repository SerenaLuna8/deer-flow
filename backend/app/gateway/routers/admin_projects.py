from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.sinks import SystemProjectLifecycleAuditSink
from app.gateway.deps import (
    get_project_audit_service,
    get_project_quota_service,
    project_session,
)
from app.gateway.routers.admin_operations import (
    AdminOperationsRoute,
    authenticated_system_identity,
    current_system_context,
    map_admin_operations_errors,
)
from app.gateway.routers.project_usage import (
    ProjectUsageResponse,
    QuotaPolicyResponse,
    QuotaPolicyUpdateRequest,
    _policy_response,
    _usage_response,
)
from app.projects.system_lifecycle import SystemProjectLifecycleService
from app.quotas.models import (
    ProjectQuotaLimits,
    QuotaConflict,
    QuotaForbidden,
    QuotaPolicyInvalid,
)
from app.reliability.errors import (
    ReliabilityConflict,
    ReliabilityInvalid,
    ReliabilityNotFound,
)
from app.reliability.operations import SystemOperationsRepository

router = APIRouter(
    prefix="/api/admin/projects",
    tags=["admin-projects"],
    route_class=AdminOperationsRoute,
)


class AdminProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: uuid.UUID
    slug: str
    display_name: str
    status: Literal["active", "pending_deletion"]
    is_suspended: bool
    state_version: int
    created_at: datetime
    updated_at: datetime
    deletion_effective_at: datetime | None


class AdminProjectPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[AdminProjectResponse]
    next_cursor: str | None


def _project_response(item) -> AdminProjectResponse:
    return AdminProjectResponse(
        project_id=item.project_id,
        slug=item.slug,
        display_name=item.display_name,
        status=item.status,
        is_suspended=item.is_suspended,
        state_version=item.state_version,
        created_at=item.created_at,
        updated_at=item.updated_at,
        deletion_effective_at=item.deletion_effective_at,
    )


@router.get("", response_model=AdminProjectPageResponse)
@map_admin_operations_errors
async def list_admin_projects(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    query: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    status: Literal["active", "pending_deletion"] | None = None,
    suspended: bool | None = None,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
) -> AdminProjectPageResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        page = await SystemOperationsRepository(session).list_projects(
            limit=limit,
            cursor=cursor,
            query=query,
            status=status,
            suspended=suspended,
            request_id=context.request_id,
        )
        return AdminProjectPageResponse(
            items=[_project_response(item) for item in page.items],
            next_cursor=page.next_cursor,
        )


@router.get("/{project_id}", response_model=AdminProjectResponse)
@map_admin_operations_errors
async def get_admin_project(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
) -> AdminProjectResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        item = await SystemOperationsRepository(session).get_project(
            project_id,
            request_id=context.request_id,
        )
        return _project_response(item)


@router.post("/{project_id}/suspend", response_model=AdminProjectResponse)
@map_admin_operations_errors
async def suspend_admin_project(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_project_audit_service),
) -> AdminProjectResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        item = await SystemProjectLifecycleService(
            session,
            audit=SystemProjectLifecycleAuditSink(audit, context),
        ).suspend(context, project_id, now=datetime.now(UTC))
        return _project_response(item)


@router.post("/{project_id}/resume", response_model=AdminProjectResponse)
@map_admin_operations_errors
async def resume_admin_project(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_project_audit_service),
) -> AdminProjectResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        item = await SystemProjectLifecycleService(
            session,
            audit=SystemProjectLifecycleAuditSink(audit, context),
        ).resume(context, project_id, now=datetime.now(UTC))
        return _project_response(item)


@router.get("/{project_id}/usage", response_model=ProjectUsageResponse)
@map_admin_operations_errors
async def get_admin_project_usage(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
    quotas=Depends(get_project_quota_service),
) -> ProjectUsageResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        try:
            usage = await quotas.read_usage_as_system_admin(
                session,
                context,
                project_id,
            )
        except QuotaForbidden:
            raise ReliabilityNotFound(identity[1]) from None
        return _usage_response(usage)


@router.patch("/{project_id}/usage/limits", response_model=QuotaPolicyResponse)
@map_admin_operations_errors
async def update_admin_project_quota_limits(
    project_id: uuid.UUID,
    body: QuotaPolicyUpdateRequest,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
    quotas=Depends(get_project_quota_service),
    audit=Depends(get_project_audit_service),
) -> QuotaPolicyResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        try:
            policy = await quotas.set_limits_as_system_admin(
                session,
                context,
                project_id,
                ProjectQuotaLimits(**body.limits.model_dump()),
                expected_version=body.expected_version,
            )
        except QuotaForbidden:
            raise ReliabilityNotFound(identity[1]) from None
        except QuotaConflict:
            raise ReliabilityConflict(identity[1]) from None
        except QuotaPolicyInvalid:
            raise ReliabilityInvalid(identity[1]) from None
        await SystemProjectLifecycleAuditSink(audit, context).quota_policy_updated(
            session,
            project_id=project_id,
            policy=policy,
        )
        return _policy_response(policy)


__all__ = ["router"]
