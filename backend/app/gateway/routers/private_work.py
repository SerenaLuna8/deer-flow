from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import (
    get_config,
    private_work_context,
    project_session,
    require_project_private_open,
)
from app.gateway.private_work_schemas import (
    PrivateRunCreateRequest as PrivateRunCreateRequest,
)
from app.gateway.private_work_schemas import (
    PrivateThreadTokenUsageResponse as PrivateThreadTokenUsageResponse,
)
from app.gateway.private_work_schemas import (
    PrivateWorkRoute,
)
from app.gateway.routers.private_work_routes import (
    approvals,
    context_controls,
    files,
    runs,
)
from app.gateway.routers.private_work_routes.context_controls import (
    _context_projection_sse_consumer as _context_projection_sse_consumer,
)
from app.gateway.routers.private_work_routes.context_controls import (
    _normalize_prepared_edit_replay as _normalize_prepared_edit_replay,
)
from app.gateway.routers.private_work_routes.context_controls import (
    _prepared_history_replay_kind as _prepared_history_replay_kind,
)
from app.gateway.routers.private_work_routes.context_controls import (
    stream_private_thread_context_usage as stream_private_thread_context_usage,
)
from app.gateway.routers.private_work_routes.contracts import (
    _POSTGRES_BIGINT_MAX,
    PRIVATE_THREAD_TITLE_MAX_LENGTH,  # noqa: F401
    PrivateFeedbackCreateRequest,
    PrivateFeedbackResponse,
    PrivateThreadCreateRequest,
    PrivateThreadDeleteResponse,
    PrivateThreadPatchRequest,
    PrivateThreadResponse,
    PrivateThreadSearchRequest,
    PrivateThreadSearchResponse,
    PrivateThreadStateResponse,
    PrivateWorkReadinessResponse,
    _feedback_response,
    _thread_response,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalApprovedResponse as ExecutionApprovalApprovedResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalBaseResponse as ExecutionApprovalBaseResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalClaimedResponse as ExecutionApprovalClaimedResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalClosedResponse as ExecutionApprovalClosedResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalContinuationRunResponse as ExecutionApprovalContinuationRunResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalDecisionRequest as ExecutionApprovalDecisionRequest,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalDeniedResponse as ExecutionApprovalDeniedResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalDomainResponse as ExecutionApprovalDomainResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalEnvelopeResponse as ExecutionApprovalEnvelopeResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalFinishedResponse as ExecutionApprovalFinishedResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalLaunchFailedResponse as ExecutionApprovalLaunchFailedResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalPendingResponse as ExecutionApprovalPendingResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalResponse as ExecutionApprovalResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalSourceAgentResponse as ExecutionApprovalSourceAgentResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalUnknownResponse as ExecutionApprovalUnknownResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateCompactKeep as PrivateCompactKeep,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateEditRegeneratePrepareRequest as PrivateEditRegeneratePrepareRequest,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateEditRegeneratePrepareResponse as PrivateEditRegeneratePrepareResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateFileDeleteResponse as PrivateFileDeleteResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateFileResponse as PrivateFileResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateRegeneratePrepareRequest as PrivateRegeneratePrepareRequest,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateRegeneratePrepareResponse as PrivateRegeneratePrepareResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateRunDeleteResponse as PrivateRunDeleteResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateRunExecutionProfileResponse as PrivateRunExecutionProfileResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateRunExecutionStateResponse as PrivateRunExecutionStateResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateRunMessageResponse as PrivateRunMessageResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateRunMessagesPageResponse as PrivateRunMessagesPageResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateRunResponse as PrivateRunResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateSuggestionsRequest as PrivateSuggestionsRequest,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateSuggestionsResponse as PrivateSuggestionsResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateThreadBranchRequest as PrivateThreadBranchRequest,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateThreadBranchResponse as PrivateThreadBranchResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateThreadCompactRequest as PrivateThreadCompactRequest,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateThreadCompactResponse as PrivateThreadCompactResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateThreadGoalRequest as PrivateThreadGoalRequest,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateThreadGoalResponse as PrivateThreadGoalResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateUploadLimitsResponse as PrivateUploadLimitsResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateUploadProjectStorageResponse as PrivateUploadProjectStorageResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    _execution_approval_response as _execution_approval_response,
)
from app.gateway.routers.private_work_routes.contracts import (
    _file_response as _file_response,
)
from app.gateway.routers.private_work_routes.contracts import (
    _public_run_metadata as _public_run_metadata,
)
from app.gateway.routers.private_work_routes.contracts import (
    _run_response as _run_response,
)
from app.gateway.routers.private_work_routes.contracts import (
    _timestamp as _timestamp,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _browser_chat_run_service,
    _feedback_service,
    _raise_http,
    _scoped_checkpointer,
    _thread_service,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _chat_control_service as _chat_control_service,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _execution_approval_service as _execution_approval_service,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _file_service as _file_service,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _file_streamer as _file_streamer,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _run_event_store as _run_event_store,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _run_service as _run_service,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _run_store as _run_store,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _runtime_dependency as _runtime_dependency,
)
from app.gateway.routers.private_work_routes.runs import (
    _prepend_admitted_human_input_response as _prepend_admitted_human_input_response,
)
from app.gateway.routers.private_work_routes.runs import (
    _project_scoped_checkpoint_durations as _project_scoped_checkpoint_durations,
)
from app.gateway.routers.private_work_routes.runs import (
    _project_scoped_checkpoint_token_usage as _project_scoped_checkpoint_token_usage,
)
from app.gateway.routers.private_work_routes.runs import (
    _project_scoped_event_durations as _project_scoped_event_durations,
)
from app.gateway.routers.private_work_routes.runs import (
    _project_scoped_event_token_usage as _project_scoped_event_token_usage,
)
from app.gateway.routers.private_work_routes.runs import (
    bind_scoped_checkpoint_state as bind_scoped_checkpoint_state,
)
from app.gateway.routers.private_work_routes.runs import (
    list_private_run_events as list_private_run_events,
)
from app.gateway.routers.private_work_routes.runs import (
    list_private_run_messages as list_private_run_messages,
)
from app.gateway.routers.private_work_routes.runs import (
    list_private_thread_messages as list_private_thread_messages,
)
from app.gateway.routers.private_work_routes.runs import (
    private_thread_token_usage as private_thread_token_usage,
)
from app.gateway.routers.private_work_routes.runs import (
    reconnect_private_run_stream as reconnect_private_run_stream,
)
from app.gateway.routers.private_work_routes.runs import (
    start_private_run as start_private_run,
)
from app.gateway.routers.private_work_routes.runs import (
    wait_private_run as wait_private_run,
)
from app.gateway.routers.private_work_routes.streaming import (
    _PRIVATE_STREAM_HEARTBEAT_SECONDS as _PRIVATE_STREAM_HEARTBEAT_SECONDS,
)
from app.gateway.routers.private_work_routes.streaming import (
    _PRIVATE_STREAM_POLL_SECONDS as _PRIVATE_STREAM_POLL_SECONDS,
)
from app.gateway.routers.private_work_routes.streaming import (
    _PRIVATE_STREAM_WAKEUP_WAIT_SECONDS as _PRIVATE_STREAM_WAKEUP_WAIT_SECONDS,
)
from app.gateway.routers.private_work_routes.streaming import (
    _durable_private_sse_consumer as _durable_private_sse_consumer,
)
from app.gateway.routers.private_work_routes.streaming import (
    _private_stream_bridge as _private_stream_bridge,
)
from app.gateway.routers.private_work_routes.streaming import (
    _private_stream_cursor as _private_stream_cursor,
)
from app.gateway.routers.private_work_routes.streaming import (
    _read_private_stream_page as _read_private_stream_page,
)
from app.gateway.routers.private_work_routes.streaming import (
    _require_run_runtime as _require_run_runtime,
)
from app.gateway.routers.private_work_routes.streaming import (
    _run_event_wakeup as _run_event_wakeup,
)
from app.gateway.routers.private_work_routes.streaming import (
    _wait_for_durable_private_run as _wait_for_durable_private_run,
)
from app.private_work.checkpoint_state import (
    checkpoint_config,
    snapshot_checkpoint_id,
)
from app.private_work.checkpointer import (
    PRIVATE_SCOPE_MARKER,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkError, PrivateWorkNotFound
from app.private_work.readiness_service import (
    PrivateWorkReadinessService,
)
from app.private_work.thread_repository import ThreadAgentRef
from deerflow.config.app_config import AppConfig
from deerflow.runtime import serialize_channel_values_for_api
from deerflow.utils.time import coerce_iso

router = APIRouter(
    prefix="/api/projects/{project_id}/private-work",
    tags=["project-private-work"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)


@router.get("/readiness", response_model=PrivateWorkReadinessResponse)
async def get_private_work_readiness(
    context: PrivateWorkContext = Depends(private_work_context),
    session: AsyncSession = Depends(project_session),
) -> PrivateWorkReadinessResponse:
    result = await PrivateWorkReadinessService().read(session, context)
    return PrivateWorkReadinessResponse(
        status=result.status,
        code=result.code,
        request_id=result.request_id,
    )


router.include_router(context_controls.router)
router.include_router(files.router)
router.include_router(approvals.router)
router.include_router(runs.router)


@router.get(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    response_model=PrivateFeedbackResponse | None,
)
async def get_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFeedbackResponse | None:
    try:
        record = await _feedback_service(
            request,
            context.request_id,
        ).get(
            context,
            thread_id=str(thread_id),
            run_id=str(run_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return None if record is None else _feedback_response(record)


async def _upsert_private_feedback(
    *,
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    body: PrivateFeedbackCreateRequest,
    request: Request,
    context: PrivateWorkContext,
) -> PrivateFeedbackResponse:
    try:
        record = await _feedback_service(
            request,
            context.request_id,
        ).upsert(
            context,
            thread_id=str(thread_id),
            run_id=str(run_id),
            rating=body.rating,
            message_id=body.message_id,
            comment=body.comment,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _feedback_response(record)


@router.put(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    response_model=PrivateFeedbackResponse,
)
async def upsert_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    body: PrivateFeedbackCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFeedbackResponse:
    return await _upsert_private_feedback(
        thread_id=thread_id,
        run_id=run_id,
        body=body,
        request=request,
        context=context,
    )


@router.delete(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> Response:
    try:
        await _feedback_service(
            request,
            context.request_id,
        ).delete(
            context,
            thread_id=str(thread_id),
            run_id=str(run_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    response_model=PrivateFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
)
async def create_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    body: PrivateFeedbackCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFeedbackResponse:
    return await _upsert_private_feedback(
        thread_id=thread_id,
        run_id=run_id,
        body=body,
        request=request,
        context=context,
    )


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


__all__ = ["router"]
