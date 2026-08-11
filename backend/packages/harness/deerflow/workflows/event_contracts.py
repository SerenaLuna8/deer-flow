"""Closed Workflow event payload authority shared by transport and storage."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from deerflow.workflows.contracts import MAX_SAFE_JSON_INTEGER, WorkflowNodeKind

_MAX_SAFE_PREVIEW_BYTES = 65_536


def _require_utf8_byte_length(value: str, *, minimum: int = 0, maximum: int) -> str:
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("Workflow text must contain only Unicode scalar values") from error
    if length < minimum or length > maximum:
        raise ValueError(f"Workflow text must contain between {minimum} and {maximum} UTF-8 bytes")
    return value


def _utf8_byte_validator(*, minimum: int = 0, maximum: int):
    def validate(value: str) -> str:
        return _require_utf8_byte_length(value, minimum=minimum, maximum=maximum)

    return validate


_SafeIdentifier = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
_PositiveInt = Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_JSON_INTEGER)]
_NonNegativeInt = Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_JSON_INTEGER)]
type WorkflowEventActivationId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]
type WorkflowEventDatabasePositiveInt = Annotated[
    StrictInt,
    Field(ge=1, le=2_147_483_647),
]
_SafeMessage = Annotated[StrictStr, AfterValidator(_utf8_byte_validator(minimum=1, maximum=2_048))]
_SafePreviewText = Annotated[StrictStr, AfterValidator(_utf8_byte_validator(maximum=_MAX_SAFE_PREVIEW_BYTES))]
_WorkflowDeltaText = Annotated[StrictStr, AfterValidator(_utf8_byte_validator(maximum=16_384))]
_WorkflowLogText = Annotated[StrictStr, AfterValidator(_utf8_byte_validator(maximum=65_536))]


class _StrictWorkflowEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SafePreviewV1(_StrictWorkflowEventContract):
    format: Literal["text", "json", "summary"]
    text: _SafePreviewText
    truncated: StrictBool
    redacted: StrictBool
    original_byte_count: _NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_original_byte_count(self) -> Self:
        retained_bytes = len(self.text.encode("utf-8"))
        if self.original_byte_count is not None and self.original_byte_count < retained_bytes:
            raise ValueError("preview original byte count cannot be below retained UTF-8 bytes")
        return self


class WorkflowErrorCode(StrEnum):
    WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"
    WORKFLOW_DRAFT_CONFLICT = "WORKFLOW_DRAFT_CONFLICT"
    WORKFLOW_DRAFT_INVALID = "WORKFLOW_DRAFT_INVALID"
    WORKFLOW_VERSION_NOT_EXECUTABLE = "WORKFLOW_VERSION_NOT_EXECUTABLE"
    WORKFLOW_NODE_TYPE_UNAVAILABLE = "WORKFLOW_NODE_TYPE_UNAVAILABLE"
    WORKFLOW_DEPENDENCY_STALE = "WORKFLOW_DEPENDENCY_STALE"
    WORKFLOW_INPUT_INVALID = "WORKFLOW_INPUT_INVALID"
    WORKFLOW_RUN_CONFLICT = "WORKFLOW_RUN_CONFLICT"
    WORKFLOW_RUN_NOT_RESUMABLE = "WORKFLOW_RUN_NOT_RESUMABLE"
    WORKFLOW_RUN_RETRY_FORBIDDEN = "WORKFLOW_RUN_RETRY_FORBIDDEN"
    WORKFLOW_WAIT_CONFLICT = "WORKFLOW_WAIT_CONFLICT"
    WORKFLOW_WAIT_EXPIRED = "WORKFLOW_WAIT_EXPIRED"
    WORKFLOW_OUTPUT_INVALID = "WORKFLOW_OUTPUT_INVALID"
    WORKFLOW_COMPILER_UNAVAILABLE = "WORKFLOW_COMPILER_UNAVAILABLE"
    WORKFLOW_RUNTIME_POLICY_UNAVAILABLE = "WORKFLOW_RUNTIME_POLICY_UNAVAILABLE"
    WORKFLOW_RUNTIME_PROFILE_PENDING = "WORKFLOW_RUNTIME_PROFILE_PENDING"
    WORKFLOW_CODE_INVALID = "WORKFLOW_CODE_INVALID"
    WORKFLOW_CODE_SYNTAX_ERROR = "WORKFLOW_CODE_SYNTAX_ERROR"
    WORKFLOW_CODE_SANDBOX_UNAVAILABLE = "WORKFLOW_CODE_SANDBOX_UNAVAILABLE"
    WORKFLOW_CODE_SANDBOX_CLEANUP_FAILED = "WORKFLOW_CODE_SANDBOX_CLEANUP_FAILED"
    WORKFLOW_CODE_INFRASTRUCTURE_ERROR = "WORKFLOW_CODE_INFRASTRUCTURE_ERROR"
    WORKFLOW_CODE_TIMEOUT = "WORKFLOW_CODE_TIMEOUT"
    WORKFLOW_CODE_RESOURCE_EXHAUSTED = "WORKFLOW_CODE_RESOURCE_EXHAUSTED"
    WORKFLOW_CODE_OUTPUT_LIMIT = "WORKFLOW_CODE_OUTPUT_LIMIT"
    WORKFLOW_CODE_OUTPUT_INVALID = "WORKFLOW_CODE_OUTPUT_INVALID"
    WORKFLOW_CODE_RUNTIME_ERROR = "WORKFLOW_CODE_RUNTIME_ERROR"
    WORKFLOW_VARIABLE_AGGREGATE_NO_VALUE = "WORKFLOW_VARIABLE_AGGREGATE_NO_VALUE"
    WORKFLOW_VARIABLE_AGGREGATE_AMBIGUOUS = "WORKFLOW_VARIABLE_AGGREGATE_AMBIGUOUS"
    WORKFLOW_LOOP_LIMIT_EXCEEDED = "WORKFLOW_LOOP_LIMIT_EXCEEDED"
    WORKFLOW_HTTP_UNAVAILABLE = "WORKFLOW_HTTP_UNAVAILABLE"
    WORKFLOW_HTTP_ENDPOINT_FORBIDDEN = "WORKFLOW_HTTP_ENDPOINT_FORBIDDEN"
    WORKFLOW_HTTP_REQUEST_INVALID = "WORKFLOW_HTTP_REQUEST_INVALID"
    WORKFLOW_HTTP_TIMEOUT = "WORKFLOW_HTTP_TIMEOUT"
    WORKFLOW_HTTP_RESPONSE_LIMIT = "WORKFLOW_HTTP_RESPONSE_LIMIT"
    WORKFLOW_HTTP_RESPONSE_INVALID = "WORKFLOW_HTTP_RESPONSE_INVALID"
    WORKFLOW_HTTP_TRANSPORT_ERROR = "WORKFLOW_HTTP_TRANSPORT_ERROR"
    SIDE_EFFECT_STATE_UNKNOWN = "SIDE_EFFECT_STATE_UNKNOWN"


class WorkflowEventSafeErrorV1(_StrictWorkflowEventContract):
    code: WorkflowErrorCode
    safe_message: _SafeMessage
    line: _PositiveInt | None = None
    column: _PositiveInt | None = None

    @field_validator("code", mode="before")
    @classmethod
    def parse_exact_public_code(cls, value: object) -> object:
        if type(value) is str:
            return WorkflowErrorCode(value)
        return value


class WorkflowEventUsageV1(_StrictWorkflowEventContract):
    model_calls: _NonNegativeInt | None = None
    input_tokens: _NonNegativeInt | None = None
    output_tokens: _NonNegativeInt | None = None


class WorkflowEmptyEventPayloadV1(_StrictWorkflowEventContract):
    pass


class WorkflowNodeLifecycleEventPayloadV1(_StrictWorkflowEventContract):
    node_type: WorkflowNodeKind


class WorkflowNodeDeltaEventPayloadV1(_StrictWorkflowEventContract):
    node_type: Literal["llm"]
    text: _WorkflowDeltaText
    truncated: StrictBool


class WorkflowNodeLogEventPayloadV1(_StrictWorkflowEventContract):
    node_type: Literal["python_code"]
    stream: Literal["stdout", "stderr"]
    text: _WorkflowLogText
    truncated: StrictBool


class WorkflowNodeCompletedEventPayloadV1(_StrictWorkflowEventContract):
    node_type: WorkflowNodeKind
    duration_ms: _NonNegativeInt
    output_preview: SafePreviewV1 | None = None
    usage: WorkflowEventUsageV1 | None = None
    branch_port_id: _SafeIdentifier | None = None
    retry_count: _NonNegativeInt | None = None
    truncated: StrictBool | None = None


class WorkflowNodeFailedEventPayloadV1(_StrictWorkflowEventContract):
    node_type: WorkflowNodeKind
    duration_ms: _NonNegativeInt
    error: WorkflowEventSafeErrorV1
    retry_count: _NonNegativeInt | None = None


class WorkflowRunCompletedEventPayloadV1(_StrictWorkflowEventContract):
    duration_ms: _NonNegativeInt
    output_preview: SafePreviewV1 | None = None


class WorkflowRunFailedEventPayloadV1(_StrictWorkflowEventContract):
    duration_ms: _NonNegativeInt
    error: WorkflowEventSafeErrorV1


class WorkflowRunCancelledEventPayloadV1(_StrictWorkflowEventContract):
    duration_ms: _NonNegativeInt | None = None


class WorkflowRunSideEffectUnknownEventPayloadV1(_StrictWorkflowEventContract):
    code: Literal["SIDE_EFFECT_STATE_UNKNOWN"]
    safe_message: _SafeMessage


type WorkflowEventTypeV1 = Literal[
    "workflow.run.started",
    "workflow.node.queued",
    "workflow.node.started",
    "workflow.node.delta",
    "workflow.node.log",
    "workflow.node.completed",
    "workflow.node.failed",
    "workflow.run.completed",
    "workflow.run.failed",
    "workflow.run.cancelled",
    "workflow.run.side_effect_unknown",
]

_WORKFLOW_EVENT_PAYLOAD_ADAPTERS: dict[str, TypeAdapter[BaseModel]] = {
    "workflow.run.started": TypeAdapter(WorkflowEmptyEventPayloadV1),
    "workflow.node.queued": TypeAdapter(WorkflowNodeLifecycleEventPayloadV1),
    "workflow.node.started": TypeAdapter(WorkflowNodeLifecycleEventPayloadV1),
    "workflow.node.delta": TypeAdapter(WorkflowNodeDeltaEventPayloadV1),
    "workflow.node.log": TypeAdapter(WorkflowNodeLogEventPayloadV1),
    "workflow.node.completed": TypeAdapter(WorkflowNodeCompletedEventPayloadV1),
    "workflow.node.failed": TypeAdapter(WorkflowNodeFailedEventPayloadV1),
    "workflow.run.completed": TypeAdapter(WorkflowRunCompletedEventPayloadV1),
    "workflow.run.failed": TypeAdapter(WorkflowRunFailedEventPayloadV1),
    "workflow.run.cancelled": TypeAdapter(WorkflowRunCancelledEventPayloadV1),
    "workflow.run.side_effect_unknown": TypeAdapter(WorkflowRunSideEffectUnknownEventPayloadV1),
}
WORKFLOW_EVENT_TYPES = frozenset(_WORKFLOW_EVENT_PAYLOAD_ADAPTERS)
_WORKFLOW_EVENT_ACTIVATION_ID_ADAPTER = TypeAdapter(WorkflowEventActivationId)
_WORKFLOW_EVENT_DATABASE_POSITIVE_INT_ADAPTER = TypeAdapter(WorkflowEventDatabasePositiveInt)


def canonical_workflow_event_activation_id(value: object) -> str:
    return _WORKFLOW_EVENT_ACTIVATION_ID_ADAPTER.validate_python(value)


def canonical_workflow_event_database_positive_int(
    value: object,
    *,
    field_name: str,
) -> int:
    try:
        return _WORKFLOW_EVENT_DATABASE_POSITIVE_INT_ADAPTER.validate_python(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a positive PostgreSQL INTEGER") from error


def canonical_workflow_event_payload_v1(
    event_type: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Validate one closed event shape and return its canonical JSON form."""

    adapter = _WORKFLOW_EVENT_PAYLOAD_ADAPTERS.get(event_type)
    if adapter is None:
        raise ValueError("unsupported Workflow event type")
    if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
        raise TypeError("Workflow event payload must be an object mapping")
    model = adapter.validate_python(dict(payload))
    return cast(
        dict[str, object],
        model.model_dump(mode="json", exclude_none=True),
    )


__all__ = [
    "SafePreviewV1",
    "WORKFLOW_EVENT_TYPES",
    "WorkflowEmptyEventPayloadV1",
    "WorkflowErrorCode",
    "WorkflowEventActivationId",
    "WorkflowEventDatabasePositiveInt",
    "WorkflowEventSafeErrorV1",
    "WorkflowEventTypeV1",
    "WorkflowEventUsageV1",
    "WorkflowNodeCompletedEventPayloadV1",
    "WorkflowNodeDeltaEventPayloadV1",
    "WorkflowNodeFailedEventPayloadV1",
    "WorkflowNodeLifecycleEventPayloadV1",
    "WorkflowNodeLogEventPayloadV1",
    "WorkflowRunCancelledEventPayloadV1",
    "WorkflowRunCompletedEventPayloadV1",
    "WorkflowRunFailedEventPayloadV1",
    "WorkflowRunSideEffectUnknownEventPayloadV1",
    "canonical_workflow_event_activation_id",
    "canonical_workflow_event_database_positive_int",
    "canonical_workflow_event_payload_v1",
]
