from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.gateway.deps import get_config, private_work_context, project_session
from app.gateway.pagination import trim_run_message_page
from app.gateway.private_work_schemas import (
    PrivateRunCreateRequest,
    PrivateThreadTokenUsageResponse,
    PrivateWorkRoute,
)
from app.gateway.routers.private_work_routes import streaming
from app.gateway.routers.private_work_routes.context_controls import (
    _normalize_prepared_edit_replay,
    _prepared_history_replay_kind,
)
from app.gateway.routers.private_work_routes.contracts import (
    _POSTGRES_BIGINT_MAX,
    PrivateRunDeleteResponse,
    PrivateRunExecutionStateResponse,
    PrivateRunMessageResponse,
    PrivateRunMessagesPageResponse,
    PrivateRunResponse,
    _public_run_metadata,
    _run_response,
    _timestamp,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _browser_chat_run_service,
    _chat_control_service,
    _raise_http,
    _run_event_store,
    _run_service,
    _run_store,
    _scoped_checkpointer,
)
from app.gateway.routers.private_work_routes.streaming import (
    _durable_private_sse_consumer,
    _require_run_runtime,
    _wait_for_durable_private_run,
)
from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    checkpoint_config,
    snapshot_checkpoint_id,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkDatabaseUnavailable,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.http_runtime import start_private_run
from app.private_work.message_projection import (
    compute_run_durations,
    project_checkpoint_message_durations,
    project_event_message_durations,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_execution_state import (
    RunExecutionState,
    RunExecutionStatePolicy,
    RunExecutionStateUnavailable,
    read_run_execution_state,
)
from app.private_work.run_repository import PrivateRunRecord
from app.projects.capabilities import Capability
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import ReliabilityError
from deerflow.agents.human_input import read_human_input_response
from deerflow.config.app_config import AppConfig
from deerflow.runtime import DisconnectMode, serialize_channel_values_for_api
from deerflow.runtime.events.stream import parse_stream_cursor
from deerflow.runtime.public_token_usage import (
    project_public_persisted_run_event,
    project_public_token_usage,
)
from deerflow.workspace_changes import get_workspace_changes_response

router = APIRouter(route_class=PrivateWorkRoute)


def _public_event(record: Mapping[str, Any]) -> dict[str, Any]:
    public = {key: value for key, value in record.items() if key not in {"project_id", "owner_user_id", "user_id"}}
    if "seq" in public:
        public["seq"] = str(int(public["seq"]))
    return public


def _run_message_response(record: Mapping[str, Any]) -> PrivateRunMessageResponse:
    return PrivateRunMessageResponse(
        run_id=str(record["run_id"]),
        seq=str(int(record["seq"])),
        content=dict(record["content"]),
        metadata=dict(record.get("metadata") or {}),
        created_at=str(record["created_at"]),
    )


async def _project_scoped_event_durations(
    request: Request,
    context: PrivateWorkContext,
    thread_id: str,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    run_ids = {run_id for record in records if isinstance((run_id := record.get("run_id")), str) and run_id}
    if not run_ids:
        return records
    try:
        runs = await _run_service(request, context.request_id).get_many(
            context,
            thread_id,
            run_ids,
        )
        last_ai = await _run_event_store(
            request,
            context.request_id,
        ).get_last_visible_ai_seq_by_run(
            thread_id,
            run_ids,
            scope=context.resource_scope,
        )
    except PrivateWorkError:
        raise
    except Exception:
        raise PrivateWorkUnavailable(context.request_id) from None
    return project_event_message_durations(
        records,
        run_durations=compute_run_durations(runs.values()),
        last_visible_ai_seq_by_run=last_ai,
    )


async def _project_scoped_checkpoint_durations(
    request: Request,
    context: PrivateWorkContext,
    thread_id: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    messages = values.get("messages")
    if not isinstance(messages, list):
        return values
    run_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, Mapping) or message.get("type") not in {
            "human",
            "user",
        }:
            continue
        additional_kwargs = message.get("additional_kwargs")
        run_id = additional_kwargs.get("run_id") if isinstance(additional_kwargs, Mapping) else None
        if isinstance(run_id, str) and run_id:
            run_ids.add(run_id)
    if not run_ids:
        return values
    try:
        runs = await _run_service(request, context.request_id).get_many(
            context,
            thread_id,
            run_ids,
        )
    except PrivateWorkError:
        raise
    except Exception:
        raise PrivateWorkUnavailable(context.request_id) from None
    projected = dict(values)
    projected["messages"] = project_checkpoint_message_durations(
        messages,
        run_durations=compute_run_durations(runs.values()),
    )
    return projected


def _checkpoint_turn_run_id(message: object) -> str | None:
    if not isinstance(message, Mapping) or message.get("type") not in {
        "human",
        "user",
    }:
        return None
    additional_kwargs = message.get("additional_kwargs")
    run_id = additional_kwargs.get("run_id") if isinstance(additional_kwargs, Mapping) else None
    return run_id if isinstance(run_id, str) and run_id else None


async def _frozen_token_tracking_by_run(
    request: Request,
    context: PrivateWorkContext,
    run_ids: list[str],
) -> dict[str, bool]:
    """Resolve verifiable frozen token-tracking flags in one batch."""

    selected = list(dict.fromkeys(run_ids))
    tracking_by_run = dict.fromkeys(selected, False)
    if not selected:
        return tracking_by_run
    materializer = getattr(
        request.app.state,
        "system_runtime_policy_materializer",
        None,
    )
    materialize = getattr(
        materializer,
        "materialize_run_snapshot_envelopes",
        None,
    )
    project_id = getattr(context, "project_id", None)
    owner_user_id = getattr(context, "user_id", None)
    if not callable(materialize) or not isinstance(project_id, uuid.UUID) or not isinstance(owner_user_id, uuid.UUID):
        return tracking_by_run
    try:
        materialized_by_run = await materialize(
            project_id=project_id,
            owner_user_id=str(owner_user_id),
            run_ids=selected,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return tracking_by_run
    if not isinstance(materialized_by_run, Mapping):
        return tracking_by_run
    for run_id in selected:
        materialized = materialized_by_run.get(run_id)
        policy = getattr(materialized, "value", None)
        token_usage = getattr(policy, "token_usage", None)
        tracking_by_run[run_id] = getattr(token_usage, "enabled", None) is True
    return tracking_by_run


async def _verified_tracking_run_ids_for_thread(
    request: Request,
    context: PrivateWorkContext,
    thread_id: str,
) -> frozenset[str]:
    """Return only Runs whose scoped frozen policy verifies tracking=true."""

    materializer = getattr(
        request.app.state,
        "system_runtime_policy_materializer",
        None,
    )
    materialize = getattr(
        materializer,
        "materialize_thread_run_snapshot_envelopes",
        None,
    )
    project_id = getattr(context, "project_id", None)
    owner_user_id = getattr(context, "user_id", None)
    if not callable(materialize) or not isinstance(project_id, uuid.UUID) or not isinstance(owner_user_id, uuid.UUID):
        return frozenset()
    try:
        materialized_by_run = await materialize(
            project_id=project_id,
            owner_user_id=str(owner_user_id),
            thread_id=thread_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return frozenset()
    if not isinstance(materialized_by_run, Mapping):
        return frozenset()
    verified: set[str] = set()
    for run_id, materialized in materialized_by_run.items():
        if not isinstance(run_id, str) or not run_id:
            continue
        policy = getattr(materialized, "value", None)
        token_usage = getattr(policy, "token_usage", None)
        if getattr(token_usage, "enabled", None) is True:
            verified.add(run_id)
    return frozenset(verified)


async def _project_scoped_checkpoint_token_usage(
    request: Request,
    context: PrivateWorkContext,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Apply each turn's admitted token-tracking policy to REST messages."""

    messages = values.get("messages")
    if not isinstance(messages, list):
        return values

    run_ids = list(dict.fromkeys(run_id for message in messages if (run_id := _checkpoint_turn_run_id(message)) is not None))
    tracking_by_run = await _frozen_token_tracking_by_run(
        request,
        context,
        run_ids,
    )

    projected_messages: list[Any] = []
    current_tracking_enabled = False
    for message in messages:
        if isinstance(message, Mapping) and message.get("type") in {
            "human",
            "user",
        }:
            run_id = _checkpoint_turn_run_id(message)
            current_tracking_enabled = tracking_by_run.get(run_id, False) if run_id is not None else False
        projected_messages.append(
            project_public_token_usage(
                message,
                tracking_enabled=current_tracking_enabled,
            )
        )

    projected = dict(values)
    projected["messages"] = projected_messages
    return projected


async def _project_scoped_event_token_usage(
    request: Request,
    context: PrivateWorkContext,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply each Run's admitted token-tracking policy to persisted events."""

    run_ids = list(dict.fromkeys(run_id for record in records if isinstance((run_id := record.get("run_id")), str) and run_id))

    tracking_by_run = await _frozen_token_tracking_by_run(
        request,
        context,
        run_ids,
    )
    return [
        project_public_persisted_run_event(
            record,
            tracking_enabled=tracking_by_run.get(record.get("run_id"), False),
        )
        for record in records
    ]


_FAILED_PRIVATE_RUN_STATUSES = frozenset({"error", "failed", "timeout"})


def _admitted_failure_message(
    record: PrivateRunRecord,
    *,
    before_seq: int | None,
    after_seq: int | None,
) -> dict[str, Any] | None:
    """Recover the submitted visible prompt when a Run fails before journaling."""

    if record.status not in _FAILED_PRIVATE_RUN_STATUSES:
        return None
    if before_seq is not None and before_seq <= 0:
        return None
    if after_seq is not None:
        return None

    graph_input = record.kwargs.get("input")
    if not isinstance(graph_input, Mapping):
        return None
    messages = graph_input.get("messages")
    if not isinstance(messages, list):
        return None

    for value in reversed(messages):
        if not isinstance(value, Mapping):
            continue
        message_type = value.get("type")
        message_role = value.get("role")
        if message_type != "human" and message_role != "user":
            continue
        additional_kwargs = value.get("additional_kwargs")
        if isinstance(additional_kwargs, Mapping) and additional_kwargs.get("hide_from_ui") is True:
            continue
        if not isinstance(value.get("content"), (str, list)):
            continue

        message_id = value.get("id")
        content = {
            "type": "human",
            "id": message_id if isinstance(message_id, str) and message_id else f"run-admission-{record.run_id}",
            "content": copy.deepcopy(value["content"]),
        }
        if isinstance(additional_kwargs, Mapping):
            content["additional_kwargs"] = _public_run_metadata(additional_kwargs)
        return {
            "run_id": record.run_id,
            "seq": 0,
            "content": content,
            "metadata": {"source": "run_admission"},
            "created_at": _timestamp(record.created_at),
        }
    return None


def _prepend_admitted_human_input_response(
    record: PrivateRunRecord,
    records: list[dict[str, Any]],
    *,
    include_admission: bool,
) -> list[dict[str, Any]]:
    """Recover a promoted response when the Worker journal omitted its input."""

    if not include_admission:
        return records
    graph_input = record.kwargs.get("input")
    if not isinstance(graph_input, Mapping):
        return records
    messages = graph_input.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        return records
    message = messages[0]
    if not isinstance(message, Mapping):
        return records
    message_type = message.get("type") or message.get("role")
    additional_kwargs = message.get("additional_kwargs")
    if message_type not in {"human", "user"} or not isinstance(additional_kwargs, Mapping) or additional_kwargs.get("hide_from_ui") is not True:
        return records
    response = read_human_input_response(additional_kwargs)
    if response is None:
        return records
    for existing in records:
        content = existing.get("content")
        if not isinstance(content, Mapping):
            continue
        existing_additional = content.get("additional_kwargs")
        if isinstance(existing_additional, Mapping) and read_human_input_response(existing_additional) == response:
            return records
    recovered = {
        "run_id": record.run_id,
        "seq": 0,
        "content": copy.deepcopy(dict(message)),
        "metadata": {"caller": "lead_agent", "source": "run_admission"},
        "created_at": _timestamp(record.created_at),
    }
    return [recovered, *records]


def _private_rest_feed_cursor(
    raw_cursor: str | None,
    request_id: str,
) -> int | None:
    if raw_cursor is None:
        return None
    try:
        return parse_stream_cursor(raw_cursor)
    except ValueError:
        raise PrivateWorkInvalid(request_id) from None


@router.post(
    "/threads/{thread_id}/runs",
    response_model=PrivateRunResponse,
)
async def create_private_run(
    thread_id: uuid.UUID,
    body: PrivateRunCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_config),
) -> PrivateRunResponse:
    try:
        thread_id_value = str(thread_id)
        await _browser_chat_run_service(
            request,
            context,
            thread_id_value,
        )
        normalized_body = await _normalize_prepared_edit_replay(
            body,
            thread_id=thread_id_value,
            context=context,
            service=(_chat_control_service(request, context.request_id) if _prepared_history_replay_kind(body.metadata) is not None else None),
            app_config=config,
        )
        record = await start_private_run(
            normalized_body,
            thread_id_value,
            request,
            context,
            context_rebase_reason=_prepared_history_replay_kind(
                normalized_body.metadata,
            ),
        )
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
    config: AppConfig = Depends(get_config),
) -> StreamingResponse:
    try:
        thread_id_value = str(thread_id)
        service = await _browser_chat_run_service(
            request,
            context,
            thread_id_value,
        )
        bridge = streaming._private_stream_bridge(request, context.request_id)
        cursor = streaming._private_stream_cursor(request, context.request_id)
        normalized_body = await _normalize_prepared_edit_replay(
            body,
            thread_id=thread_id_value,
            context=context,
            service=(_chat_control_service(request, context.request_id) if _prepared_history_replay_kind(body.metadata) is not None else None),
            app_config=config,
        )
        record = await start_private_run(
            normalized_body,
            thread_id_value,
            request,
            context,
            context_rebase_reason=_prepared_history_replay_kind(
                normalized_body.metadata,
            ),
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
            thread_id=str(thread_id),
            run_id=record.run_id,
            request=request,
            cursor=cursor,
            initial_frames=(),
            cancel_on_disconnect=record.on_disconnect == DisconnectMode.cancel,
            wakeup=streaming._run_event_wakeup(request),
        ),
        media_type="text/event-stream",
        headers=streaming._private_stream_headers(
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
        service = await _browser_chat_run_service(
            request,
            context,
            selected_thread_id,
        )
        bridge = streaming._private_stream_bridge(request, context.request_id)
        cursor = streaming._private_stream_cursor(request, context.request_id)
        await streaming._await_stream_database_operation(
            service.get(
                context,
                selected_thread_id,
                selected_run_id,
            )
        )
        full_state_horizon = await streaming._read_private_full_state_horizon(
            bridge,
            context,
            selected_thread_id,
            selected_run_id,
        )
        initial_frames = await streaming._read_private_stream_page(
            bridge,
            context,
            selected_thread_id,
            selected_run_id,
            cursor,
            full_state_horizon,
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
            wakeup=streaming._run_event_wakeup(request),
            full_state_horizon=full_state_horizon,
        ),
        media_type="text/event-stream",
        headers=streaming._private_stream_headers(
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
    config: AppConfig = Depends(get_config),
) -> dict[str, Any]:
    try:
        thread_id_value = str(thread_id)
        service = await _browser_chat_run_service(
            request,
            context,
            thread_id_value,
        )
        _require_run_runtime(request, context.request_id)
        normalized_body = await _normalize_prepared_edit_replay(
            body,
            thread_id=thread_id_value,
            context=context,
            service=(_chat_control_service(request, context.request_id) if _prepared_history_replay_kind(body.metadata) is not None else None),
            app_config=config,
        )
        record = await start_private_run(
            normalized_body,
            thread_id_value,
            request,
            context,
            context_rebase_reason=_prepared_history_replay_kind(
                normalized_body.metadata,
            ),
        )
        completed, durable_record = await _wait_for_durable_private_run(
            service=service,
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

        snapshot = await bind_scoped_checkpoint_state(
            _scoped_checkpointer(request, context.request_id),
            context,
            config,
            as_node="wait",
        ).aget(checkpoint_config(str(thread_id)))
        if snapshot_checkpoint_id(snapshot) is None:
            raise PrivateWorkNotFound(context.request_id)
        values = serialize_channel_values_for_api(dict(snapshot.values or {}))
        return await _project_scoped_checkpoint_token_usage(
            request,
            context,
            values,
        )
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
) -> list[PrivateRunResponse]:
    try:
        service = await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        records = await service.list(
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
        service = await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        record = await service.get(
            context,
            str(thread_id),
            str(run_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _run_response(record)


@router.get(
    "/threads/{thread_id}/runs/{run_id}/execution-state",
    response_model=PrivateRunExecutionStateResponse,
)
async def get_private_run_execution_state(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    context: PrivateWorkContext = Depends(private_work_context),
    session: AsyncSession = Depends(project_session),
    config: AppConfig = Depends(get_config),
) -> PrivateRunExecutionStateResponse:
    try:
        await PrivateWorkRevalidator().require(
            session,
            context,
            Capability.PRIVATE_WORK_READ_OWN,
        )
        projection = await read_run_execution_state(
            session,
            context,
            str(thread_id),
            str(run_id),
            RunExecutionStatePolicy(
                worker_fresh_for_seconds=config.worker.heartbeat_seconds * 3,
            ),
        )
        if type(projection) is RunExecutionStateUnavailable:
            raise PrivateWorkUnavailable(context.request_id)
        if type(projection) is not RunExecutionState:
            raise PrivateWorkUnavailable(context.request_id)
    except PrivateWorkDatabaseUnavailable:
        _raise_http(PrivateWorkUnavailable(context.request_id))
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateRunExecutionStateResponse(
        phase=projection.phase,
        observed_at=projection.observed_at,
        phase_started_at=projection.phase_started_at,
        execution_started_at=projection.execution_started_at,
        retry_at=projection.retry_at,
        run_status=projection.run_status,
    )


@router.get(
    "/threads/{thread_id}/runs/{run_id}/workspace-changes",
    response_model=dict[str, Any],
)
async def get_private_run_workspace_changes(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    include_files: bool = Query(default=True),
    include_diff: bool = Query(default=True),
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, Any]:
    """Return the scoped workspace/output changes recorded for one chat Run."""

    try:
        service = await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        await service.get(
            context,
            str(thread_id),
            str(run_id),
        )
        return await get_workspace_changes_response(
            _run_event_store(request, context.request_id),
            str(thread_id),
            str(run_id),
            include_files=include_files,
            include_diff=include_diff,
            scope=context.resource_scope,
        )
    except PrivateWorkError as error:
        _raise_http(error)


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
        service = await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        if action != "interrupt":
            # Durable rollback requires an explicit checkpoint restore job;
            # silently treating it as interrupt would violate the SDK contract.
            raise PrivateWorkConflict(context.request_id)
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
        service = await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        await service.delete(
            context,
            str(thread_id),
            str(run_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateRunDeleteResponse(success=True)


@router.get(
    "/threads/{thread_id}/runs/{run_id}/messages",
    response_model=PrivateRunMessagesPageResponse,
)
async def list_private_run_messages(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    before_seq: str | None = Query(default=None),
    after_seq: str | None = Query(default=None),
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateRunMessagesPageResponse:
    try:
        before_sequence = _private_rest_feed_cursor(
            before_seq,
            context.request_id,
        )
        after_sequence = _private_rest_feed_cursor(
            after_seq,
            context.request_id,
        )
        if before_sequence is not None and after_sequence is not None:
            raise PrivateWorkInvalid(context.request_id)
        service = await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        run = await service.get(
            context,
            str(thread_id),
            str(run_id),
        )
        records = await _run_event_store(
            request,
            context.request_id,
        ).list_messages_by_run(
            str(thread_id),
            str(run_id),
            limit=limit + 1,
            before_seq=before_sequence,
            after_seq=after_sequence,
            scope=context.resource_scope,
        )
        if not records:
            admitted_message = _admitted_failure_message(
                run,
                before_seq=before_sequence,
                after_seq=after_sequence,
            )
            if admitted_message is not None:
                records = [admitted_message]
        records = _prepend_admitted_human_input_response(
            run,
            records,
            include_admission=(after_sequence is None and (before_sequence is None or before_sequence > 0) and len(records) < limit + 1),
        )
    except PrivateWorkError as error:
        _raise_http(error)

    data, has_more = trim_run_message_page(
        records,
        limit=limit,
        after_seq=after_sequence,
    )
    try:
        data = await _project_scoped_event_durations(
            request,
            context,
            str(thread_id),
            data,
        )
        data = await _project_scoped_event_token_usage(
            request,
            context,
            data,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateRunMessagesPageResponse(
        data=[_run_message_response(record) for record in data],
        has_more=has_more,
    )


@router.get(
    "/threads/{thread_id}/messages",
    response_model=list[dict[str, Any]],
)
async def list_private_thread_messages(
    thread_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    before_seq: str | None = Query(default=None),
    after_seq: str | None = Query(default=None),
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[dict[str, Any]]:
    try:
        before_sequence = _private_rest_feed_cursor(
            before_seq,
            context.request_id,
        )
        after_sequence = _private_rest_feed_cursor(
            after_seq,
            context.request_id,
        )
        if before_sequence is not None and after_sequence is not None:
            raise PrivateWorkInvalid(context.request_id)
        service = await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        await service.list(
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
            before_seq=before_sequence,
            after_seq=after_sequence,
            scope=context.resource_scope,
        )
        records = await _project_scoped_event_durations(
            request,
            context,
            str(thread_id),
            records,
        )
        records = await _project_scoped_event_token_usage(
            request,
            context,
            records,
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
    service = await _browser_chat_run_service(
        request,
        context,
        str(thread_id),
    )
    await service.get(
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
    records = await _project_scoped_event_token_usage(
        request,
        context,
        records,
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
    after_seq: str | None = Query(default=None),
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[dict[str, Any]]:
    try:
        after_sequence = _private_rest_feed_cursor(
            after_seq,
            context.request_id,
        )
        return await _private_run_events(
            thread_id=thread_id,
            run_id=run_id,
            request=request,
            context=context,
            event_types=event_types,
            task_id=task_id,
            limit=limit,
            after_seq=after_sequence,
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
    after_seq: str | None = Query(default=None),
    context: PrivateWorkContext = Depends(private_work_context),
) -> list[dict[str, Any]]:
    try:
        after_sequence = _private_rest_feed_cursor(
            after_seq,
            context.request_id,
        )
        return await _private_run_events(
            thread_id=thread_id,
            run_id=run_id,
            request=request,
            context=context,
            event_types=event_types,
            task_id=task_id,
            limit=limit,
            after_seq=after_sequence,
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
        service = await _browser_chat_run_service(
            request,
            context,
            str(thread_id),
        )
        await service.list(
            context,
            str(thread_id),
            limit=1,
            offset=0,
        )
        included_run_ids = await _verified_tracking_run_ids_for_thread(
            request,
            context,
            str(thread_id),
        )
        aggregate = await _run_store(
            request,
            context.request_id,
        ).aggregate_tokens_by_thread(
            str(thread_id),
            include_active=include_active,
            included_run_ids=included_run_ids,
            scope=context.resource_scope,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadTokenUsageResponse(thread_id=str(thread_id), **aggregate)
