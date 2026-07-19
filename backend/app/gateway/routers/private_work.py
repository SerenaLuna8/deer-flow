from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.gateway.deps import private_work_context, project_session, require_project_private_open
from app.gateway.private_work_schemas import (
    PrivateRunCreateRequest,
    PrivateThreadTokenUsageResponse,
    PrivateWorkRoute,
    StrictPrivateWorkRequest,
    StrictPrivateWorkResponse,
)
from app.private_work.checkpointer import (
    PRIVATE_SCOPE_MARKER,
    ProjectScopedCheckpointer,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.file_service import PrivateFileService
from app.private_work.file_streaming import (
    PrivateFileStreamer,
    private_streaming_response,
)
from app.private_work.http_runtime import format_sse, start_private_run
from app.private_work.readiness_service import (
    PrivateWorkReadinessService,
    ReadinessStatus,
)
from app.private_work.run_repository import PrivateRunRecord
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRecord, ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService
from app.projects.capabilities import Capability
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityDatabaseUnavailable,
    ReliabilityError,
    ReliabilityInvalidStreamCursor,
)
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.persistence.private_work.file_repository import (
    PRIVATE_FILE_CHUNK_SIZE,
    PrivateFileRecord,
)
from deerflow.runtime import DisconnectMode, RunRecord, serialize_channel_values_for_api
from deerflow.runtime.events.models import (
    StoredStreamFrame,
    StreamCursorOutOfRange,
)
from deerflow.runtime.events.store import RunEventStore
from deerflow.runtime.events.stream import (
    PostgresStreamBridge,
    parse_stream_cursor,
)
from deerflow.runtime.runs.store import RunStore
from deerflow.utils.time import coerce_iso

router = APIRouter(
    prefix="/api/projects/{project_id}/private-work",
    tags=["project-private-work"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)


class PrivateThreadCreateRequest(StrictPrivateWorkRequest):
    thread_id: uuid.UUID
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"] = "project"
    display_name: str | None = Field(default=None, max_length=256)
    metadata: dict[str, object] = Field(default_factory=dict)


class PrivateThreadSearchRequest(StrictPrivateWorkRequest):
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class PrivateThreadPatchRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, max_length=256)


class PrivateThreadResponse(StrictPrivateWorkResponse):
    thread_id: str
    agent_asset_id: str
    agent_scope: str
    display_name: str | None
    status: str
    metadata: dict[str, Any]
    version: int


class PrivateThreadSearchResponse(StrictPrivateWorkResponse):
    items: list[PrivateThreadResponse]


class PrivateThreadDeleteResponse(StrictPrivateWorkResponse):
    success: bool


class PrivateThreadStateResponse(StrictPrivateWorkResponse):
    values: dict[str, Any] = Field(default_factory=dict)
    next: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    checkpoint_id: str | None = None
    parent_checkpoint_id: str | None = None
    created_at: str | None = None
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class PrivateRunResponse(StrictPrivateWorkResponse):
    run_id: str
    thread_id: str
    assistant_id: str | None = None
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    multitask_strategy: str = "reject"
    error: str | None = None
    model_name: str | None = None
    created_at: str = ""
    updated_at: str = ""


class PrivateRunDeleteResponse(StrictPrivateWorkResponse):
    success: bool


class PrivateFeedbackCreateRequest(StrictPrivateWorkRequest):
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=4096)
    message_id: str | None = Field(default=None, max_length=64)


class PrivateFeedbackResponse(StrictPrivateWorkResponse):
    feedback_id: str
    run_id: str
    thread_id: str
    message_id: str | None = None
    rating: Literal[1, -1]
    comment: str | None = None
    created_at: str = ""


class PrivateFileResponse(StrictPrivateWorkResponse):
    id: uuid.UUID
    logical_path: str
    display_name: str
    kind: str
    media_type: str
    size: int
    sha256: str
    status: str
    created_at: datetime
    updated_at: datetime


class PrivateFileDeleteResponse(StrictPrivateWorkResponse):
    success: bool


class PrivateWorkReadinessResponse(StrictPrivateWorkResponse):
    status: ReadinessStatus
    code: str
    request_id: str


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


def _thread_response(record: PrivateThreadRecord) -> PrivateThreadResponse:
    return PrivateThreadResponse(
        thread_id=record.thread_id,
        agent_asset_id=str(record.agent_asset_id),
        agent_scope=record.agent_scope,
        display_name=record.display_name,
        status=record.status,
        metadata=record.metadata,
        version=record.version,
    )


def _public_run_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    sensitive_parts = (
        "secret",
        "token",
        "password",
        "credential",
        "ciphertext",
        "private_key",
        "key_id",
        "nonce",
        "storage_locator",
    )
    return {
        str(key): (_public_run_metadata(item) if isinstance(item, Mapping) else [_public_run_metadata(entry) if isinstance(entry, Mapping) else entry for entry in item] if isinstance(item, list) else item)
        for key, item in value.items()
        if isinstance(key, str) and key not in {"project_id", "owner_user_id", "user_id"} and not any(part in key.lower() for part in sensitive_parts)
    }


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _run_response(record: PrivateRunRecord | RunRecord) -> PrivateRunResponse:
    raw_status = record.status
    status_value = getattr(raw_status, "value", raw_status)
    return PrivateRunResponse(
        run_id=record.run_id,
        thread_id=record.thread_id,
        assistant_id=record.assistant_id,
        status=str(status_value),
        metadata=_public_run_metadata(record.metadata),
        multitask_strategy=record.multitask_strategy,
        error=record.error,
        model_name=record.model_name,
        created_at=_timestamp(record.created_at),
        updated_at=_timestamp(record.updated_at),
    )


def _thread_service(request: Request, request_id: str) -> PrivateThreadService:
    service = getattr(request.app.state, "private_thread_service", None)
    if not isinstance(service, PrivateThreadService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _file_service(request: Request, request_id: str) -> PrivateFileService:
    service = getattr(request.app.state, "private_file_service", None)
    if not isinstance(service, PrivateFileService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _file_streamer(request: Request, request_id: str) -> PrivateFileStreamer:
    streamer = getattr(request.app.state, "private_file_streamer", None)
    if not isinstance(streamer, PrivateFileStreamer):
        raise PrivateWorkUnavailable(request_id)
    return streamer


def _file_response(record: PrivateFileRecord) -> PrivateFileResponse:
    return PrivateFileResponse(
        id=record.id,
        logical_path=record.logical_path,
        display_name=record.logical_path.rsplit("/", 1)[-1],
        kind=record.kind,
        media_type=record.media_type,
        size=record.size,
        sha256=record.sha256,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _upload_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(PRIVATE_FILE_CHUNK_SIZE):
        yield chunk


def _run_service(request: Request, request_id: str) -> PrivateRunService:
    service = getattr(request.app.state, "private_run_service", None)
    if not isinstance(service, PrivateRunService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _run_event_store(request: Request, request_id: str) -> RunEventStore:
    store = getattr(request.app.state, "private_run_event_store", None)
    if not isinstance(store, RunEventStore):
        raise PrivateWorkUnavailable(request_id)
    return store


def _run_store(request: Request, request_id: str) -> RunStore:
    store = getattr(request.app.state, "run_store", None)
    if not isinstance(store, RunStore):
        raise PrivateWorkUnavailable(request_id)
    return store


def _feedback_repository(request: Request, request_id: str) -> FeedbackRepository:
    repository = getattr(request.app.state, "feedback_repo", None)
    if not isinstance(repository, FeedbackRepository):
        raise PrivateWorkUnavailable(request_id)
    return repository


def _public_event(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"project_id", "owner_user_id", "user_id"}}


def _runtime_dependency(request: Request, request_id: str, name: str) -> object:
    dependency = getattr(request.app.state, name, None)
    if dependency is None:
        raise PrivateWorkUnavailable(request_id)
    return dependency


def _private_stream_bridge(
    request: Request,
    request_id: str,
) -> PostgresStreamBridge:
    bridge = getattr(request.app.state, "private_stream_bridge", None)
    if not isinstance(bridge, PostgresStreamBridge):
        raise PrivateWorkUnavailable(request_id)
    return bridge


def _require_run_runtime(request: Request, request_id: str) -> PostgresStreamBridge:
    _runtime_dependency(request, request_id, "project_scoped_checkpointer")
    return _private_stream_bridge(request, request_id)


_PRIVATE_STREAM_POLL_SECONDS = 0.25
_PRIVATE_STREAM_HEARTBEAT_SECONDS = 15.0
_PRIVATE_RUN_TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})


def _private_stream_cursor(request: Request, request_id: str) -> int:
    raw_cursor = request.headers.get("Last-Event-ID")
    if raw_cursor is None or raw_cursor == "":
        return 0
    try:
        return parse_stream_cursor(raw_cursor)
    except ValueError:
        raise ReliabilityInvalidStreamCursor(request_id)


def _private_stream_headers(
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
) -> dict[str, str]:
    run_path = f"/api/projects/{context.project_id}/private-work/threads/{thread_id}/runs/{run_id}"
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "Content-Location": run_path,
        # LangGraph SDK resolves this against its project-private API base.
        "Location": f"/threads/{thread_id}/runs/{run_id}/stream",
    }


def _fallback_terminal_status(status_value: str) -> str:
    return "completed" if status_value == "success" else status_value


async def _read_private_stream_page(
    bridge: PostgresStreamBridge,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    cursor: int,
) -> tuple[StoredStreamFrame, ...]:
    try:
        return await bridge.read_after(
            context.resource_scope,
            thread_id,
            cursor=cursor,
            limit=100,
            run_id=run_id,
        )
    except StreamCursorOutOfRange:
        raise ReliabilityInvalidStreamCursor(context.request_id) from None
    except DBAPIError:
        raise ReliabilityDatabaseUnavailable(context.request_id) from None


async def _durable_private_sse_consumer(
    *,
    bridge: PostgresStreamBridge,
    service: PrivateRunService,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    request: Request,
    cursor: int,
    initial_frames: tuple[StoredStreamFrame, ...],
    cancel_on_disconnect: bool,
) -> AsyncIterator[str]:
    frames = initial_frames
    pending_terminal: StoredStreamFrame | None = None
    disconnected = False
    cancelled = False
    terminal_emitted = False
    loop = asyncio.get_running_loop()
    next_heartbeat = loop.time() + _PRIVATE_STREAM_HEARTBEAT_SECONDS
    try:
        while True:
            for frame in frames:
                if frame.terminal:
                    pending_terminal = frame
                    break
                cursor = int(frame.id)
                yield format_sse(
                    frame.event,
                    frame.data,
                    event_id=frame.id,
                )
            frames = ()

            if await request.is_disconnected():
                disconnected = True
                return

            if pending_terminal is None:
                frames = await _read_private_stream_page(
                    bridge,
                    context,
                    thread_id,
                    run_id,
                    cursor,
                )
                if frames:
                    continue

            record = await service.get(context, thread_id, run_id)
            if record.status in _PRIVATE_RUN_TERMINAL_STATUSES:
                terminal = await bridge.ensure_settled_terminal(
                    context.resource_scope,
                    thread_id,
                    run_id,
                    status=_fallback_terminal_status(record.status),
                )
                cursor = int(terminal.id)
                terminal_emitted = True
                yield format_sse(
                    terminal.event,
                    terminal.data,
                    event_id=terminal.id,
                )
                return

            now = loop.time()
            if now >= next_heartbeat:
                yield ": heartbeat\n\n"
                next_heartbeat = loop.time() + _PRIVATE_STREAM_HEARTBEAT_SECONDS
            await asyncio.sleep(
                min(
                    _PRIVATE_STREAM_POLL_SECONDS,
                    max(0.001, next_heartbeat - loop.time()),
                )
            )
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if (disconnected or cancelled) and cancel_on_disconnect and not terminal_emitted:
            await _persist_private_disconnect_cancel(
                service=service,
                context=context,
                thread_id=thread_id,
                run_id=run_id,
            )


async def _persist_private_disconnect_cancel(
    *,
    service: PrivateRunService,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
) -> None:
    cancel_task = asyncio.create_task(
        service.cancel(
            context,
            thread_id,
            run_id,
            reason="client_disconnected",
        )
    )
    try:
        await asyncio.shield(cancel_task)
    except asyncio.CancelledError:
        try:
            await cancel_task
        except PrivateWorkError:
            pass
    except PrivateWorkError:
        pass


async def _wait_for_durable_private_run(
    *,
    service: PrivateRunService,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    request: Request,
    cancel_on_disconnect: bool,
) -> tuple[bool, PrivateRunRecord]:
    try:
        while True:
            record = await service.get(context, thread_id, run_id)
            if record.status in _PRIVATE_RUN_TERMINAL_STATUSES:
                return True, record
            if await request.is_disconnected():
                if cancel_on_disconnect:
                    await _persist_private_disconnect_cancel(
                        service=service,
                        context=context,
                        thread_id=thread_id,
                        run_id=run_id,
                    )
                return False, record
            await asyncio.sleep(_PRIVATE_STREAM_POLL_SECONDS)
    except asyncio.CancelledError:
        if cancel_on_disconnect:
            await _persist_private_disconnect_cancel(
                service=service,
                context=context,
                thread_id=thread_id,
                run_id=run_id,
            )
        raise


def _scoped_checkpointer(
    request: Request,
    request_id: str,
) -> ProjectScopedCheckpointer:
    checkpointer = getattr(request.app.state, "project_scoped_checkpointer", None)
    if not isinstance(checkpointer, ProjectScopedCheckpointer):
        raise PrivateWorkUnavailable(request_id)
    return checkpointer


def _raise_http(error: PrivateWorkError) -> None:
    raise private_work_http_exception(error) from None


@router.post(
    "/threads/{thread_id}/uploads",
    response_model=PrivateFileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_private_file(
    thread_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFileResponse:
    try:
        record = await _file_service(request, context.request_id).upload(
            context,
            thread_id=str(thread_id),
            logical_path=f"uploads/{file.filename or ''}",
            media_type=file.content_type or "application/octet-stream",
            chunks=_upload_chunks(file),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _file_response(record)


@router.get(
    "/threads/{thread_id}/uploads",
    response_model=list[PrivateFileResponse],
)
async def list_private_files(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[PrivateFileResponse]:
    try:
        records = await _file_service(request, context.request_id).list_ready(
            context,
            thread_id=str(thread_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return [_file_response(record) for record in records]


@router.delete(
    "/threads/{thread_id}/uploads",
    response_model=PrivateFileDeleteResponse,
)
async def delete_private_file(
    thread_id: uuid.UUID,
    request: Request,
    file_id: uuid.UUID = Query(),
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFileDeleteResponse:
    try:
        await _file_service(request, context.request_id).delete_ready(
            context,
            thread_id=str(thread_id),
            file_id=file_id,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateFileDeleteResponse(success=True)


@router.get("/threads/{thread_id}/files/{file_id}")
async def download_private_file(
    thread_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> StreamingResponse:
    try:
        stream = await _file_streamer(
            request,
            context.request_id,
        ).stream_file(
            context,
            thread_id=str(thread_id),
            file_id=file_id,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return private_streaming_response(stream)


@router.get("/artifacts/{artifact_id}")
async def download_private_artifact(
    artifact_id: uuid.UUID,
    request: Request,
    thread_id: uuid.UUID = Query(),
    context: PrivateWorkContext = Depends(private_work_context),
) -> StreamingResponse:
    try:
        stream = await _file_streamer(
            request,
            context.request_id,
        ).stream_artifact(
            context,
            thread_id=str(thread_id),
            artifact_id=artifact_id,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return private_streaming_response(stream)


@router.post(
    "/threads/{thread_id}/runs",
    response_model=PrivateRunResponse,
)
async def create_private_run(
    thread_id: uuid.UUID,
    body: PrivateRunCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateRunResponse:
    try:
        record = await start_private_run(body, str(thread_id), request, context)
    except PrivateWorkError as error:
        _raise_http(error)
    except ReliabilityError as error:
        raise reliability_http_exception(error) from None
    return _run_response(record)


@router.post("/threads/{thread_id}/runs/stream")
async def stream_private_run(
    thread_id: uuid.UUID,
    body: PrivateRunCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> StreamingResponse:
    try:
        bridge = _private_stream_bridge(request, context.request_id)
        cursor = _private_stream_cursor(request, context.request_id)
        record = await start_private_run(body, str(thread_id), request, context)
        service = _run_service(request, context.request_id)
    except PrivateWorkError as error:
        _raise_http(error)
    except ReliabilityError as error:
        raise reliability_http_exception(error) from None

    return StreamingResponse(
        _durable_private_sse_consumer(
            bridge=bridge,
            service=service,
            context=context,
            thread_id=str(thread_id),
            run_id=record.run_id,
            request=request,
            cursor=cursor,
            initial_frames=(),
            cancel_on_disconnect=record.on_disconnect == DisconnectMode.cancel,
        ),
        media_type="text/event-stream",
        headers=_private_stream_headers(
            context,
            str(thread_id),
            record.run_id,
        ),
    )


@router.get("/threads/{thread_id}/runs/{run_id}/stream")
async def reconnect_private_run_stream(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> StreamingResponse:
    selected_thread_id = str(thread_id)
    selected_run_id = str(run_id)
    try:
        bridge = _private_stream_bridge(request, context.request_id)
        cursor = _private_stream_cursor(request, context.request_id)
        service = _run_service(request, context.request_id)
        await service.get(
            context,
            selected_thread_id,
            selected_run_id,
        )
        initial_frames = await _read_private_stream_page(
            bridge,
            context,
            selected_thread_id,
            selected_run_id,
            cursor,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    except ReliabilityError as error:
        raise reliability_http_exception(error) from None

    return StreamingResponse(
        _durable_private_sse_consumer(
            bridge=bridge,
            service=service,
            context=context,
            thread_id=selected_thread_id,
            run_id=selected_run_id,
            request=request,
            cursor=cursor,
            initial_frames=initial_frames,
            cancel_on_disconnect=False,
        ),
        media_type="text/event-stream",
        headers=_private_stream_headers(
            context,
            selected_thread_id,
            selected_run_id,
        ),
    )


@router.post(
    "/threads/{thread_id}/runs/wait",
    response_model=dict[str, Any],
)
async def wait_private_run(
    thread_id: uuid.UUID,
    body: PrivateRunCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, Any]:
    try:
        _require_run_runtime(request, context.request_id)
        record = await start_private_run(body, str(thread_id), request, context)
        completed, durable_record = await _wait_for_durable_private_run(
            service=_run_service(request, context.request_id),
            context=context,
            thread_id=str(thread_id),
            run_id=record.run_id,
            request=request,
            cancel_on_disconnect=record.on_disconnect == DisconnectMode.cancel,
        )
        if not completed:
            return {
                "status": durable_record.status,
                "error": durable_record.error,
            }

        saver = _scoped_checkpointer(request, context.request_id).for_context(context)
        item = await saver.aget_tuple(
            {
                "configurable": {
                    "thread_id": str(thread_id),
                    "checkpoint_ns": "",
                }
            }
        )
        if item is None:
            raise PrivateWorkNotFound(context.request_id)
        checkpoint = item.checkpoint or {}
        channel_values = checkpoint.get("channel_values", {})
        return serialize_channel_values_for_api(channel_values if isinstance(channel_values, dict) else {})
    except PrivateWorkError as error:
        _raise_http(error)
    except ReliabilityError as error:
        raise reliability_http_exception(error) from None


@router.get(
    "/threads/{thread_id}/runs",
    response_model=list[PrivateRunResponse],
)
async def list_private_runs(
    thread_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[PrivateRunResponse]:
    try:
        records = await _run_service(request, context.request_id).list(
            context,
            str(thread_id),
            limit=limit,
            offset=offset,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return [_run_response(record) for record in records]


@router.get(
    "/threads/{thread_id}/runs/{run_id}",
    response_model=PrivateRunResponse,
)
async def get_private_run(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateRunResponse:
    try:
        record = await _run_service(request, context.request_id).get(
            context,
            str(thread_id),
            str(run_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _run_response(record)


@router.post("/threads/{thread_id}/runs/{run_id}/cancel")
async def cancel_private_run(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    wait: bool = Query(default=False),
    action: Literal["interrupt", "rollback"] = Query(default="interrupt"),
    context: PrivateWorkContext = Depends(private_work_context),
) -> Response:
    """Persist cooperative cancellation for the durable private Run job."""

    try:
        if action != "interrupt":
            # Durable rollback requires an explicit checkpoint restore job;
            # silently treating it as interrupt would violate the SDK contract.
            raise PrivateWorkConflict(context.request_id)
        service = _run_service(request, context.request_id)
        await service.cancel(
            context,
            str(thread_id),
            str(run_id),
        )
        if wait:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30.0
            while loop.time() < deadline:
                if await request.is_disconnected():
                    return Response(status_code=499)
                record = await service.get(
                    context,
                    str(thread_id),
                    str(run_id),
                )
                if record.status in {
                    "success",
                    "error",
                    "timeout",
                    "interrupted",
                }:
                    return Response(status_code=status.HTTP_204_NO_CONTENT)
                await asyncio.sleep(min(0.25, deadline - loop.time()))
    except PrivateWorkError as error:
        _raise_http(error)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.delete(
    "/threads/{thread_id}/runs/{run_id}",
    response_model=PrivateRunDeleteResponse,
)
async def delete_private_run(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateRunDeleteResponse:
    try:
        await _run_service(request, context.request_id).delete(
            context,
            str(thread_id),
            str(run_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateRunDeleteResponse(success=True)


@router.get(
    "/threads/{thread_id}/messages",
    response_model=list[dict[str, Any]],
)
async def list_private_thread_messages(
    thread_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    before_seq: int | None = Query(default=None, ge=0),
    after_seq: int | None = Query(default=None, ge=0),
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[dict[str, Any]]:
    try:
        await _run_service(request, context.request_id).list(
            context,
            str(thread_id),
            limit=1,
            offset=0,
        )
        records = await _run_event_store(
            request,
            context.request_id,
        ).list_messages(
            str(thread_id),
            limit=limit,
            before_seq=before_seq,
            after_seq=after_seq,
            scope=context.resource_scope,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return [_public_event(record) for record in records]


async def _private_run_events(
    *,
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext,
    event_types: str | None,
    task_id: str | None,
    limit: int,
    after_seq: int | None,
) -> list[dict[str, Any]]:
    await _run_service(request, context.request_id).get(
        context,
        str(thread_id),
        str(run_id),
    )
    types = [value for value in event_types.split(",") if value] if event_types else None
    records = await _run_event_store(request, context.request_id).list_events(
        str(thread_id),
        str(run_id),
        event_types=types,
        task_id=task_id,
        limit=limit,
        after_seq=after_seq,
        scope=context.resource_scope,
    )
    return [_public_event(record) for record in records]


@router.get(
    "/threads/{thread_id}/runs/{run_id}/events",
    response_model=list[dict[str, Any]],
)
async def list_private_run_events(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    event_types: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    after_seq: int | None = Query(default=None, ge=0),
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[dict[str, Any]]:
    try:
        return await _private_run_events(
            thread_id=thread_id,
            run_id=run_id,
            request=request,
            context=context,
            event_types=event_types,
            task_id=task_id,
            limit=limit,
            after_seq=after_seq,
        )
    except PrivateWorkError as error:
        _raise_http(error)


@router.get(
    "/threads/{thread_id}/events",
    response_model=list[dict[str, Any]],
)
async def list_private_thread_events(
    thread_id: uuid.UUID,
    request: Request,
    run_id: uuid.UUID = Query(),
    event_types: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    after_seq: int | None = Query(default=None, ge=0),
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[dict[str, Any]]:
    try:
        return await _private_run_events(
            thread_id=thread_id,
            run_id=run_id,
            request=request,
            context=context,
            event_types=event_types,
            task_id=task_id,
            limit=limit,
            after_seq=after_seq,
        )
    except PrivateWorkError as error:
        _raise_http(error)


@router.get(
    "/threads/{thread_id}/token-usage",
    response_model=PrivateThreadTokenUsageResponse,
)
async def private_thread_token_usage(
    thread_id: uuid.UUID,
    request: Request,
    include_active: bool = Query(default=False),
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateThreadTokenUsageResponse:
    try:
        await _run_service(request, context.request_id).list(
            context,
            str(thread_id),
            limit=1,
            offset=0,
        )
        aggregate = await _run_store(
            request,
            context.request_id,
        ).aggregate_tokens_by_thread(
            str(thread_id),
            include_active=include_active,
            scope=context.resource_scope,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadTokenUsageResponse(thread_id=str(thread_id), **aggregate)


@router.post(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    response_model=PrivateFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    body: PrivateFeedbackCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFeedbackResponse:
    try:
        await _run_service(request, context.request_id).get(
            context,
            str(thread_id),
            str(run_id),
        )
        if Capability.PRIVATE_WORK_CREATE not in context.capabilities:
            raise PrivateWorkForbidden(context.request_id)
        record = await _feedback_repository(
            request,
            context.request_id,
        ).create(
            run_id=str(run_id),
            thread_id=str(thread_id),
            rating=body.rating,
            scope=context.resource_scope,
            message_id=body.message_id,
            comment=body.comment,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    except ValueError:
        _raise_http(PrivateWorkNotFound(context.request_id))
    return PrivateFeedbackResponse(
        feedback_id=str(record["feedback_id"]),
        run_id=str(record["run_id"]),
        thread_id=str(record["thread_id"]),
        message_id=record.get("message_id"),
        rating=record["rating"],
        comment=record.get("comment"),
        created_at=_timestamp(record.get("created_at")),
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
        record = await _thread_service(request, context.request_id).create(
            context,
            thread_id=str(body.thread_id),
            agent=ThreadAgentRef(body.agent_asset_id, body.agent_scope),
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
    expected_version: int = Query(ge=1),
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
) -> PrivateThreadStateResponse:
    try:
        saver = _scoped_checkpointer(request, context.request_id).for_context(context)
        item = await saver.aget_tuple(
            {
                "configurable": {
                    "thread_id": str(thread_id),
                    "checkpoint_ns": "",
                }
            }
        )
        if item is None:
            raise PrivateWorkNotFound(context.request_id)
    except PrivateWorkError as error:
        _raise_http(error)

    checkpoint = item.checkpoint or {}
    metadata = dict(item.metadata or {})
    metadata.pop(PRIVATE_SCOPE_MARKER, None)
    configurable = (item.config or {}).get("configurable", {})
    checkpoint_id_value = configurable.get("checkpoint_id")
    checkpoint_id = str(checkpoint_id_value) if checkpoint_id_value is not None else None
    parent_configurable = (item.parent_config or {}).get("configurable", {})
    parent_id_value = parent_configurable.get("checkpoint_id")
    parent_checkpoint_id = str(parent_id_value) if parent_id_value is not None else None
    raw_tasks = getattr(item, "tasks", ()) or ()
    tasks = [{"id": str(getattr(task, "id", "")), "name": str(getattr(task, "name", ""))} for task in raw_tasks]
    created_at = coerce_iso(metadata.get("created_at", ""))
    channel_values = checkpoint.get("channel_values", {})
    values = serialize_channel_values_for_api(channel_values if isinstance(channel_values, dict) else {})
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
