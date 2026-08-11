"""Strict public and server-private contracts for first-class Workflows.

This module deliberately contains no ORM, router, or runtime implementation.
It freezes transport identities before the control plane and Worker are built.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from deerflow.workflows import (
    MAX_SAFE_JSON_INTEGER,
    CanonicalJsonUtf8BudgetExceeded,
    StrictLiteralOne,
    WorkflowEdgeId,
    canonical_json_value_with_utf8_budget,
)
from deerflow.workflows.event_contracts import (
    SafePreviewV1,
    WorkflowEmptyEventPayloadV1,
    WorkflowErrorCode,
    WorkflowEventActivationId,
    WorkflowEventDatabasePositiveInt,
    WorkflowEventSafeErrorV1,
    WorkflowEventTypeV1,
    WorkflowEventUsageV1,
    WorkflowNodeCompletedEventPayloadV1,
    WorkflowNodeDeltaEventPayloadV1,
    WorkflowNodeFailedEventPayloadV1,
    WorkflowNodeLifecycleEventPayloadV1,
    WorkflowNodeLogEventPayloadV1,
    WorkflowRunCancelledEventPayloadV1,
    WorkflowRunCompletedEventPayloadV1,
    WorkflowRunFailedEventPayloadV1,
    WorkflowRunSideEffectUnknownEventPayloadV1,
    canonical_workflow_event_payload_v1,
)

_SafeIdentifier = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
_SafeCode = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")]
_Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_MAX_WORKFLOW_HTTP_SETTLED_BODY_BYTES = 2_097_152
_MAX_WORKFLOW_HTTP_SETTLED_HEADER_BYTES = 65_536
_MAX_WORKFLOW_HTTP_JSON_DEPTH = 64
_MAX_WORKFLOW_HTTP_JSON_NODES = 65_536


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


def _bounded_http_json_material(value: JsonValue) -> int:
    nodes = 0
    stack: list[tuple[JsonValue, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_WORKFLOW_HTTP_JSON_DEPTH:
            raise ValueError("settled HTTP JSON exceeds the maximum depth")
        nodes += 1
        if nodes > _MAX_WORKFLOW_HTTP_JSON_NODES:
            raise ValueError("settled HTTP JSON exceeds the maximum node count")
        if isinstance(current, bool) or current is None or isinstance(current, str):
            if isinstance(current, str):
                _require_utf8_byte_length(current, maximum=_MAX_WORKFLOW_HTTP_SETTLED_BODY_BYTES)
            continue
        if isinstance(current, int):
            if abs(current) > MAX_SAFE_JSON_INTEGER:
                raise ValueError("settled HTTP JSON integer exceeds the cross-runtime safe range")
            continue
        if isinstance(current, float):
            if not math.isfinite(current) or (current.is_integer() and abs(current) > MAX_SAFE_JSON_INTEGER):
                raise ValueError("settled HTTP JSON number is not portable")
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                _require_utf8_byte_length(key, maximum=_MAX_WORKFLOW_HTTP_SETTLED_BODY_BYTES)
                nodes += 1
                if nodes > _MAX_WORKFLOW_HTTP_JSON_NODES:
                    raise ValueError("settled HTTP JSON exceeds the maximum node count")
                stack.append((item, depth + 1))
            continue
        raise ValueError("settled HTTP JSON contains an unsupported value")

    try:
        _, serialized_bytes = canonical_json_value_with_utf8_budget(
            value,
            max_utf8_bytes=_MAX_WORKFLOW_HTTP_SETTLED_BODY_BYTES,
        )
    except CanonicalJsonUtf8BudgetExceeded as error:
        raise ValueError("settled HTTP JSON exceeds the persisted byte limit") from error
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise ValueError("settled HTTP JSON is not portable JSON") from error
    return serialized_bytes


def _validate_canonical_cursor_range(value: str) -> str:
    if int(value) > _MAX_SIGNED_BIGINT:
        raise ValueError("Workflow event cursor exceeds the signed BIGINT range")
    return value


_CanonicalCursor = Annotated[
    StrictStr,
    Field(pattern=r"^(0|[1-9][0-9]*)$"),
    AfterValidator(_validate_canonical_cursor_range),
]
_OriginTraceId = Annotated[StrictStr, Field(min_length=1, max_length=512, pattern=r"^[\x20-\x7e]+$")]
_HttpHeaderName = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9!#$%&'*+.^_`|~-]+$")]
_PositiveInt = Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_JSON_INTEGER)]
_NonNegativeInt = Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_JSON_INTEGER)]
_CANONICAL_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_canonical_uuid_input(value: object) -> object:
    if isinstance(value, str) and _CANONICAL_UUID_TEXT.fullmatch(value) is None:
        raise ValueError("UUID input must use canonical lowercase hyphenated text")
    return value


_CanonicalUuid = Annotated[uuid.UUID, BeforeValidator(_validate_canonical_uuid_input)]
_SafeMessage = Annotated[StrictStr, AfterValidator(_utf8_byte_validator(minimum=1, maximum=2_048))]
_HttpHeaderValue = Annotated[StrictStr, AfterValidator(_utf8_byte_validator(maximum=4_096))]
_HttpTextBody = Annotated[StrictStr, AfterValidator(_utf8_byte_validator(maximum=_MAX_WORKFLOW_HTTP_SETTLED_BODY_BYTES))]
_HttpSettledByteCount = Annotated[StrictInt, Field(ge=0, le=_MAX_WORKFLOW_HTTP_SETTLED_BODY_BYTES)]


class _StrictWorkflowContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _WorkflowProjectReadinessBaseV1(_StrictWorkflowContract):
    request_id: Annotated[StrictStr, Field(min_length=1, max_length=512, pattern=r"^[\x20-\x7e]+$")]

    @field_validator("workflow_enabled", "schema_ready", "admission_ready", mode="before", check_fields=False)
    @classmethod
    def require_real_boolean(cls, value: object) -> object:
        # ``Literal[True]`` follows Python equality and would otherwise accept
        # the integer ``1``. Transport booleans must remain actual booleans.
        if type(value) is not bool:
            raise ValueError("readiness flags must be booleans")
        return value


class WorkflowControlPlaneReadyV1(_WorkflowProjectReadinessBaseV1):
    status: Literal["ready"]
    code: Literal["WORKFLOW_CONTROL_PLANE_READY"]
    workflow_enabled: Literal[True]
    schema_ready: Literal[True]
    admission_ready: StrictBool


class WorkflowDisabledV1(_WorkflowProjectReadinessBaseV1):
    status: Literal["ready"]
    code: Literal["WORKFLOW_DISABLED"]
    workflow_enabled: Literal[False]
    schema_ready: Literal[True]
    admission_ready: Literal[False]


class WorkflowSchemaUnavailableV1(_WorkflowProjectReadinessBaseV1):
    status: Literal["unavailable"]
    code: Literal["WORKFLOW_SCHEMA_UNAVAILABLE"]
    workflow_enabled: Literal[False]
    schema_ready: Literal[False]
    admission_ready: Literal[False]


class WorkflowPolicyUnavailableV1(_WorkflowProjectReadinessBaseV1):
    status: Literal["unavailable"]
    code: Literal["WORKFLOW_POLICY_UNAVAILABLE"]
    workflow_enabled: Literal[False]
    schema_ready: Literal[True]
    admission_ready: Literal[False]


type WorkflowProjectReadinessV1 = Annotated[
    WorkflowControlPlaneReadyV1 | WorkflowDisabledV1 | WorkflowSchemaUnavailableV1 | WorkflowPolicyUnavailableV1,
    Field(discriminator="code"),
]
WORKFLOW_PROJECT_READINESS_V1_ADAPTER = TypeAdapter(WorkflowProjectReadinessV1)


class WorkflowValidationIssueV1(_StrictWorkflowContract):
    severity: Literal["error", "warning"]
    code: _SafeCode
    message: _SafeMessage
    path: Annotated[tuple[StrictStr, ...], Field(max_length=64)]
    node_id: _CanonicalUuid | None = None
    edge_id: WorkflowEdgeId | None = None
    port_id: _SafeIdentifier | None = None


class WorkflowHttpHeaderV1(_StrictWorkflowContract):
    """A bounded, sanitized response header safe for owner-private replay."""

    name: _HttpHeaderName
    value: _HttpHeaderValue

    @field_validator("name")
    @classmethod
    def reject_sensitive_or_redirect_headers(cls, value: str) -> str:
        if value in {
            "authorization",
            "proxy-authenticate",
            "proxy-authorization",
            "set-cookie",
            "www-authenticate",
            "location",
        }:
            raise ValueError("sensitive and redirect response headers cannot be persisted")
        return value


class WorkflowHttpEmptyBodyV1(_StrictWorkflowContract):
    kind: Literal["empty"]


class WorkflowHttpTextBodyV1(_StrictWorkflowContract):
    kind: Literal["text"]
    text: _HttpTextBody


class WorkflowHttpJsonBodyV1(_StrictWorkflowContract):
    kind: Literal["json"]
    value: JsonValue

    @model_validator(mode="after")
    def validate_bounded_json(self) -> Self:
        _bounded_http_json_material(self.value)
        return self


type WorkflowHttpBodyV1 = Annotated[
    WorkflowHttpEmptyBodyV1 | WorkflowHttpTextBodyV1 | WorkflowHttpJsonBodyV1,
    Field(discriminator="kind"),
]


class WorkflowHttpObservedByteCountV1(_StrictWorkflowContract):
    """Bounded count; ``at_least`` represents a limit crossing without overflow."""

    value: _HttpSettledByteCount
    relation: Literal["exact", "at_least"]


class WorkflowHttpResponseV1(_StrictWorkflowContract):
    """Bounded response material persisted only with the private settled effect."""

    status_code: Annotated[StrictInt, Field(ge=100, le=599)]
    headers: Annotated[tuple[WorkflowHttpHeaderV1, ...], Field(max_length=64)]
    body: WorkflowHttpBodyV1
    duration_ms: _NonNegativeInt
    wire_byte_count: WorkflowHttpObservedByteCountV1
    decoded_byte_count: WorkflowHttpObservedByteCountV1
    retained_body_byte_count: _HttpSettledByteCount

    @model_validator(mode="after")
    def reject_duplicate_headers(self) -> Self:
        names = [header.name for header in self.headers]
        if len(names) != len(set(names)):
            raise ValueError("settled HTTP response headers must be unique")
        header_bytes = sum(len(header.name.encode("ascii")) + len(header.value.encode("utf-8")) for header in self.headers)
        if header_bytes > _MAX_WORKFLOW_HTTP_SETTLED_HEADER_BYTES:
            raise ValueError("settled HTTP response headers exceed the persisted byte limit")
        if isinstance(self.body, WorkflowHttpEmptyBodyV1):
            retained_body_bytes = 0
        elif isinstance(self.body, WorkflowHttpTextBodyV1):
            retained_body_bytes = len(self.body.text.encode("utf-8"))
        else:
            retained_body_bytes = _bounded_http_json_material(self.body.value)
        if self.wire_byte_count.relation != "exact" or self.decoded_byte_count.relation != "exact":
            raise ValueError("settled HTTP responses require exact wire and decoded byte counts")
        if self.retained_body_byte_count != retained_body_bytes:
            raise ValueError("retained body byte count must match canonical persisted response material")
        return self


class WorkflowHttpSuccessOutcomeV1(_StrictWorkflowContract):
    kind: Literal["success"]
    response: WorkflowHttpResponseV1


class WorkflowHttpErrorOutcomeV1(_StrictWorkflowContract):
    kind: Literal["http_error"]
    response: WorkflowHttpResponseV1


class WorkflowHttpResponseInvalidOutcomeV1(_StrictWorkflowContract):
    kind: Literal["response_invalid"]
    status_code: Annotated[StrictInt, Field(ge=100, le=599)]
    duration_ms: _NonNegativeInt
    wire_byte_count: WorkflowHttpObservedByteCountV1
    decoded_byte_count: WorkflowHttpObservedByteCountV1
    error: WorkflowEventSafeErrorV1

    @model_validator(mode="after")
    def require_response_validation_error(self) -> Self:
        if self.error.code not in {
            WorkflowErrorCode.WORKFLOW_HTTP_RESPONSE_LIMIT,
            WorkflowErrorCode.WORKFLOW_HTTP_RESPONSE_INVALID,
        }:
            raise ValueError("response_invalid requires a stable HTTP response validation error")
        if self.error.code == WorkflowErrorCode.WORKFLOW_HTTP_RESPONSE_LIMIT and not (self.wire_byte_count.relation == "at_least" or self.decoded_byte_count.relation == "at_least"):
            raise ValueError("response-limit outcomes require at least one capped byte observation")
        return self


type WorkflowHttpSettledOutcomeV1 = Annotated[
    WorkflowHttpSuccessOutcomeV1 | WorkflowHttpErrorOutcomeV1 | WorkflowHttpResponseInvalidOutcomeV1,
    Field(discriminator="kind"),
]
WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER = TypeAdapter(WorkflowHttpSettledOutcomeV1)


type WorkflowRunStatusV1 = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "side_effect_unknown",
]
WORKFLOW_RUN_STATUS_V1_ADAPTER = TypeAdapter(WorkflowRunStatusV1)


class WorkflowEventEnvelopeV1(_StrictWorkflowContract):
    schema_version: StrictLiteralOne
    run_id: _CanonicalUuid
    workflow_version_id: _CanonicalUuid
    seq: _CanonicalCursor
    type: WorkflowEventTypeV1
    node_id: _CanonicalUuid | None = None
    activation_id: WorkflowEventActivationId | None = None
    scope_path_hash: _Sha256Hex | None = None
    iteration_path: Annotated[
        tuple[WorkflowEventDatabasePositiveInt, ...],
        Field(max_length=16),
    ]
    attempt: WorkflowEventDatabasePositiveInt | None = None
    occurred_at: datetime
    payload: dict[StrictStr, JsonValue]

    @field_validator("occurred_at")
    @classmethod
    def require_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def validate_activation_shape(self) -> Self:
        node_event = self.type.startswith("workflow.node.")
        activation_fields = (self.node_id, self.activation_id, self.scope_path_hash, self.attempt)
        if node_event and any(value is None for value in activation_fields):
            raise ValueError("node events require node, activation, scope, and attempt identity")
        if not node_event and (any(value is not None for value in activation_fields) or self.iteration_path):
            raise ValueError("run events cannot carry node activation identity")
        canonical_workflow_event_payload_v1(self.type, self.payload)
        return self


WorkflowNodeLastRunErrorV1 = WorkflowEventSafeErrorV1
WorkflowNodeLastRunUsageV1 = WorkflowEventUsageV1


class WorkflowNodeLastRunV1(_StrictWorkflowContract):
    run_id: _CanonicalUuid
    node_id: _CanonicalUuid
    activation_id: WorkflowEventActivationId
    iteration_path: Annotated[
        tuple[WorkflowEventDatabasePositiveInt, ...],
        Field(max_length=16),
    ]
    attempt: WorkflowEventDatabasePositiveInt
    status: Literal[
        "queued",
        "provisioning",
        "running",
        "collecting",
        "cleanup_pending",
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
    ]
    started_at: datetime | None = None
    duration_ms: _NonNegativeInt | None = None
    input_preview: SafePreviewV1 | None = None
    output_preview: SafePreviewV1 | None = None
    error: WorkflowNodeLastRunErrorV1 | None = None
    usage: WorkflowNodeLastRunUsageV1 | None = None
    branch_port_id: _SafeIdentifier | None = None
    retry_count: _NonNegativeInt | None = None
    truncated: StrictBool | None = None

    @field_validator("started_at")
    @classmethod
    def require_offset(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("started_at must include a UTC offset")
        return value


class AgentRunExecutionReferenceV1(_StrictWorkflowContract):
    kind: Literal["agent_run"]
    run_id: _CanonicalUuid


class WorkflowRunExecutionReferenceV1(_StrictWorkflowContract):
    kind: Literal["workflow_run"]
    workflow_run_id: _CanonicalUuid
    workflow_epoch: _PositiveInt
    required_worker_profile_digest: _Sha256Hex | None = None


type WorkflowExecutionReferenceV1 = Annotated[
    AgentRunExecutionReferenceV1 | WorkflowRunExecutionReferenceV1,
    Field(discriminator="kind"),
]
WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER = TypeAdapter(WorkflowExecutionReferenceV1)


class WorkflowJobExecutionContextV1(_StrictWorkflowContract):
    """Server-private correlation context; never serialize as a public DTO."""

    job_id: _CanonicalUuid
    project_id: _CanonicalUuid
    owner_user_id: _CanonicalUuid
    origin_trace_id: _OriginTraceId
    execution_reference: WorkflowExecutionReferenceV1


__all__ = [
    "AgentRunExecutionReferenceV1",
    "SafePreviewV1",
    "WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER",
    "WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER",
    "WORKFLOW_PROJECT_READINESS_V1_ADAPTER",
    "WORKFLOW_RUN_STATUS_V1_ADAPTER",
    "WorkflowControlPlaneReadyV1",
    "WorkflowDisabledV1",
    "WorkflowEmptyEventPayloadV1",
    "WorkflowErrorCode",
    "WorkflowEventEnvelopeV1",
    "WorkflowEventSafeErrorV1",
    "WorkflowEventTypeV1",
    "WorkflowEventUsageV1",
    "WorkflowExecutionReferenceV1",
    "WorkflowHttpBodyV1",
    "WorkflowHttpEmptyBodyV1",
    "WorkflowHttpErrorOutcomeV1",
    "WorkflowHttpHeaderV1",
    "WorkflowHttpJsonBodyV1",
    "WorkflowHttpObservedByteCountV1",
    "WorkflowHttpResponseInvalidOutcomeV1",
    "WorkflowHttpResponseV1",
    "WorkflowHttpSettledOutcomeV1",
    "WorkflowHttpSuccessOutcomeV1",
    "WorkflowHttpTextBodyV1",
    "WorkflowJobExecutionContextV1",
    "WorkflowNodeCompletedEventPayloadV1",
    "WorkflowNodeDeltaEventPayloadV1",
    "WorkflowNodeFailedEventPayloadV1",
    "WorkflowNodeLastRunErrorV1",
    "WorkflowNodeLastRunUsageV1",
    "WorkflowNodeLastRunV1",
    "WorkflowNodeLifecycleEventPayloadV1",
    "WorkflowNodeLogEventPayloadV1",
    "WorkflowPolicyUnavailableV1",
    "WorkflowProjectReadinessV1",
    "WorkflowRunCancelledEventPayloadV1",
    "WorkflowRunCompletedEventPayloadV1",
    "WorkflowRunExecutionReferenceV1",
    "WorkflowRunFailedEventPayloadV1",
    "WorkflowRunSideEffectUnknownEventPayloadV1",
    "WorkflowRunStatusV1",
    "WorkflowSchemaUnavailableV1",
    "WorkflowValidationIssueV1",
]
