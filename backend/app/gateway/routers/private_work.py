from __future__ import annotations

import asyncio
import contextlib
import copy
import uuid
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import asdict
from datetime import datetime
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
from pydantic import Field, model_validator
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
    PrivateThreadContextUsageResponse,
    PrivateThreadTokenUsageResponse,
    PrivateWorkRoute,
    StrictPrivateWorkRequest,
    StrictPrivateWorkResponse,
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
    ProjectScopedCheckpointer,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.execution_approval import (
    ExecutionApprovalProjection,
    ExecutionApprovalService,
)
from app.private_work.execution_profile import (
    RunExecutionProfileUnsupported,
    effective_run_execution_profile_from_kwargs,
)
from app.private_work.feedback_service import (
    PrivateFeedbackRecord,
    PrivateFeedbackService,
)
from app.private_work.file_service import PrivateFileService, PrivateUploadLimits
from app.private_work.file_streaming import (
    PrivateFileStreamer,
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
    ReadinessStatus,
)
from app.private_work.run_repository import PrivateRunRecord
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRecord, ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService
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
    PrivateFileRecord,
)
from deerflow.runtime import DisconnectMode, RunRecord, serialize_channel_values_for_api
from deerflow.runtime.events.models import (
    STREAM_TERMINAL_ERROR_CODES,
    StoredStreamFrame,
    StreamCursorOutOfRange,
)
from deerflow.runtime.events.store import RunEventStore
from deerflow.runtime.events.stream import (
    PostgresStreamBridge,
    parse_stream_cursor,
)
from deerflow.runtime.goal import DEFAULT_MAX_GOAL_CONTINUATIONS
from deerflow.runtime.runs.private_file_lifecycle import await_despite_cancellation
from deerflow.runtime.runs.store import RunStore
from deerflow.utils.messages import message_to_text
from deerflow.utils.time import coerce_iso

router = APIRouter(
    prefix="/api/projects/{project_id}/private-work",
    tags=["project-private-work"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
PRIVATE_THREAD_TITLE_MAX_LENGTH = 200


class PrivateThreadCreateRequest(StrictPrivateWorkRequest):
    thread_id: uuid.UUID
    agent_asset_id: uuid.UUID | None = None
    agent_scope: Literal["project", "system"] | None = None
    display_name: str | None = Field(
        default=None,
        max_length=PRIVATE_THREAD_TITLE_MAX_LENGTH,
    )
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_agent_selection(self) -> PrivateThreadCreateRequest:
        selection_fields = {"agent_asset_id", "agent_scope"}
        provided_fields = selection_fields.intersection(self.model_fields_set)
        if provided_fields and (provided_fields != selection_fields or self.agent_asset_id is None or self.agent_scope is None):
            raise ValueError("agent_asset_id and agent_scope must be provided together")
        return self


class PrivateThreadSearchRequest(StrictPrivateWorkRequest):
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(
        default=0,
        ge=0,
        le=_POSTGRES_BIGINT_MAX,
        json_schema_extra={
            "format": "int64",
            "x-postgres-bigint-maximum": str(_POSTGRES_BIGINT_MAX),
        },
    )


class PrivateThreadPatchRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(
        ge=1,
        le=_POSTGRES_BIGINT_MAX,
        json_schema_extra={
            "format": "int64",
            "x-postgres-bigint-maximum": str(_POSTGRES_BIGINT_MAX),
        },
    )
    display_name: str | None = Field(
        default=None,
        max_length=PRIVATE_THREAD_TITLE_MAX_LENGTH,
    )


class PrivateThreadResponse(StrictPrivateWorkResponse):
    thread_id: str
    agent_asset_id: str
    agent_scope: str
    display_name: str | None
    status: str
    metadata: dict[str, Any]
    version: int
    created_at: str
    updated_at: str


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


class PrivateRunExecutionProfileResponse(StrictPrivateWorkResponse):
    model_name: str
    thinking_enabled: bool
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    supports_vision: bool


class PrivateRunResponse(StrictPrivateWorkResponse):
    run_id: str
    thread_id: str
    assistant_id: str | None = None
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    multitask_strategy: str = "reject"
    error: str | None = None
    model_name: str | None = None
    execution_profile: PrivateRunExecutionProfileResponse | None = None
    created_at: str = ""
    updated_at: str = ""


class ExecutionApprovalContinuationRunResponse(StrictPrivateWorkResponse):
    run_id: str = Field(min_length=1, max_length=64)
    status: Literal[
        "pending",
        "running",
        "error",
        "success",
        "timeout",
        "interrupted",
    ]


class ExecutionApprovalDomainResponse(StrictPrivateWorkResponse):
    label: str = Field(min_length=1, max_length=256)
    effective_user_label: str = Field(min_length=1, max_length=256)


class ExecutionApprovalSourceAgentResponse(StrictPrivateWorkResponse):
    kind: Literal["lead", "subagent"]
    label: str = Field(min_length=1, max_length=256)
    path: list[str] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_path(self) -> ExecutionApprovalSourceAgentResponse:
        if any(not item or len(item) > 256 for item in self.path):
            raise ValueError("source agent path is invalid")
        return self


class ExecutionApprovalBaseResponse(StrictPrivateWorkResponse):
    approval_id: uuid.UUID
    source_run_id: str = Field(min_length=1, max_length=64)
    source_tool_call_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^(?:0|[1-9][0-9]*)$")
    execution_domain: ExecutionApprovalDomainResponse
    command_preview: str = Field(min_length=1, max_length=65_536)
    cwd_preview: str = Field(min_length=1, max_length=4_096)
    timeout_seconds: int = Field(ge=1, le=3_600)
    source_agent: ExecutionApprovalSourceAgentResponse
    risk_level: Literal["host_execution"]
    warning_code: Literal[
        "LOCAL_PROCESS_RUNS_ON_HOST",
        "HOST_EXECUTION_STATE_UNKNOWN",
    ]
    can_decide: bool
    continuation_run: ExecutionApprovalContinuationRunResponse | None


class ExecutionApprovalPendingResponse(ExecutionApprovalBaseResponse):
    status: Literal["pending"]
    warning_code: Literal["LOCAL_PROCESS_RUNS_ON_HOST"]
    continuation_run: None
    decision_expires_at: datetime
    remaining_ttl_seconds: int = Field(ge=0, le=86_400)


class ExecutionApprovalApprovedResponse(ExecutionApprovalBaseResponse):
    status: Literal["approved"]
    warning_code: Literal["LOCAL_PROCESS_RUNS_ON_HOST"]
    can_decide: Literal[False]
    decision_at: datetime
    claim_expires_at: datetime


class ExecutionApprovalClaimedResponse(ExecutionApprovalBaseResponse):
    status: Literal["claimed"]
    warning_code: Literal["LOCAL_PROCESS_RUNS_ON_HOST"]
    can_decide: Literal[False]
    continuation_run: ExecutionApprovalContinuationRunResponse
    claimed_at: datetime


class ExecutionApprovalFinishedResponse(ExecutionApprovalBaseResponse):
    status: Literal["finished"]
    warning_code: Literal["LOCAL_PROCESS_RUNS_ON_HOST"]
    can_decide: Literal[False]
    exit_code: int
    finished_at: datetime
    result_summary_code: str = Field(min_length=1, max_length=128)


class ExecutionApprovalLaunchFailedResponse(ExecutionApprovalBaseResponse):
    status: Literal["launch_failed"]
    warning_code: Literal["LOCAL_PROCESS_RUNS_ON_HOST"]
    can_decide: Literal[False]
    finished_at: datetime
    reason_code: str = Field(min_length=1, max_length=128)


class ExecutionApprovalUnknownResponse(ExecutionApprovalBaseResponse):
    status: Literal["unknown"]
    warning_code: Literal["HOST_EXECUTION_STATE_UNKNOWN"]
    can_decide: Literal[False]
    finished_at: datetime


class ExecutionApprovalDeniedResponse(ExecutionApprovalBaseResponse):
    status: Literal["denied"]
    warning_code: Literal["LOCAL_PROCESS_RUNS_ON_HOST"]
    can_decide: Literal[False]
    decision_at: datetime
    denial_delivery_status: Literal[
        "not_required",
        "pending",
        "admitted",
        "delivered",
        "failed",
    ]


class ExecutionApprovalClosedResponse(ExecutionApprovalBaseResponse):
    status: Literal["expired", "cancelled"]
    warning_code: Literal["LOCAL_PROCESS_RUNS_ON_HOST"]
    can_decide: Literal[False]
    finished_at: datetime
    reason_code: str = Field(min_length=1, max_length=128)


ExecutionApprovalResponse = Annotated[
    ExecutionApprovalPendingResponse
    | ExecutionApprovalApprovedResponse
    | ExecutionApprovalClaimedResponse
    | ExecutionApprovalFinishedResponse
    | ExecutionApprovalLaunchFailedResponse
    | ExecutionApprovalUnknownResponse
    | ExecutionApprovalDeniedResponse
    | ExecutionApprovalClosedResponse,
    Field(discriminator="status"),
]


class ExecutionApprovalEnvelopeResponse(StrictPrivateWorkResponse):
    schema_version: Literal[1]
    server_time: datetime
    approval: ExecutionApprovalResponse | None


class ExecutionApprovalDecisionRequest(StrictPrivateWorkRequest):
    schema_version: Literal[1]
    decision: Literal["allow_once", "deny"]
    expected_version: str = Field(pattern=r"^(?:0|[1-9][0-9]*)$")
    idempotency_key: uuid.UUID


class PrivateRunDeleteResponse(StrictPrivateWorkResponse):
    success: bool


class PrivateRunMessageResponse(StrictPrivateWorkResponse):
    run_id: str
    seq: str = Field(pattern=r"^(?:0|[1-9][0-9]*)$")
    content: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class PrivateRunMessagesPageResponse(StrictPrivateWorkResponse):
    data: list[PrivateRunMessageResponse]
    has_more: bool


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
    deleted: bool


class PrivateUploadProjectStorageResponse(StrictPrivateWorkResponse):
    policy: Literal["project_quota"]
    remaining_bytes: int = Field(ge=0)


class PrivateUploadLimitsResponse(StrictPrivateWorkResponse):
    max_files: int = Field(ge=1)
    max_file_size: int = Field(ge=1)
    max_total_size: int = Field(ge=1)
    project_storage: PrivateUploadProjectStorageResponse
    request_id: str = Field(min_length=1)


class PrivateWorkReadinessResponse(StrictPrivateWorkResponse):
    status: ReadinessStatus
    code: str
    request_id: str


class PrivateThreadGoalRequest(StrictPrivateWorkRequest):
    objective: str = Field(min_length=1, max_length=4000)
    max_continuations: int = Field(
        default=DEFAULT_MAX_GOAL_CONTINUATIONS,
        ge=0,
        le=DEFAULT_MAX_GOAL_CONTINUATIONS,
    )


class PrivateThreadGoalResponse(StrictPrivateWorkResponse):
    goal: dict[str, Any] | None = None


class PrivateCompactKeep(StrictPrivateWorkRequest):
    type: Literal["fraction", "tokens", "messages"]
    value: int | float = Field(ge=0)

    @model_validator(mode="after")
    def validate_value(self) -> PrivateCompactKeep:
        if self.type != "messages" and float(self.value) <= 0:
            raise ValueError("fraction and tokens must be greater than 0")
        if self.type == "fraction" and float(self.value) > 1:
            raise ValueError("fraction must not exceed 1")
        if self.type in {"tokens", "messages"} and (not isinstance(self.value, int) or isinstance(self.value, bool)):
            raise ValueError("tokens and messages require integer values")
        return self

    def to_tuple(self) -> tuple[str, int | float]:
        return self.type, self.value


class PrivateThreadCompactRequest(StrictPrivateWorkRequest):
    force: bool = True
    keep: PrivateCompactKeep | None = None


class PrivateThreadCompactResponse(StrictPrivateWorkResponse):
    thread_id: str
    compacted: bool
    reason: str | None = None
    removed_message_count: int = 0
    preserved_message_count: int = 0
    summary_updated: bool = False
    checkpoint_id: str | None = None
    total_tokens: int = 0


class PrivateThreadBranchRequest(StrictPrivateWorkRequest):
    message_id: str = Field(min_length=1, max_length=128)
    message_ids: list[str] = Field(default_factory=list, max_length=20)
    title: str | None = Field(
        default=None,
        max_length=PRIVATE_THREAD_TITLE_MAX_LENGTH,
    )

    @model_validator(mode="after")
    def validate_message_ids(self) -> PrivateThreadBranchRequest:
        if any(not value or len(value) > 128 for value in self.message_ids):
            raise ValueError("message ids must be between 1 and 128 characters")
        if len(self.message_ids) != len(set(self.message_ids)):
            raise ValueError("message ids must be unique")
        return self


class PrivateThreadBranchResponse(StrictPrivateWorkResponse):
    thread_id: str
    parent_thread_id: str
    parent_checkpoint_id: str
    branched_from_message_id: str
    workspace_clone_mode: str


class PrivateRegeneratePrepareRequest(StrictPrivateWorkRequest):
    message_id: str = Field(min_length=1, max_length=128)


class PrivateRegeneratePrepareResponse(StrictPrivateWorkResponse):
    input: dict[str, Any]
    checkpoint: dict[str, Any]
    metadata: dict[str, Any]
    target_run_id: str


class PrivateEditRegeneratePrepareRequest(StrictPrivateWorkRequest):
    human_message_id: str = Field(min_length=1, max_length=128)
    replacement_text: str = Field(min_length=1, max_length=65_536)


class PrivateEditRegeneratePrepareResponse(PrivateRegeneratePrepareResponse):
    replacement_human_message_id: str
    source_message_ids: list[str]


class PrivateSuggestionsRequest(StrictPrivateWorkRequest):
    n: int = Field(default=3, ge=1, le=5)


class PrivateSuggestionsResponse(StrictPrivateWorkResponse):
    suggestions: list[str] = Field(default_factory=list)


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
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
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
        if isinstance(key, str) and not key.startswith("__") and key not in {"project_id", "owner_user_id", "user_id"} and not any(part in key.lower() for part in sensitive_parts)
    }


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _run_response(record: PrivateRunRecord | RunRecord) -> PrivateRunResponse:
    raw_status = record.status
    status_value = getattr(raw_status, "value", raw_status)
    try:
        effective_profile = effective_run_execution_profile_from_kwargs(
            record.kwargs,
        )
    except RunExecutionProfileUnsupported:
        effective_profile = None
    return PrivateRunResponse(
        run_id=record.run_id,
        thread_id=record.thread_id,
        assistant_id=record.assistant_id,
        status=str(status_value),
        metadata=_public_run_metadata(record.metadata),
        multitask_strategy=record.multitask_strategy,
        error=record.error,
        model_name=record.model_name,
        execution_profile=(
            PrivateRunExecutionProfileResponse(
                **effective_profile.as_dict(),
            )
            if effective_profile is not None
            else None
        ),
        created_at=_timestamp(record.created_at),
        updated_at=_timestamp(record.updated_at),
    )


def _thread_service(request: Request, request_id: str) -> PrivateThreadService:
    service = getattr(request.app.state, "private_thread_service", None)
    if not isinstance(service, PrivateThreadService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _execution_approval_service(
    request: Request,
    request_id: str,
) -> ExecutionApprovalService:
    service = getattr(request.app.state, "execution_approval_service", None)
    if not isinstance(service, ExecutionApprovalService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _execution_approval_response(
    projection: ExecutionApprovalProjection,
) -> ExecutionApprovalEnvelopeResponse:
    return ExecutionApprovalEnvelopeResponse(
        schema_version=1,
        server_time=projection.server_time,
        approval=projection.approval,
    )


def _chat_control_service(
    request: Request,
    request_id: str,
) -> ProjectChatControlService:
    service = getattr(request.app.state, "project_chat_control_service", None)
    if not isinstance(service, ProjectChatControlService):
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


async def _browser_chat_run_service(
    request: Request,
    context: PrivateWorkContext,
    thread_id: str,
) -> PrivateRunService:
    """Return the Run service after rejecting hidden Builder threads.

    The generic browser Run API is a projection of visible chat threads only.
    Builder orchestration keeps using the same internal Run substrate through
    its dedicated services, never through these public routes.
    """

    service = _run_service(request, context.request_id)
    await service.require_browser_chat_thread(context, thread_id)
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


def _feedback_service(request: Request, request_id: str) -> PrivateFeedbackService:
    service = getattr(request.app.state, "private_feedback_service", None)
    if not isinstance(service, PrivateFeedbackService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _feedback_response(record: PrivateFeedbackRecord) -> PrivateFeedbackResponse:
    return PrivateFeedbackResponse(
        feedback_id=record.feedback_id,
        run_id=record.run_id,
        thread_id=record.thread_id,
        message_id=record.message_id,
        rating=record.rating,
        comment=record.comment,
        created_at=_timestamp(record.created_at),
    )


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
_PRIVATE_STREAM_WAKEUP_WAIT_SECONDS = 2.5
_PRIVATE_STREAM_HEARTBEAT_SECONDS = 15.0
_PRIVATE_RUN_TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})


async def _normalize_prepared_edit_replay(
    body: PrivateRunCreateRequest,
    *,
    thread_id: str,
    context: PrivateWorkContext,
    service: ProjectChatControlService | None,
    app_config: AppConfig,
) -> PrivateRunCreateRequest:
    """Restore a server-validated RemoveMessage for an edited replay."""

    if body.metadata.get("replay_kind") != "edit":
        return body
    if service is None:
        raise PrivateWorkUnavailable(context.request_id)
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


def _fallback_terminal_status(status_value: str) -> str:
    return "completed" if status_value == "success" else status_value


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
) -> tuple[StoredStreamFrame, ...]:
    try:
        return await _await_stream_database_operation(
            bridge.read_after(
                context.resource_scope,
                thread_id,
                cursor=cursor,
                limit=100,
                run_id=run_id,
            )
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
    wakeup: RunEventWakeup | None = None,
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
                        status=_fallback_terminal_status(record.status),
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
    response_model=PrivateThreadContextUsageResponse,
)
async def private_thread_context_usage(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_current_agent_runtime_config),
) -> PrivateThreadContextUsageResponse:
    try:
        usage = await _chat_control_service(
            request,
            context.request_id,
        ).context_usage(
            context,
            str(thread_id),
            app_config=config,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return PrivateThreadContextUsageResponse(
        thread_id=str(thread_id),
        **asdict(usage),
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
            service=(_chat_control_service(request, context.request_id) if body.metadata.get("replay_kind") == "edit" else None),
            app_config=config,
        )
        record = await start_private_run(
            normalized_body,
            thread_id_value,
            request,
            context,
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
            service=(_chat_control_service(request, context.request_id) if body.metadata.get("replay_kind") == "edit" else None),
            app_config=config,
        )
        record = await start_private_run(
            normalized_body,
            thread_id_value,
            request,
            context,
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
            wakeup=_run_event_wakeup(request),
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
            service=(_chat_control_service(request, context.request_id) if body.metadata.get("replay_kind") == "edit" else None),
            app_config=config,
        )
        record = await start_private_run(
            normalized_body,
            thread_id_value,
            request,
            context,
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
        return serialize_channel_values_for_api(dict(snapshot.values or {}))
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
