from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.sinks import SystemJobAuditSink
from app.gateway.deps import get_project_audit_service, project_session
from app.gateway.routers.admin_operations import (
    AdminOperationsRoute,
    authenticated_system_identity,
    current_system_context,
    map_admin_operations_errors,
)
from app.reliability.operations import AdminJobRecord, SystemOperationsRepository
from deerflow.persistence.jobs.sql import JobRepository

router = APIRouter(
    prefix="/api/admin/jobs",
    tags=["admin-jobs"],
    route_class=AdminOperationsRoute,
)


class AdminJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: uuid.UUID
    dead_job_id: uuid.UUID | None
    project_id: uuid.UUID
    project_slug: str = Field(min_length=3, max_length=63, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    project_display_name: str = Field(min_length=1, max_length=120)
    job_type: Literal[
        "private_run",
        "automation_run",
        "retention_purge",
        "mcp_discovery",
    ]
    status: Literal[
        "queued",
        "leased",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
        "dead",
    ]
    retry_safety: Literal["safe", "unknown", "unsafe"]
    safe_to_requeue: bool
    public_error_code: str | None
    predecessor_dead_job_id: uuid.UUID | None


class AdminJobPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[AdminJobResponse]
    next_cursor: str | None


class RequeueJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: uuid.UUID = Field(strict=False)
    dead_job_id: uuid.UUID = Field(strict=False)
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_attempts: int = Field(ge=1, le=20)


class RequeueJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: uuid.UUID
    project_id: uuid.UUID
    status: Literal["queued"]
    retry_safety: Literal["safe"]
    attempt_count: Literal[0]
    predecessor_dead_job_id: uuid.UUID


def _response(item: AdminJobRecord) -> AdminJobResponse:
    return AdminJobResponse(
        job_id=item.job_id,
        dead_job_id=item.dead_job_id,
        project_id=item.project_id,
        project_slug=item.project_slug,
        project_display_name=item.project_display_name,
        job_type=item.job_type,
        status=item.status,
        retry_safety=item.retry_safety,
        safe_to_requeue=item.safe_to_requeue,
        public_error_code=item.public_error_code,
        predecessor_dead_job_id=item.predecessor_dead_job_id,
    )


@router.get("", response_model=AdminJobPageResponse)
@map_admin_operations_errors
async def list_admin_jobs(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    project_id: uuid.UUID | None = None,
    project_query: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    status: Literal[
        "queued",
        "leased",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
        "dead",
    ]
    | None = None,
    type: Literal[
        "private_run",
        "automation_run",
        "retention_purge",
        "mcp_discovery",
    ]
    | None = None,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
) -> AdminJobPageResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        page = await SystemOperationsRepository(session).list_jobs(
            limit=limit,
            cursor=cursor,
            project_id=project_id,
            project_query=project_query,
            status=status,
            job_type=type,
            request_id=context.request_id,
        )
        return AdminJobPageResponse(
            items=[_response(item) for item in page.items],
            next_cursor=page.next_cursor,
        )


@router.post(
    "/requeue",
    response_model=RequeueJobResponse,
    status_code=status.HTTP_201_CREATED,
)
@map_admin_operations_errors
async def requeue_safe_admin_job(
    body: RequeueJobRequest,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_project_audit_service),
) -> RequeueJobResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        successor_id = await JobRepository(session).requeue_safe_system(
            body.project_id,
            body.dead_job_id,
            idempotency_key=body.idempotency_key,
            max_attempts=body.max_attempts,
            request_id=identity[1],
            audit_port=SystemJobAuditSink(audit, context),
        )
        successor = await SystemOperationsRepository(session).public_job(
            project_id=body.project_id,
            job_id=successor_id,
        )
        if successor is None or successor.project_id != body.project_id or successor.status != "queued" or successor.retry_safety != "safe" or successor.predecessor_dead_job_id != body.dead_job_id:
            raise ValueError("safe requeue result is invalid")
        return RequeueJobResponse(
            job_id=successor.job_id,
            project_id=successor.project_id,
            status="queued",
            retry_safety="safe",
            attempt_count=0,
            predecessor_dead_job_id=body.dead_job_id,
        )


__all__ = ["router"]
