from __future__ import annotations

import uuid
from datetime import UTC, datetime
from functools import wraps
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.error_mapping import automation_http_exception
from app.automations.errors import AutomationError
from app.gateway.automation_schemas import (
    AutomationCreateRequest,
    AutomationDeleteResponse,
    AutomationListQuery,
    AutomationListResponse,
    AutomationPatchRequest,
    AutomationReadinessResponse,
    AutomationResponse,
    AutomationRoute,
    AutomationRunListResponse,
    AutomationRunResponse,
    AutomationVersionRequest,
)
from app.gateway.deps import (
    automation_context,
    get_automation_dispatcher,
    get_automation_occurrence_service,
    get_automation_readiness_service,
    get_automation_scheduler_enabled,
    get_automation_service,
    project_session,
    require_project_automation_open,
)
from app.private_work.context import PrivateWorkContext


def _map_automation_errors(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except AutomationError as error:
            raise automation_http_exception(error) from None

    return wrapped


readiness_router = APIRouter(
    prefix="/api/projects/{project_id}/automations",
    tags=["project-automations"],
    route_class=AutomationRoute,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/automations",
    tags=["project-automations"],
    route_class=AutomationRoute,
    dependencies=[Depends(require_project_automation_open)],
)


@readiness_router.get(
    "/readiness",
    response_model=AutomationReadinessResponse,
)
@_map_automation_errors
async def get_automation_readiness(
    context: PrivateWorkContext = Depends(automation_context),
    session: AsyncSession = Depends(project_session),
    service=Depends(get_automation_readiness_service),
    scheduler_enabled: bool = Depends(get_automation_scheduler_enabled),
) -> AutomationReadinessResponse:
    result = await service.read(session, context, scheduler_enabled)
    return AutomationReadinessResponse(
        status=result.status,
        code=result.code,
        scheduler_enabled=result.scheduler_enabled,
        scheduler_status=result.scheduler_status,
        project_private_work_ready=result.project_private_work_ready,
        automation_cutover_ready=result.automation_cutover_ready,
        request_id=result.request_id,
    )


@router.get("", response_model=AutomationListResponse)
@_map_automation_errors
async def list_automations(
    query: Annotated[AutomationListQuery, Query()],
    context: PrivateWorkContext = Depends(automation_context),
    service=Depends(get_automation_service),
) -> AutomationListResponse:
    values = await service.list(
        context,
        limit=query.limit,
        offset=query.offset,
    )
    return AutomationListResponse(items=[AutomationResponse.from_view(value) for value in values])


@router.post(
    "",
    response_model=AutomationResponse,
    status_code=status.HTTP_201_CREATED,
)
@_map_automation_errors
async def create_automation(
    body: AutomationCreateRequest,
    context: PrivateWorkContext = Depends(automation_context),
    service=Depends(get_automation_service),
) -> AutomationResponse:
    value = await service.create(context, body.to_command())
    return AutomationResponse.from_view(value)


# Register this static prefix before the task-id routes. It remains a distinct
# collection endpoint and can never be interpreted as task_id="threads".
@router.get(
    "/threads/{thread_id}",
    response_model=AutomationListResponse,
)
@_map_automation_errors
async def list_thread_automations(
    thread_id: uuid.UUID,
    query: Annotated[AutomationListQuery, Query()],
    context: PrivateWorkContext = Depends(automation_context),
    service=Depends(get_automation_service),
) -> AutomationListResponse:
    values = await service.list(
        context,
        limit=query.limit,
        offset=query.offset,
        thread_id=str(thread_id),
    )
    return AutomationListResponse(items=[AutomationResponse.from_view(value) for value in values])


@router.get("/{task_id}", response_model=AutomationResponse)
@_map_automation_errors
async def get_automation(
    task_id: str,
    context: PrivateWorkContext = Depends(automation_context),
    service=Depends(get_automation_service),
) -> AutomationResponse:
    return AutomationResponse.from_view(await service.get(context, task_id))


@router.patch("/{task_id}", response_model=AutomationResponse)
@_map_automation_errors
async def patch_automation(
    task_id: str,
    body: AutomationPatchRequest,
    context: PrivateWorkContext = Depends(automation_context),
    service=Depends(get_automation_service),
) -> AutomationResponse:
    return AutomationResponse.from_view(await service.update(context, task_id, body.to_changes()))


@router.delete("/{task_id}", response_model=AutomationDeleteResponse)
@_map_automation_errors
async def delete_automation(
    task_id: str,
    body: Annotated[AutomationVersionRequest, Body()],
    context: PrivateWorkContext = Depends(automation_context),
    service=Depends(get_automation_service),
) -> AutomationDeleteResponse:
    await service.delete(context, task_id, body.expected_version)
    return AutomationDeleteResponse(id=task_id, deleted=True)


@router.post("/{task_id}/pause", response_model=AutomationResponse)
@_map_automation_errors
async def pause_automation(
    task_id: str,
    body: AutomationVersionRequest,
    context: PrivateWorkContext = Depends(automation_context),
    service=Depends(get_automation_service),
) -> AutomationResponse:
    return AutomationResponse.from_view(await service.pause(context, task_id, body.expected_version))


@router.post("/{task_id}/resume", response_model=AutomationResponse)
@_map_automation_errors
async def resume_automation(
    task_id: str,
    body: AutomationVersionRequest,
    context: PrivateWorkContext = Depends(automation_context),
    service=Depends(get_automation_service),
) -> AutomationResponse:
    return AutomationResponse.from_view(await service.resume(context, task_id, body.expected_version))


@router.post("/{task_id}/trigger", response_model=AutomationRunResponse)
@_map_automation_errors
async def trigger_automation(
    task_id: str,
    idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
    context: PrivateWorkContext = Depends(automation_context),
    occurrences=Depends(get_automation_occurrence_service),
    dispatcher=Depends(get_automation_dispatcher),
) -> AutomationRunResponse:
    now = datetime.now(UTC)
    admitted = await dispatcher.admit_manual(
        context,
        task_id,
        idempotency_key,
        scheduled_for=now,
    )
    return AutomationRunResponse.from_view(await occurrences.get(context, admitted.occurrence.id))


@router.get("/{task_id}/runs", response_model=AutomationRunListResponse)
@_map_automation_errors
async def list_automation_runs(
    task_id: str,
    query: Annotated[AutomationListQuery, Query()],
    context: PrivateWorkContext = Depends(automation_context),
    occurrences=Depends(get_automation_occurrence_service),
) -> AutomationRunListResponse:
    values = await occurrences.list(
        context,
        task_id,
        limit=query.limit,
        offset=query.offset,
    )
    return AutomationRunListResponse(items=[AutomationRunResponse.from_view(value) for value in values])


__all__ = ["readiness_router", "router"]
