from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from starlette.responses import StreamingResponse

from app.gateway.deps import get_config, get_current_agent_runtime_config, private_work_context
from app.gateway.private_work_schemas import (
    PrivateRunCreateRequest,
    PrivateThreadContextUsageQuery,
    PrivateWorkRoute,
    public_run_input_projection,
)
from app.gateway.routers.private_work_routes.contracts import (
    PrivateEditRegeneratePrepareRequest,
    PrivateEditRegeneratePrepareResponse,
    PrivateRegeneratePrepareRequest,
    PrivateRegeneratePrepareResponse,
    PrivateSuggestionsRequest,
    PrivateSuggestionsResponse,
    PrivateThreadBranchRequest,
    PrivateThreadBranchResponse,
    PrivateThreadCompactRequest,
    PrivateThreadCompactResponse,
    PrivateThreadGoalRequest,
    PrivateThreadGoalResponse,
)
from app.gateway.routers.private_work_routes.dependencies import _chat_control_service, _raise_http
from app.gateway.routers.private_work_routes.streaming import _PRIVATE_STREAM_POLL_SECONDS
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkUnavailable,
)
from app.private_work.http_runtime import format_sse
from deerflow.config.app_config import AppConfig
from deerflow.runtime.context_evidence import ContextProjectionHead
from deerflow.runtime.events.stream import parse_stream_cursor
from deerflow.utils.messages import message_to_text

router = APIRouter(route_class=PrivateWorkRoute)


def _prepared_history_replay_kind(
    metadata: Mapping[str, object],
) -> Literal["regeneration", "message_edit"] | None:
    if metadata.get("replay_kind") == "edit":
        return "message_edit"
    regeneration_keys = {
        "regenerate_from_message_id",
        "regenerate_from_run_id",
        "regenerate_checkpoint_id",
    }
    if regeneration_keys.intersection(metadata):
        return "regeneration"
    return None


async def _normalize_prepared_edit_replay(
    body: PrivateRunCreateRequest,
    *,
    thread_id: str,
    context: PrivateWorkContext,
    service: ProjectChatControlService | None,
    app_config: AppConfig,
) -> PrivateRunCreateRequest:
    """Rebuild one server-validated regeneration or edited replay."""

    replay_kind = _prepared_history_replay_kind(body.metadata)
    if replay_kind is None:
        return body
    if service is None:
        raise PrivateWorkUnavailable(context.request_id)
    if replay_kind == "regeneration":
        if body.metadata.get("replay_kind") is not None:
            raise PrivateWorkConflict(context.request_id)
        message_id = body.metadata.get("regenerate_from_message_id")
        if not isinstance(message_id, str) or not message_id:
            raise PrivateWorkConflict(context.request_id)
        prepared = await service.prepare_regenerate(
            context,
            thread_id,
            message_id=message_id,
            app_config=app_config,
        )
        checkpoint = body.checkpoint
        prepared_checkpoint = prepared.get("checkpoint")
        prepared_input = prepared.get("input")
        if (
            checkpoint is None
            or not isinstance(prepared_checkpoint, Mapping)
            or not isinstance(prepared_input, Mapping)
            or checkpoint.checkpoint_id != prepared_checkpoint.get("checkpoint_id")
            or dict(body.metadata) != prepared.get("metadata")
            or body.input != public_run_input_projection(prepared_input)
        ):
            raise PrivateWorkConflict(context.request_id)
        return body.model_copy(
            update={
                "input": copy.deepcopy(prepared["input"]),
                "metadata": copy.deepcopy(prepared["metadata"]),
            }
        )

    graph_input = body.input
    messages = graph_input.get("messages") if isinstance(graph_input, Mapping) else None
    if not isinstance(messages, list) or len(messages) != 1:
        raise PrivateWorkConflict(context.request_id)
    replacement = messages[0]
    if not isinstance(replacement, Mapping):
        raise PrivateWorkConflict(context.request_id)
    replacement_base_id = replacement.get("id")
    source_message_id = body.metadata.get("edit_from_message_id")
    replacement_text = message_to_text(replacement).strip()
    if not isinstance(replacement_base_id, str) or not replacement_base_id or not isinstance(source_message_id, str) or not source_message_id or not replacement_text:
        raise PrivateWorkConflict(context.request_id)

    prepared = await service.prepare_edit_regenerate(
        context,
        thread_id,
        human_message_id=source_message_id,
        replacement_text=replacement_text,
        replacement_base_id=replacement_base_id,
        app_config=app_config,
    )
    checkpoint = body.checkpoint
    prepared_checkpoint = prepared.get("checkpoint")
    if checkpoint is None or not isinstance(prepared_checkpoint, Mapping) or checkpoint.checkpoint_id != prepared_checkpoint.get("checkpoint_id") or dict(body.metadata) != prepared.get("metadata"):
        raise PrivateWorkConflict(context.request_id)
    return body.model_copy(
        update={
            "input": copy.deepcopy(prepared["input"]),
            "metadata": copy.deepcopy(prepared["metadata"]),
        }
    )


@router.get(
    "/threads/{thread_id}/goal",
    response_model=PrivateThreadGoalResponse,
)
async def get_private_thread_goal(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_config),
) -> PrivateThreadGoalResponse:
    try:
        goal = await _chat_control_service(
            request,
            context.request_id,
        ).get_goal(
            context,
            str(thread_id),
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadGoalResponse(goal=goal)


@router.put(
    "/threads/{thread_id}/goal",
    response_model=PrivateThreadGoalResponse,
)
async def set_private_thread_goal(
    thread_id: uuid.UUID,
    body: PrivateThreadGoalRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_config),
) -> PrivateThreadGoalResponse:
    try:
        goal = await _chat_control_service(
            request,
            context.request_id,
        ).set_goal(
            context,
            str(thread_id),
            objective=body.objective,
            max_continuations=body.max_continuations,
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadGoalResponse(goal=goal)


@router.delete(
    "/threads/{thread_id}/goal",
    response_model=PrivateThreadGoalResponse,
)
async def clear_private_thread_goal(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_config),
) -> PrivateThreadGoalResponse:
    try:
        await _chat_control_service(
            request,
            context.request_id,
        ).clear_goal(
            context,
            str(thread_id),
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadGoalResponse(goal=None)


@router.post(
    "/threads/{thread_id}/compact",
    response_model=PrivateThreadCompactResponse,
)
async def compact_private_thread(
    thread_id: uuid.UUID,
    body: PrivateThreadCompactRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_current_agent_runtime_config),
) -> PrivateThreadCompactResponse:
    try:
        result = await _chat_control_service(
            request,
            context.request_id,
        ).compact(
            context,
            str(thread_id),
            force=body.force,
            keep=None if body.keep is None else body.keep.to_tuple(),
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadCompactResponse(
        thread_id=result.thread_id,
        compacted=result.compacted,
        reason=result.reason,
        removed_message_count=result.removed_message_count,
        preserved_message_count=result.preserved_message_count,
        summary_updated=result.summary_updated,
        checkpoint_id=result.checkpoint_id,
        total_tokens=result.total_tokens,
    )


@router.get(
    "/threads/{thread_id}/context-usage",
    response_model=ContextProjectionHead,
)
async def private_thread_context_usage(
    thread_id: uuid.UUID,
    request: Request,
    query: Annotated[PrivateThreadContextUsageQuery, Query()],
    context: PrivateWorkContext = Depends(private_work_context),
) -> ContextProjectionHead:
    try:
        projection = await _chat_control_service(
            request,
            context.request_id,
        ).context_projection(
            context,
            str(thread_id),
            subject_kind=query.subject_kind,
            execution_id=(str(query.subject_id) if query.subject_kind == "subagent_task" else None),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    if isinstance(projection, ContextProjectionHead):
        return projection
    return ContextProjectionHead.from_safe_mapping(projection)


async def _context_projection_sse_consumer(
    *,
    service: ProjectChatControlService,
    context: PrivateWorkContext,
    thread_id: str,
    request: Request,
    cursor: int,
    poll_seconds: float = _PRIVATE_STREAM_POLL_SECONDS,
) -> AsyncIterator[str]:
    """Poll rebuildable Heads and monotonically converge every Context Subject."""

    while True:
        updates = await service.context_projection_updates(
            context,
            thread_id,
            after_projection_seq=cursor,
        )
        for projection in updates:
            next_cursor = int(projection.projection_seq)
            if next_cursor <= cursor:
                continue
            cursor = next_cursor
            yield format_sse(
                "context.projection.updated.v2",
                projection.to_safe_mapping(),
                event_id=projection.projection_seq,
            )
        if await request.is_disconnected():
            return
        await asyncio.sleep(max(0, poll_seconds))


def _context_projection_stream_cursor(
    request: Request,
    *,
    after_seq: str,
    request_id: str,
) -> int:
    """Resume after every cursor the browser or query has already observed."""

    try:
        query_cursor = parse_stream_cursor(after_seq)
        raw_last_event_id = request.headers.get("Last-Event-ID")
        header_cursor = 0 if raw_last_event_id in {None, ""} else parse_stream_cursor(raw_last_event_id)
    except ValueError:
        raise PrivateWorkInvalid(request_id) from None
    return max(query_cursor, header_cursor)


@router.get("/threads/{thread_id}/context-usage/stream")
async def stream_private_thread_context_usage(
    thread_id: uuid.UUID,
    request: Request,
    after_seq: Annotated[str, Query(pattern=r"^(0|[1-9][0-9]*)$")] = "0",
    context: PrivateWorkContext = Depends(private_work_context),
) -> StreamingResponse:
    try:
        cursor = _context_projection_stream_cursor(
            request,
            after_seq=after_seq,
            request_id=context.request_id,
        )
        service = _chat_control_service(request, context.request_id)
        # Preflight authorization and storage before committing HTTP 200.
        await service.context_projection_updates(
            context,
            str(thread_id),
            after_projection_seq=cursor,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return StreamingResponse(
        _context_projection_sse_consumer(
            service=service,
            context=context,
            thread_id=str(thread_id),
            request=request,
            cursor=cursor,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/threads/{thread_id}/branches",
    response_model=PrivateThreadBranchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def branch_private_thread(
    thread_id: uuid.UUID,
    body: PrivateThreadBranchRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_config),
) -> PrivateThreadBranchResponse:
    try:
        record, checkpoint_id = await _chat_control_service(
            request,
            context.request_id,
        ).branch(
            context,
            str(thread_id),
            message_id=body.message_id,
            message_ids=body.message_ids,
            title=body.title,
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadBranchResponse(
        thread_id=record.thread_id,
        parent_thread_id=str(thread_id),
        parent_checkpoint_id=checkpoint_id,
        branched_from_message_id=body.message_id,
        workspace_clone_mode=str(record.metadata.get("workspace_clone_mode", "historical_skip")),
    )


@router.post(
    "/threads/{thread_id}/runs/regenerate/prepare",
    response_model=PrivateRegeneratePrepareResponse,
)
async def prepare_private_regenerate_run(
    thread_id: uuid.UUID,
    body: PrivateRegeneratePrepareRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_config),
) -> PrivateRegeneratePrepareResponse:
    try:
        payload = await _chat_control_service(
            request,
            context.request_id,
        ).prepare_regenerate(
            context,
            str(thread_id),
            message_id=body.message_id,
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateRegeneratePrepareResponse.model_validate(payload)


@router.post(
    "/threads/{thread_id}/runs/edit-regenerate/prepare",
    response_model=PrivateEditRegeneratePrepareResponse,
)
async def prepare_private_edit_regenerate_run(
    thread_id: uuid.UUID,
    body: PrivateEditRegeneratePrepareRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_config),
) -> PrivateEditRegeneratePrepareResponse:
    try:
        payload = await _chat_control_service(
            request,
            context.request_id,
        ).prepare_edit_regenerate(
            context,
            str(thread_id),
            human_message_id=body.human_message_id,
            replacement_text=body.replacement_text,
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateEditRegeneratePrepareResponse.model_validate(payload)


@router.post(
    "/threads/{thread_id}/suggestions",
    response_model=PrivateSuggestionsResponse,
)
async def generate_private_suggestions(
    thread_id: uuid.UUID,
    body: PrivateSuggestionsRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_current_agent_runtime_config),
) -> PrivateSuggestionsResponse:
    try:
        suggestions = await _chat_control_service(
            request,
            context.request_id,
        ).suggest(
            context,
            str(thread_id),
            n=body.n,
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateSuggestionsResponse(suggestions=suggestions)
