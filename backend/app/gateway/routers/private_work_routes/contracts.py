from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.gateway.private_work_schemas import StrictPrivateWorkRequest, StrictPrivateWorkResponse
from app.private_work.execution_approval import ExecutionApprovalProjection
from app.private_work.execution_profile import (
    RunExecutionProfileUnsupported,
    effective_run_execution_profile_from_kwargs,
)
from app.private_work.feedback_service import PrivateFeedbackRecord
from app.private_work.readiness_service import ReadinessStatus
from app.private_work.run_repository import PrivateRunRecord
from app.private_work.thread_repository import PrivateThreadRecord
from app.private_work.workload_profile import (
    RUN_WORKLOAD_PROFILE_KWARG,
    RunWorkloadProfileUnsupported,
    parse_persisted_run_workload_profile,
)
from deerflow.persistence.private_work.file_repository import PrivateFileRecord
from deerflow.runtime import RunRecord
from deerflow.runtime.goal import DEFAULT_MAX_GOAL_CONTINUATIONS

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


class PrivateRunExecutionStateResponse(StrictPrivateWorkResponse):
    phase: Literal[
        "queued",
        "waiting_for_worker",
        "starting",
        "executing",
        "retry_wait",
        "waiting_for_lease_expiry",
        "waiting_for_terminalization",
        "waiting_for_recovery",
        "recovering",
        "cancelling",
        "terminal",
    ]
    observed_at: datetime
    phase_started_at: datetime | None
    execution_started_at: datetime | None
    retry_at: datetime | None
    run_status: Literal["pending", "running", "success", "error", "timeout", "interrupted"]


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
    workload_profile: Literal["interactive", "research"] | None = None
    created_at: str = ""
    updated_at: str = ""


class ExecutionApprovalContinuationRunResponse(StrictPrivateWorkResponse):
    run_id: str = Field(min_length=1, max_length=64)
    status: Literal["pending", "running", "error", "success", "timeout", "interrupted"]


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
    warning_code: Literal["LOCAL_PROCESS_RUNS_ON_HOST", "HOST_EXECUTION_STATE_UNKNOWN"]
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
    denial_delivery_status: Literal["not_required", "pending", "admitted", "delivered", "failed"]


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
    max_continuations: int = Field(default=DEFAULT_MAX_GOAL_CONTINUATIONS, ge=0, le=DEFAULT_MAX_GOAL_CONTINUATIONS)


class PrivateThreadGoalResponse(StrictPrivateWorkResponse):
    goal: dict[str, Any] | None = None


class PrivateCompactKeep(StrictPrivateWorkRequest):
    type: Literal["tokens"] = "tokens"
    value: int = Field(ge=1, le=2_000_000)

    def to_tuple(self) -> tuple[str, int]:
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
    title: str | None = Field(default=None, max_length=PRIVATE_THREAD_TITLE_MAX_LENGTH)

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
    sensitive_parts = ("secret", "token", "password", "credential", "ciphertext", "private_key", "key_id", "nonce", "storage_locator")
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
        effective_profile = effective_run_execution_profile_from_kwargs(record.kwargs)
    except RunExecutionProfileUnsupported:
        effective_profile = None
    try:
        frozen_workload_profile = record.kwargs.get(RUN_WORKLOAD_PROFILE_KWARG)
        effective_workload_profile = parse_persisted_run_workload_profile(frozen_workload_profile)[1] if frozen_workload_profile is not None else None
    except RunWorkloadProfileUnsupported:
        effective_workload_profile = None
    return PrivateRunResponse(
        run_id=record.run_id,
        thread_id=record.thread_id,
        assistant_id=record.assistant_id,
        status=str(status_value),
        metadata=_public_run_metadata(record.metadata),
        multitask_strategy=record.multitask_strategy,
        error=record.error,
        model_name=record.model_name,
        execution_profile=PrivateRunExecutionProfileResponse(**effective_profile.as_dict()) if effective_profile is not None else None,
        workload_profile=effective_workload_profile.name if effective_workload_profile is not None else None,
        created_at=_timestamp(record.created_at),
        updated_at=_timestamp(record.updated_at),
    )


def _execution_approval_response(projection: ExecutionApprovalProjection) -> ExecutionApprovalEnvelopeResponse:
    return ExecutionApprovalEnvelopeResponse(schema_version=1, server_time=projection.server_time, approval=projection.approval)


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


def _feedback_response(record: PrivateFeedbackRecord) -> PrivateFeedbackResponse:
    return PrivateFeedbackResponse(feedback_id=record.feedback_id, run_id=record.run_id, thread_id=record.thread_id, message_id=record.message_id, rating=record.rating, comment=record.comment, created_at=_timestamp(record.created_at))
