from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.gateway.deps import get_config, private_work_context
from app.gateway.private_work_schemas import PrivateWorkRoute
from app.gateway.routers.private_work_routes.contracts import (
    _POSTGRES_BIGINT_MAX,
    PrivateThreadCreateRequest,
    PrivateThreadDeleteResponse,
    PrivateThreadPatchRequest,
    PrivateThreadResponse,
    PrivateThreadSearchRequest,
    PrivateThreadSearchResponse,
    PrivateThreadStateResponse,
    _thread_response,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _browser_chat_run_service,
    _raise_http,
    _scoped_checkpointer,
    _thread_service,
)
from app.gateway.routers.private_work_routes.runs import (
    _project_scoped_checkpoint_durations,
    _project_scoped_checkpoint_token_usage,
    bind_scoped_checkpoint_state,
)
from app.private_work.checkpoint_state import checkpoint_config, snapshot_checkpoint_id
from app.private_work.checkpointer import PRIVATE_SCOPE_MARKER
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkError, PrivateWorkNotFound
from app.private_work.thread_repository import ThreadAgentRef
from deerflow.config.app_config import AppConfig
from deerflow.runtime import serialize_channel_values_for_api
from deerflow.utils.time import coerce_iso

router = APIRouter(route_class=PrivateWorkRoute)


@router.post(
    "/threads",
    response_model=PrivateThreadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_thread(
    body: PrivateThreadCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateThreadResponse:
    try:
        agent = ThreadAgentRef(body.agent_asset_id, body.agent_scope) if body.agent_asset_id is not None and body.agent_scope is not None else None
        record = await _thread_service(request, context.request_id).create(
            context,
            thread_id=str(body.thread_id),
            agent=agent,
            display_name=body.display_name,
            metadata=body.metadata,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _thread_response(record)


@router.post("/threads/search", response_model=PrivateThreadSearchResponse)
async def search_threads(
    body: PrivateThreadSearchRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateThreadSearchResponse:
    try:
        records = await _thread_service(request, context.request_id).search(
            context,
            limit=body.limit,
            offset=body.offset,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadSearchResponse(items=[_thread_response(record) for record in records])


@router.get("/threads/{thread_id}", response_model=PrivateThreadResponse)
async def get_thread(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateThreadResponse:
    try:
        record = await _thread_service(request, context.request_id).get(
            context,
            str(thread_id),
        )
        if record is None:
            raise PrivateWorkNotFound(context.request_id)
    except PrivateWorkError as error:
        _raise_http(error)
    return _thread_response(record)


@router.patch("/threads/{thread_id}", response_model=PrivateThreadResponse)
async def patch_thread(
    thread_id: uuid.UUID,
    body: PrivateThreadPatchRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateThreadResponse:
    try:
        record = await _thread_service(request, context.request_id).patch(
            context,
            str(thread_id),
            expected_version=body.expected_version,
            display_name=body.display_name,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _thread_response(record)


@router.delete(
    "/threads/{thread_id}",
    response_model=PrivateThreadDeleteResponse,
)
async def delete_thread(
    thread_id: uuid.UUID,
    request: Request,
    expected_version: int = Query(
        ge=1,
        le=_POSTGRES_BIGINT_MAX,
        json_schema_extra={
            "format": "int64",
            "x-postgres-bigint-maximum": str(_POSTGRES_BIGINT_MAX),
        },
    ),
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateThreadDeleteResponse:
    try:
        await _thread_service(request, context.request_id).delete(
            context,
            str(thread_id),
            expected_version=expected_version,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadDeleteResponse(success=True)


@router.get(
    "/threads/{thread_id}/state",
    response_model=PrivateThreadStateResponse,
)
async def get_thread_state(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_config),
) -> PrivateThreadStateResponse:
    try:
        await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        snapshot = await bind_scoped_checkpoint_state(
            _scoped_checkpointer(request, context.request_id),
            context,
            config,
            as_node="state",
        ).aget(checkpoint_config(str(thread_id)))
        if snapshot_checkpoint_id(snapshot) is None:
            raise PrivateWorkNotFound(context.request_id)
    except PrivateWorkError as error:
        _raise_http(error)

    metadata = dict(snapshot.metadata or {})
    metadata.pop(PRIVATE_SCOPE_MARKER, None)
    configurable = (snapshot.config or {}).get("configurable", {})
    checkpoint_id_value = configurable.get("checkpoint_id")
    checkpoint_id = str(checkpoint_id_value) if checkpoint_id_value is not None else None
    parent_configurable = (snapshot.parent_config or {}).get("configurable", {})
    parent_id_value = parent_configurable.get("checkpoint_id")
    parent_checkpoint_id = str(parent_id_value) if parent_id_value is not None else None
    raw_tasks = getattr(snapshot, "tasks", ()) or ()
    tasks = [{"id": str(getattr(task, "id", "")), "name": str(getattr(task, "name", ""))} for task in raw_tasks]
    created_at = coerce_iso(metadata.get("created_at", ""))
    values = serialize_channel_values_for_api(dict(snapshot.values or {}))
    try:
        values = await _project_scoped_checkpoint_token_usage(
            request,
            context,
            values,
        )
        values = await _project_scoped_checkpoint_durations(
            request,
            context,
            str(thread_id),
            values,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadStateResponse(
        values=values,
        next=[task["name"] for task in tasks if task["name"]],
        metadata=metadata,
        checkpoint={"id": checkpoint_id, "ts": created_at},
        checkpoint_id=checkpoint_id,
        parent_checkpoint_id=parent_checkpoint_id,
        created_at=created_at,
        tasks=tasks,
    )
