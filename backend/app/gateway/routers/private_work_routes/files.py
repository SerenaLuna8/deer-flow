from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from starlette.responses import StreamingResponse

from app.gateway.deps import private_work_context
from app.gateway.private_work_schemas import PrivateWorkRoute
from app.gateway.routers.private_work_routes.contracts import (
    _POSTGRES_BIGINT_MAX,
    PrivateFileDeleteResponse,
    PrivateFileResponse,
    PrivateUploadLimitsResponse,
    PrivateUploadProjectStorageResponse,
    _file_response,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _browser_chat_run_service,
    _file_service,
    _file_streamer,
    _raise_http,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkError
from app.private_work.file_service import PrivateUploadLimits
from app.private_work.file_streaming import private_streaming_response
from deerflow.persistence.private_work.file_repository import PRIVATE_FILE_CHUNK_SIZE

router = APIRouter(route_class=PrivateWorkRoute)


async def _upload_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(PRIVATE_FILE_CHUNK_SIZE):
        yield chunk


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
        await _browser_chat_run_service(request, context, str(thread_id))
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


def _upload_limits_response(
    limits: PrivateUploadLimits,
    *,
    request_id: str,
) -> PrivateUploadLimitsResponse:
    storage = limits.project_storage
    return PrivateUploadLimitsResponse(
        max_files=limits.max_files,
        max_file_size=limits.max_file_size,
        max_total_size=limits.max_total_size,
        project_storage=PrivateUploadProjectStorageResponse(
            policy=storage.policy,
            remaining_bytes=storage.remaining_bytes,
        ),
        request_id=request_id,
    )


@router.get(
    "/threads/{thread_id}/uploads/limits",
    response_model=PrivateUploadLimitsResponse,
)
async def get_private_upload_limits(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateUploadLimitsResponse:
    try:
        await _browser_chat_run_service(request, context, str(thread_id))
        limits = await _file_service(
            request,
            context.request_id,
        ).read_upload_limits(
            context,
            thread_id=str(thread_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _upload_limits_response(limits, request_id=context.request_id)


@router.get(
    "/threads/{thread_id}/uploads",
    response_model=list[PrivateFileResponse],
)
async def list_private_files(
    thread_id: uuid.UUID,
    request: Request,
    response: Response,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(
        default=0,
        ge=0,
        le=_POSTGRES_BIGINT_MAX,
        json_schema_extra={
            "format": "int64",
            "x-postgres-bigint-maximum": str(_POSTGRES_BIGINT_MAX),
        },
    ),
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[PrivateFileResponse]:
    try:
        await _browser_chat_run_service(request, context, str(thread_id))
        records = await _file_service(request, context.request_id).list_ready(
            context,
            thread_id=str(thread_id),
            limit=limit,
            offset=offset,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    if len(records) == limit and offset <= _POSTGRES_BIGINT_MAX - limit:
        response.headers["X-Next-Offset"] = str(offset + limit)
    return [_file_response(record) for record in records]


@router.delete(
    "/threads/{thread_id}/uploads",
    response_model=PrivateFileDeleteResponse,
)
async def delete_private_file(
    thread_id: uuid.UUID,
    request: Request,
    file_id: uuid.UUID = Query(),
    only_if_unreferenced: bool = Query(default=False),
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFileDeleteResponse:
    try:
        await _browser_chat_run_service(request, context, str(thread_id))
        deleted = await _file_service(request, context.request_id).delete_ready(
            context,
            thread_id=str(thread_id),
            file_id=file_id,
            only_if_unreferenced=only_if_unreferenced,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateFileDeleteResponse(success=True, deleted=deleted is not None)


@router.get("/threads/{thread_id}/files/{file_id}")
async def download_private_file(
    thread_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> StreamingResponse:
    try:
        await _browser_chat_run_service(request, context, str(thread_id))
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
        await _browser_chat_run_service(request, context, str(thread_id))
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
