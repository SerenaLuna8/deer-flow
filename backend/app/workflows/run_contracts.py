"""Strict public and server-private Workflow Run/Job contracts.

This module contains no ORM, persistence, router, or Worker implementation.  It
freezes the first-class Workflow Run aggregate and its immutable Job epoch
mapping before those implementation layers are added.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.workflows.compatibility import WorkflowCompilerSnapshotContractV1, WorkflowSchemaCompatibilityCaseV1
from app.workflows.contracts import (
    WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER,
    AgentRunExecutionReferenceV1,
    SafePreviewV1,
    WorkflowEventSafeErrorV1,
    WorkflowExecutionReferenceV1,
    WorkflowRunExecutionReferenceV1,
    WorkflowRunStatusV1,
)
from deerflow.workflows import MAX_SAFE_JSON_INTEGER, StrictLiteralOne
from deerflow.workflows.admission import (
    WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES,
    WORKFLOW_RUN_INPUT_MAX_DEPTH,
    WORKFLOW_RUN_INPUT_MAX_NODES,
    validate_workflow_run_inputs_v1,
)

_PositiveInt = Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_JSON_INTEGER)]
_NonNegativeInt = Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_JSON_INTEGER)]
_MaxAttempts = Annotated[StrictInt, Field(ge=1, le=20)]
_Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_OriginTraceId = Annotated[StrictStr, Field(min_length=1, max_length=512, pattern=r"^[\x20-\x7e]+$")]
_SafePublicErrorCode = Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")]
_InputId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")]
_CANONICAL_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CANONICAL_RFC3339_UTC_TEXT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{0,5}[1-9])?Z$")


def _validate_canonical_uuid_input(value: object) -> object:
    if isinstance(value, str) and _CANONICAL_UUID_TEXT.fullmatch(value) is None:
        raise ValueError("UUID input must use canonical lowercase hyphenated text")
    return value


_CanonicalUuid = Annotated[uuid.UUID, BeforeValidator(_validate_canonical_uuid_input)]


def _parse_canonical_utc_datetime(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Workflow timestamps must be timezone-aware")
        return value.astimezone(UTC)
    if not isinstance(value, str) or _CANONICAL_RFC3339_UTC_TEXT.fullmatch(value) is None:
        raise ValueError("Workflow timestamps must be canonical RFC3339 UTC text ending in Z")
    try:
        return datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ValueError("Workflow timestamp is not a valid Gregorian UTC instant") from error


def _require_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("Workflow timestamps must use UTC")
    return value


def _serialize_canonical_utc_datetime(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    base = f"{utc_value.year:04d}-{utc_value.month:02d}-{utc_value.day:02d}T{utc_value.hour:02d}:{utc_value.minute:02d}:{utc_value.second:02d}"
    if utc_value.microsecond:
        base += f".{utc_value.microsecond:06d}".rstrip("0")
    return f"{base}Z"


_CanonicalUtcDatetime = Annotated[
    datetime,
    BeforeValidator(_parse_canonical_utc_datetime),
    AfterValidator(_require_utc_datetime),
    PlainSerializer(_serialize_canonical_utc_datetime, return_type=str, when_used="json"),
]
_RelativeStreamUrl = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=512,
        pattern=r"^/api/projects/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/workflow-runs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/stream$",
    ),
]

type WorkflowRunJobCauseV1 = Literal["initial", "resume"]
type WorkflowPrivateJobStatusV1 = Literal[
    "queued",
    "leased",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "dead",
]


class _StrictWorkflowRunContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class WorkflowRunAdmissionRequestV1(_StrictWorkflowRunContract):
    workflow_version_id: _CanonicalUuid | None
    inputs: Annotated[dict[_InputId, object], Field(max_length=256)]

    @field_validator("inputs")
    @classmethod
    def require_canonical_json_inputs(cls, value: dict[str, object]) -> dict[str, object]:
        return validate_workflow_run_inputs_v1(value)


class WorkflowRunAdmissionResponseV1(_StrictWorkflowRunContract):
    schema_version: StrictLiteralOne
    run_id: _CanonicalUuid
    status: Literal["queued"]
    stream_url: _RelativeStreamUrl

    @model_validator(mode="after")
    def bind_stream_url_to_run(self) -> Self:
        if f"/workflow-runs/{self.run_id}/stream" not in self.stream_url:
            raise ValueError("stream URL must identify the admitted Workflow Run")
        return self


class WorkflowOwnerPrivateRunV1(_StrictWorkflowRunContract):
    """Owner-scoped public projection; server authority fields are absent."""

    schema_version: StrictLiteralOne
    run_id: _CanonicalUuid
    workflow_id: _CanonicalUuid
    workflow_version_id: _CanonicalUuid
    status: WorkflowRunStatusV1
    execution_epoch: _PositiveInt
    retry_of_run_id: _CanonicalUuid | None
    created_at: _CanonicalUtcDatetime
    started_at: _CanonicalUtcDatetime | None
    completed_at: _CanonicalUtcDatetime | None
    input_preview: SafePreviewV1 | None
    output_preview: SafePreviewV1 | None
    error: WorkflowEventSafeErrorV1 | None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.retry_of_run_id == self.run_id:
            raise ValueError("a Workflow Run cannot retry itself")
        terminal_after_start = self.status in {"succeeded", "failed", "side_effect_unknown"}
        if self.status == "queued" and (self.started_at is not None or self.completed_at is not None):
            raise ValueError("a queued Workflow Run cannot have timestamps beyond created_at")
        if self.status == "running" and (self.started_at is None or self.completed_at is not None):
            raise ValueError("a running Workflow Run requires only started_at")
        if terminal_after_start and (self.started_at is None or self.completed_at is None):
            raise ValueError("terminal Workflow Runs require started_at and completed_at")
        if self.status == "cancelled" and self.completed_at is None:
            raise ValueError("cancelled Workflow Runs require completed_at")
        error_required = self.status in {"failed", "side_effect_unknown"}
        if error_required != (self.error is not None):
            raise ValueError("failed and side-effect-unknown Workflow Runs require a safe error projection")
        if self.started_at is not None and self.created_at > self.started_at:
            raise ValueError("Workflow Run created_at cannot follow started_at")
        if self.completed_at is not None:
            if self.started_at is None:
                if self.status != "cancelled" or self.created_at > self.completed_at:
                    raise ValueError("cancel-before-start completion cannot precede Workflow Run creation")
            elif self.started_at > self.completed_at:
                raise ValueError("Workflow Run started_at cannot follow completed_at")
        return self


class WorkflowPrivateRunAuthorityV1(_StrictWorkflowRunContract):
    """Server-private persisted authority; never serialize as a public DTO."""

    schema_version: StrictLiteralOne
    run_id: _CanonicalUuid
    project_id: _CanonicalUuid
    owner_user_id: _CanonicalUuid
    workflow_id: _CanonicalUuid
    workflow_version_id: _CanonicalUuid
    status: WorkflowRunStatusV1
    execution_epoch: _PositiveInt
    current_job_id: _CanonicalUuid | None
    retry_of_run_id: _CanonicalUuid | None
    origin_trace_id: _OriginTraceId
    required_worker_profile_digest: _Sha256Hex | None

    @model_validator(mode="after")
    def validate_current_job_shape(self) -> Self:
        if self.retry_of_run_id == self.run_id:
            raise ValueError("a Workflow Run cannot retry itself")
        active = self.status in {"queued", "running"}
        if active != (self.current_job_id is not None):
            raise ValueError("current_job_id must exist only for an active first-wave Workflow Run")
        return self


class WorkflowPrivateJobV1(_StrictWorkflowRunContract):
    """Server-private Workflow Job projection without raw lease material."""

    schema_version: StrictLiteralOne
    job_id: _CanonicalUuid
    job_type: Literal["workflow_run"]
    project_id: _CanonicalUuid
    owner_user_id: _CanonicalUuid
    status: WorkflowPrivateJobStatusV1
    cause: WorkflowRunJobCauseV1
    attempt_count: _NonNegativeInt
    max_attempts: _MaxAttempts
    origin_trace_id: _OriginTraceId
    execution_reference: WorkflowRunExecutionReferenceV1
    created_at: _CanonicalUtcDatetime
    started_at: _CanonicalUtcDatetime | None
    completed_at: _CanonicalUtcDatetime | None
    public_error_code: _SafePublicErrorCode | None

    @model_validator(mode="after")
    def validate_attempt_lifecycle(self) -> Self:
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count cannot exceed max_attempts")
        if self.status == "queued" and (self.attempt_count != 0 or self.started_at is not None or self.completed_at is not None or self.public_error_code is not None):
            raise ValueError("queued Workflow Jobs require attempt zero and no attempt outcome")
        cancelled_before_start = self.status == "cancelled" and self.attempt_count == 0 and self.started_at is None
        nonqueued_after_start = self.status != "queued" and not cancelled_before_start
        if nonqueued_after_start and (self.attempt_count < 1 or self.started_at is None):
            raise ValueError("non-queued Workflow Jobs require a started attempt")
        terminal = self.status in {"succeeded", "failed", "cancelled", "dead"}
        if terminal and self.completed_at is None:
            raise ValueError("terminal Workflow Jobs require completed_at")
        if not terminal and self.completed_at is not None:
            raise ValueError("non-terminal Workflow Jobs cannot have completed_at")
        error_required = self.status in {"retry_wait", "failed", "dead"}
        if error_required != (self.public_error_code is not None):
            raise ValueError("retrying, failed, and dead Workflow Jobs require a public error code")
        if self.started_at is not None and self.created_at > self.started_at:
            raise ValueError("Workflow Job created_at cannot follow started_at")
        if self.completed_at is not None:
            if self.started_at is None:
                if not cancelled_before_start or self.created_at > self.completed_at:
                    raise ValueError("cancel-before-start completion cannot precede Workflow Job creation")
            elif self.started_at > self.completed_at:
                raise ValueError("Workflow Job started_at cannot follow completed_at")
        epoch = self.execution_reference.workflow_epoch
        if (self.cause == "initial" and epoch != 1) or (self.cause == "resume" and epoch < 2):
            raise ValueError("Workflow Job cause must match its execution epoch")
        return self


class WorkflowRunJobEpochMappingV1(_StrictWorkflowRunContract):
    """Immutable mapping row; automatic Job attempts never create a new epoch."""

    schema_version: StrictLiteralOne
    workflow_run_id: _CanonicalUuid
    execution_epoch: _PositiveInt
    job_id: _CanonicalUuid
    cause: WorkflowRunJobCauseV1
    created_at: _CanonicalUtcDatetime

    @model_validator(mode="after")
    def validate_cause(self) -> Self:
        if self.cause == "initial" and self.execution_epoch != 1:
            raise ValueError("initial Workflow Job mapping must be epoch 1")
        if self.cause == "resume" and self.execution_epoch < 2:
            raise ValueError("resume Workflow Job mapping must use a later epoch")
        return self


class WorkflowRunJobAuthorityV1(_StrictWorkflowRunContract):
    """One exact Run/current-Job/epoch authority tuple."""

    schema_version: StrictLiteralOne
    run: WorkflowPrivateRunAuthorityV1
    job: WorkflowPrivateJobV1
    mapping: WorkflowRunJobEpochMappingV1

    @model_validator(mode="after")
    def require_exact_authority_mapping(self) -> Self:
        reference = self.job.execution_reference
        if not (
            self.run.run_id == reference.workflow_run_id == self.mapping.workflow_run_id
            and self.run.current_job_id == self.job.job_id == self.mapping.job_id
            and self.run.execution_epoch == reference.workflow_epoch == self.mapping.execution_epoch
            and self.job.cause == self.mapping.cause
            and self.run.project_id == self.job.project_id
            and self.run.owner_user_id == self.job.owner_user_id
            and self.run.origin_trace_id == self.job.origin_trace_id
            and self.run.required_worker_profile_digest == reference.required_worker_profile_digest
            and self.job.created_at == self.mapping.created_at
        ):
            raise ValueError("Workflow Run, current Job, execution epoch, trace, scope, and profile must match exactly")
        return self


class WorkflowRunContractFixtureV1(_StrictWorkflowRunContract):
    schema_version: StrictLiteralOne
    admission_request: WorkflowRunAdmissionRequestV1
    admission_response: WorkflowRunAdmissionResponseV1
    owner_private_run: WorkflowOwnerPrivateRunV1
    cancelled_before_start_run: WorkflowOwnerPrivateRunV1
    cancelled_before_start_job: WorkflowPrivateJobV1
    execution_references: Annotated[tuple[WorkflowExecutionReferenceV1, ...], Field(min_length=2, max_length=2)]
    authority_bundles: Annotated[tuple[WorkflowRunJobAuthorityV1, ...], Field(min_length=2, max_length=8)]
    compatibility_cases: Annotated[tuple[WorkflowSchemaCompatibilityCaseV1, ...], Field(min_length=3, max_length=16)]
    compiler_snapshot_contract: WorkflowCompilerSnapshotContractV1


WORKFLOW_RUN_CONTRACT_FIXTURE_V1_ADAPTER = TypeAdapter(WorkflowRunContractFixtureV1)


__all__ = [
    "AgentRunExecutionReferenceV1",
    "WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER",
    "WORKFLOW_RUN_CONTRACT_FIXTURE_V1_ADAPTER",
    "WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES",
    "WORKFLOW_RUN_INPUT_MAX_DEPTH",
    "WORKFLOW_RUN_INPUT_MAX_NODES",
    "WorkflowExecutionReferenceV1",
    "WorkflowOwnerPrivateRunV1",
    "WorkflowPrivateJobStatusV1",
    "WorkflowPrivateJobV1",
    "WorkflowPrivateRunAuthorityV1",
    "WorkflowRunAdmissionRequestV1",
    "WorkflowRunAdmissionResponseV1",
    "WorkflowRunContractFixtureV1",
    "WorkflowRunExecutionReferenceV1",
    "WorkflowRunJobAuthorityV1",
    "WorkflowRunJobCauseV1",
    "WorkflowRunJobEpochMappingV1",
]
