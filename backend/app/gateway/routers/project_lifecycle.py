from __future__ import annotations

import uuid
from datetime import UTC, datetime
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import (
    get_operational_audit_sink,
    get_run_manager,
    project_session,
)
from app.gateway.routers.project_governance import (
    GOVERNANCE_DOMAIN_ERRORS,
    GovernanceRoute,
    raise_governance_error,
)
from app.gateway.routers.projects import (
    ProjectResponse,
    _response,
    authenticated_project_identity,
)
from app.private_work.authorization import notify_local_cancellation
from app.projects.context import resolve_project_context
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.lifecycle_service import ProjectLifecycleService

router = APIRouter(
    prefix="/api/projects",
    tags=["project-lifecycle"],
    route_class=GovernanceRoute,
)


def _cancellation_notifier(request: Request):
    try:
        run_manager = get_run_manager(request)
    except (RuntimeError, HTTPException):
        return None
    return partial(notify_local_cancellation, run_manager=run_manager)


@router.post("/{project_id}/deletion", response_model=ProjectResponse)
async def request_project_deletion(
    request: Request,
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_operational_audit_sink),
):
    try:
        context = await resolve_project_context(
            session,
            identity[0],
            project_id,
            identity[1],
        )
        view = await ProjectLifecycleService(
            ProjectLifecycleRepository(session),
            notify_local_cancellation=_cancellation_notifier(request),
            audit=audit,
        ).request_deletion(context, datetime.now(UTC))
        return _response(view)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])


@router.post("/{project_id}/restore", response_model=ProjectResponse)
async def restore_project(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_operational_audit_sink),
):
    try:
        view = await ProjectLifecycleService(
            ProjectLifecycleRepository(session),
            audit=audit,
        ).restore(identity[0], project_id, identity[1], datetime.now(UTC))
        return _response(view)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])
