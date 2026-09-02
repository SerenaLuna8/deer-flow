from __future__ import annotations

import asyncio
import contextlib
import copy
import uuid
from collections.abc import AsyncIterator, Awaitable, Mapping
from typing import Annotated, Any, Literal

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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.gateway.deps import (
    get_config,
    get_current_agent_runtime_config,
    private_work_context,
    project_session,
    require_project_private_open,
)
from app.gateway.pagination import trim_run_message_page
from app.gateway.private_work_schemas import (
    PrivateRunCreateRequest,
    PrivateThreadContextUsageQuery,
    PrivateThreadTokenUsageResponse,
    PrivateWorkRoute,
    public_run_input_projection,
)
from app.gateway.routers.private_work_routes.contracts import (
    _POSTGRES_BIGINT_MAX,
    PRIVATE_THREAD_TITLE_MAX_LENGTH,  # noqa: F401
    ExecutionApprovalDecisionRequest,
    ExecutionApprovalEnvelopeResponse,
    PrivateEditRegeneratePrepareRequest,
    PrivateEditRegeneratePrepareResponse,
    PrivateFeedbackCreateRequest,
    PrivateFeedbackResponse,
    PrivateFileDeleteResponse,
    PrivateFileResponse,
    PrivateRegeneratePrepareRequest,
    PrivateRegeneratePrepareResponse,
    PrivateRunDeleteResponse,
    PrivateRunExecutionStateResponse,
    PrivateRunMessageResponse,
    PrivateRunMessagesPageResponse,
    PrivateRunResponse,
    PrivateSuggestionsRequest,
    PrivateSuggestionsResponse,
    PrivateThreadBranchRequest,
    PrivateThreadBranchResponse,
    PrivateThreadCompactRequest,
    PrivateThreadCompactResponse,
    PrivateThreadCreateRequest,
    PrivateThreadDeleteResponse,
    PrivateThreadGoalRequest,
    PrivateThreadGoalResponse,
    PrivateThreadPatchRequest,
    PrivateThreadResponse,
    PrivateThreadSearchRequest,
    PrivateThreadSearchResponse,
    PrivateThreadStateResponse,
    PrivateUploadLimitsResponse,
    PrivateUploadProjectStorageResponse,
    PrivateWorkReadinessResponse,
    _execution_approval_response,
    _feedback_response,
    _file_response,
    _public_run_metadata,
    _run_response,
    _thread_response,
    _timestamp,
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
    ExecutionApprovalDeniedResponse as ExecutionApprovalDeniedResponse,
)
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalDomainResponse as ExecutionApprovalDomainResponse,
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
    PrivateRunExecutionProfileResponse as PrivateRunExecutionProfileResponse,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _browser_chat_run_service,
    _chat_control_service,
    _execution_approval_service,
    _feedback_service,
    _file_service,
    _file_streamer,
    _raise_http,
    _run_event_store,
    _run_service,
    _run_store,
    _runtime_dependency,
    _scoped_checkpointer,
    _thread_service,
)
from app.gateway.run_event_wakeup import RunEventWakeup
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    checkpoint_config,
    snapshot_checkpoint_id,
)
from app.private_work.checkpointer import (
    PRIVATE_SCOPE_MARKER,
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
from app.private_work.file_service import PrivateUploadLimits
from app.private_work.file_streaming import (
    private_streaming_response,
)
from app.private_work.http_runtime import format_sse, start_private_run
from app.private_work.message_projection import (
    compute_run_durations,
    project_checkpoint_message_durations,
    project_event_message_durations,
)
from app.private_work.readiness_service import (
    PrivateWorkReadinessService,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_execution_state import (
    RunExecutionState,
    RunExecutionStatePolicy,
    RunExecutionStateUnavailable,
    read_run_execution_state,
)
from app.private_work.run_repository import PrivateRunRecord
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import ThreadAgentRef
from app.projects.capabilities import Capability
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityDatabaseUnavailable,
    ReliabilityError,
    ReliabilityInvalidStreamCursor,
)
from deerflow.agents.human_input import read_human_input_response
from deerflow.config.app_config import AppConfig
from deerflow.persistence.private_work.file_repository import (
    PRIVATE_FILE_CHUNK_SIZE,
)
from deerflow.runtime import DisconnectMode, serialize_channel_values_for_api
from deerflow.runtime.context_evidence import ContextProjectionHead
from deerflow.runtime.events.models import (
    STREAM_TERMINAL_ERROR_CODES,
    StoredStreamFrame,
    StreamCursorOutOfRange,
    stream_terminal_status_for_run_settlement,
)
from deerflow.runtime.events.stream import (
    PostgresStreamBridge,
    parse_stream_cursor,
)
from deerflow.runtime.public_token_usage import (
    project_public_persisted_run_event,
    project_public_token_usage,
)
from deerflow.runtime.runs.private_file_lifecycle import await_despite_cancellation
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.utils.messages import message_to_text
from deerflow.utils.time import coerce_iso
from deerflow.workspace_changes import get_workspace_changes_response

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


async def _upload_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(PRIVATE_FILE_CHUNK_SIZE):
        yield chunk


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
_PRIVATE_STREAM_WAKEUP_WAIT_SECONDS = 2.5
_PRIVATE_STREAM_HEARTBEAT_SECONDS = 15.0
_PRIVATE_RUN_TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})


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


def _run_event_wakeup(request: Request) -> RunEventWakeup | None:
    """Return the per-process wakeup dispatcher; absence degrades to polling."""
    wakeup = getattr(request.app.state, "run_event_wakeup", None)
    return wakeup if isinstance(wakeup, RunEventWakeup) else None


def _private_stream_cursor(request: Request, request_id: str) -> int:
    raw_cursor = request.headers.get("Last-Event-ID")
    if raw_cursor is None or raw_cursor == "":
        return 0
    try:
        return parse_stream_cursor(raw_cursor)
    except ValueError:
        raise ReliabilityInvalidStreamCursor(request_id)


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


async def _await_stream_database_operation[T](operation: Awaitable[T]) -> T:
    """Finish one session-owning operation before propagating cancellation."""

    outcome = await await_despite_cancellation(operation)
    if outcome.cancellation_pending:
        if not outcome.task.cancelled():
            outcome.task.exception()
        raise asyncio.CancelledError
    return outcome.result()


async def _read_private_stream_page(
    bridge: PostgresStreamBridge,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    cursor: int,
    full_state_horizon: int | None = None,
) -> tuple[StoredStreamFrame, ...]:
    try:
        return await _await_stream_database_operation(
            bridge.read_after(
                context.resource_scope,
                thread_id,
                cursor=cursor,
                limit=100,
                run_id=run_id,
                full_state_horizon=full_state_horizon,
            )
        )
    except StreamCursorOutOfRange:
        raise ReliabilityInvalidStreamCursor(context.request_id) from None
    except DBAPIError:
        raise ReliabilityDatabaseUnavailable(context.request_id) from None


async def _read_private_full_state_horizon(
    bridge: PostgresStreamBridge,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
) -> int:
    """Freeze the reconnect replay compaction horizon once per connection.

    Root ``values`` frames carry the complete Run state, so catch-up replay
    only needs the newest one; frames at or above the horizon (including the
    live tail) are never dropped.
    """
    try:
        return await _await_stream_database_operation(
            bridge.latest_full_state_seq(
                context.resource_scope,
                thread_id,
                run_id=run_id,
            )
        )
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
    wakeup: RunEventWakeup | None = None,
    full_state_horizon: int | None = None,
) -> AsyncIterator[str]:
    frames = initial_frames
    pending_terminal: StoredStreamFrame | None = None
    disconnected = False
    cancelled = False
    terminal_emitted = False
    loop = asyncio.get_running_loop()
    next_heartbeat = loop.time() + _PRIVATE_STREAM_HEARTBEAT_SECONDS
    waiter = wakeup.subscribe(run_id) if wakeup is not None else None
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

            # A persisted stream.end is the immutable browser cursor fact.
            # Do not ask the settled Run row to rewrite or reinterpret an
            # event that another consumer may already have observed.
            if pending_terminal is not None:
                terminal_cursor = int(pending_terminal.id)
                if terminal_cursor > cursor:
                    cursor = terminal_cursor
                    terminal_emitted = True
                    yield format_sse(
                        pending_terminal.event,
                        pending_terminal.data,
                        event_id=pending_terminal.id,
                    )
                return

            if pending_terminal is None:
                # Re-arm before reading so a NOTIFY that lands during the read
                # is not lost between this page and the next idle wait.
                if waiter is not None:
                    waiter.clear()
                frames = await _read_private_stream_page(
                    bridge,
                    context,
                    thread_id,
                    run_id,
                    cursor,
                    full_state_horizon,
                )
                if frames:
                    continue

            record = await _await_stream_database_operation(service.get(context, thread_id, run_id))
            if record.status in _PRIVATE_RUN_TERMINAL_STATUSES:
                terminal = await _await_stream_database_operation(
                    bridge.ensure_settled_terminal(
                        context.resource_scope,
                        thread_id,
                        run_id,
                        status=stream_terminal_status_for_run_settlement(
                            RunStatus(record.status),
                        ),
                        error_code=(record.error if record.error in STREAM_TERMINAL_ERROR_CODES else None),
                    )
                )
                terminal_cursor = int(terminal.id)
                if terminal_cursor > cursor:
                    cursor = terminal_cursor
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
            idle_seconds = _PRIVATE_STREAM_WAKEUP_WAIT_SECONDS if waiter is not None and wakeup is not None and wakeup.listening else _PRIVATE_STREAM_POLL_SECONDS
            deadline = min(idle_seconds, max(0.001, next_heartbeat - loop.time()))
            if waiter is None:
                await asyncio.sleep(deadline)
            else:
                # NOTIFY is only an alarm clock: the timeout fallback keeps the
                # legacy poll behavior whenever a notification is lost.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(waiter.wait(), timeout=deadline)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if wakeup is not None and waiter is not None:
            wakeup.unsubscribe(run_id, waiter)
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


@router.get(
    "/threads/{thread_id}/execution-approvals/active",
    response_model=ExecutionApprovalEnvelopeResponse,
)
async def get_active_execution_approval(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ExecutionApprovalEnvelopeResponse:
    try:
        projection = await _execution_approval_service(
            request,
            context.request_id,
        ).active(context, str(thread_id))
    except PrivateWorkError as error:
        _raise_http(error)
    return _execution_approval_response(projection)


@router.get(
    "/threads/{thread_id}/execution-approvals/{approval_id}",
    response_model=ExecutionApprovalEnvelopeResponse,
)
async def get_execution_approval(
    thread_id: uuid.UUID,
    approval_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ExecutionApprovalEnvelopeResponse:
    try:
        projection = await _execution_approval_service(
            request,
            context.request_id,
        ).get(context, str(thread_id), approval_id)
    except PrivateWorkError as error:
        _raise_http(error)
    return _execution_approval_response(projection)


@router.post(
    "/threads/{thread_id}/runs/{source_run_id}/execution-approvals/{approval_id}/decision",
    response_model=ExecutionApprovalEnvelopeResponse,
)
async def decide_execution_approval(
    thread_id: uuid.UUID,
    source_run_id: uuid.UUID,
    approval_id: uuid.UUID,
    body: ExecutionApprovalDecisionRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ExecutionApprovalEnvelopeResponse:
    try:
        projection = await _execution_approval_service(
            request,
            context.request_id,
        ).decide(
            context,
            thread_id=str(thread_id),
            source_run_id=str(source_run_id),
            approval_id=approval_id,
            decision=body.decision,
            expected_version=int(body.expected_version),
            idempotency_key=body.idempotency_key,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _execution_approval_response(projection)


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
        bridge = _private_stream_bridge(request, context.request_id)
        cursor = _private_stream_cursor(request, context.request_id)
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
            wakeup=_run_event_wakeup(request),
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
        service = await _browser_chat_run_service(
            request,
            context,
            selected_thread_id,
        )
        bridge = _private_stream_bridge(request, context.request_id)
        cursor = _private_stream_cursor(request, context.request_id)
        await _await_stream_database_operation(
            service.get(
                context,
                selected_thread_id,
                selected_run_id,
            )
        )
        full_state_horizon = await _read_private_full_state_horizon(
            bridge,
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
            wakeup=_run_event_wakeup(request),
            full_state_horizon=full_state_horizon,
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
