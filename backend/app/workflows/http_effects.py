"""Workflow HTTP Credential and durable effect-ledger contracts.

The browser and node configuration never carry a secret.  A published
Workflow version freezes an exact Credential grant and a Run copies that
grant closure, but intentionally does *not* freeze a Credential envelope.
The active envelope is resolved and the grant is revalidated immediately
before every dispatch so rotation is visible and revocation fails closed.

The persistence adapter is added after the Phase-0 schema review.  These
contracts freeze the state machine that adapter must implement; in particular
``settled`` always contains one recoverable typed outcome and ``unknown`` is a
terminal write-side-effect state with no public retry path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
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
    field_validator,
    model_validator,
)

from app.workflows.contracts import (
    WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER,
    WorkflowHttpErrorOutcomeV1,
    WorkflowHttpResponseInvalidOutcomeV1,
    WorkflowHttpSettledOutcomeV1,
    WorkflowHttpSuccessOutcomeV1,
)
from deerflow.workflows import MAX_SAFE_JSON_INTEGER, StrictLiteralOne

SIDE_EFFECT_UNKNOWN_PUBLIC_RETRY_ALLOWED: Literal[False] = False

_CANONICAL_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CANONICAL_RFC3339_UTC_TEXT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{0,5}[1-9])?Z$")


def _validate_canonical_uuid_input(value: object) -> object:
    if isinstance(value, str) and _CANONICAL_UUID_TEXT.fullmatch(value) is None:
        raise ValueError("UUID input must use canonical lowercase hyphenated text")
    return value


def _parse_canonical_utc_datetime(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Workflow HTTP effect timestamps must be timezone-aware")
        return value.astimezone(UTC)
    if not isinstance(value, str) or _CANONICAL_RFC3339_UTC_TEXT.fullmatch(value) is None:
        raise ValueError("Workflow HTTP effect timestamps must be canonical UTC text")
    try:
        return datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ValueError("Workflow HTTP effect timestamp is invalid") from error


def _require_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("Workflow HTTP effect timestamps must use UTC")
    return value


def _serialize_canonical_utc_datetime(value: datetime) -> str:
    value = value.astimezone(UTC)
    rendered = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        rendered += f".{value.microsecond:06d}".rstrip("0")
    return f"{rendered}Z"


_CanonicalUuid = Annotated[
    uuid.UUID,
    BeforeValidator(_validate_canonical_uuid_input),
]
_CanonicalUtcDatetime = Annotated[
    datetime,
    BeforeValidator(_parse_canonical_utc_datetime),
    AfterValidator(_require_utc_datetime),
    PlainSerializer(
        _serialize_canonical_utc_datetime,
        return_type=str,
        when_used="json",
    ),
]
_PositiveInt = Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_JSON_INTEGER)]
_StableId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
_Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_CredentialPayloadContract = Literal[
    "bearer_token_v1",
    "basic_auth_v1",
    "api_key_v1",
]
_HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
_SafeErrorCode = Annotated[
    StrictStr,
    Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$"),
]


class _StrictHttpEffectContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )


class WorkflowHttpCredentialSlotRequirementV1(_StrictHttpEffectContract):
    schema_version: StrictLiteralOne
    slot_id: _StableId
    injection_profile_id: _StableId
    credential_payload_contract: _CredentialPayloadContract
    required: Literal[True]

    @field_validator("required", mode="before")
    @classmethod
    def require_real_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("HTTP Credential slot required must be a real boolean")
        return value


class WorkflowHttpDraftGrantIntentV1(_StrictHttpEffectContract):
    """Editable Draft intent; it is not executable authority."""

    schema_version: StrictLiteralOne
    workflow_id: _CanonicalUuid
    draft_revision: _PositiveInt
    slot_id: _StableId
    credential_id: _CanonicalUuid


class WorkflowHttpVersionGrantV1(_StrictHttpEffectContract):
    """Exact immutable grant closure copied into a Published Version."""

    schema_version: StrictLiteralOne
    workflow_version_id: _CanonicalUuid
    slot_id: _StableId
    injection_profile_id: _StableId
    credential_payload_contract: _CredentialPayloadContract
    credential_grant_id: _CanonicalUuid
    credential_id: _CanonicalUuid
    credential_version_id: _CanonicalUuid
    grant_version: _PositiveInt


class WorkflowHttpRunCredentialSnapshotV1(WorkflowHttpVersionGrantV1):
    """Run-exact grant snapshot; an active envelope identity is absent by design."""

    run_id: _CanonicalUuid


class WorkflowHttpCredentialGrantLiveStateV1(_StrictHttpEffectContract):
    """Live materialization metadata loaded again immediately before dispatch."""

    schema_version: StrictLiteralOne
    credential_grant_id: _CanonicalUuid
    credential_version_id: _CanonicalUuid
    grant_version: _PositiveInt
    status: Literal["active", "revoked"]
    active_envelope_generation: _PositiveInt


def require_live_workflow_http_dispatch_grant(
    snapshot: WorkflowHttpRunCredentialSnapshotV1,
    live: WorkflowHttpCredentialGrantLiveStateV1,
) -> int:
    """Fail closed on revocation/staleness and return the envelope generation to load.

    The generation is deliberately returned only to the private dispatcher.  It is
    not part of the Run snapshot and may advance after a Credential rotation.
    """

    if type(snapshot) is not WorkflowHttpRunCredentialSnapshotV1:
        raise TypeError("snapshot must be WorkflowHttpRunCredentialSnapshotV1")
    if type(live) is not WorkflowHttpCredentialGrantLiveStateV1:
        raise TypeError("live must be WorkflowHttpCredentialGrantLiveStateV1")
    if not (live.status == "active" and snapshot.credential_grant_id == live.credential_grant_id and snapshot.credential_version_id == live.credential_version_id and snapshot.grant_version == live.grant_version):
        raise ValueError("Workflow HTTP dispatch grant is revoked or stale")
    return live.active_envelope_generation


def _require_hmac_key(value: bytes) -> bytes:
    if type(value) is not bytes or len(value) < 32:
        raise ValueError("Workflow HTTP operation HMAC key must contain at least 32 bytes")
    return value


def derive_workflow_http_request_fingerprint(
    *,
    hmac_key: bytes,
    canonical_request_material: bytes,
) -> str:
    """Keyed request fingerprint; persisted material is not offline-guessable."""

    key = _require_hmac_key(hmac_key)
    if type(canonical_request_material) is not bytes:
        raise TypeError("canonical_request_material must be bytes")
    return hmac.new(
        key,
        b"workflow-http-request-fingerprint-v1\0" + canonical_request_material,
        hashlib.sha256,
    ).hexdigest()


def derive_workflow_http_operation_key(
    *,
    hmac_key: bytes,
    run_id: uuid.UUID,
    node_id: uuid.UUID,
    activation_key: str,
    request_fingerprint: str,
) -> str:
    """Stable activation operation identity; transport attempt is excluded."""

    key = _require_hmac_key(hmac_key)
    if type(run_id) is not uuid.UUID or type(node_id) is not uuid.UUID:
        raise TypeError("run_id and node_id must be UUID")
    if not isinstance(activation_key, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", activation_key) is None:
        raise ValueError("activation_key is invalid")
    if not isinstance(request_fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", request_fingerprint) is None:
        raise ValueError("request_fingerprint is invalid")
    material = (f"workflow-http-operation-v1\0{run_id}\0{node_id}\0{activation_key}\0{request_fingerprint}").encode("ascii")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def derive_workflow_http_idempotency_key(
    *,
    hmac_key: bytes,
    operation_key: str,
) -> str:
    """Return the stable server-owned write idempotency key for one operation."""

    key = _require_hmac_key(hmac_key)
    if not isinstance(operation_key, str) or re.fullmatch(r"[0-9a-f]{64}", operation_key) is None:
        raise ValueError("operation_key is invalid")
    return hmac.new(
        key,
        f"workflow-http-idempotency-v1\0{operation_key}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


class WorkflowHttpEffectIdentityV1(_StrictHttpEffectContract):
    schema_version: StrictLiteralOne
    effect_id: _CanonicalUuid
    run_id: _CanonicalUuid
    workflow_version_id: _CanonicalUuid
    node_id: _CanonicalUuid
    activation_key: _StableId
    operation_key: _Sha256Hex
    method: _HttpMethod
    request_fingerprint: _Sha256Hex
    idempotency_key: _Sha256Hex | None

    @model_validator(mode="after")
    def require_method_retry_identity(self) -> Self:
        if self.method in {"GET", "HEAD"}:
            if self.idempotency_key is not None:
                raise ValueError("read methods cannot carry a write idempotency key")
            return self
        if self.idempotency_key is None:
            raise ValueError("write methods require a server-derived idempotency key")
        return self


@dataclass(frozen=True, slots=True)
class WorkflowHttpJobExecutionFence:
    """Raw server-private Job authority held only by the executing Worker.

    The raw lease is intentionally absent from every Pydantic/public contract,
    JSON serializer and persisted effect row.  PostgreSQL stores only the same
    SHA-256 hash used by the canonical Job repository.
    """

    run_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    origin_trace_id: str
    job_id: uuid.UUID
    execution_epoch: int
    attempt: int
    worker_id: uuid.UUID
    lease_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.run_id) is not uuid.UUID or type(self.project_id) is not uuid.UUID or type(self.job_id) is not uuid.UUID:
            raise TypeError("Workflow HTTP Job fence IDs must be UUIDs")
        if type(self.owner_user_id) is not str or not 1 <= len(self.owner_user_id) <= 36:
            raise ValueError("Workflow HTTP Job fence owner is invalid")
        if type(self.origin_trace_id) is not str or not 1 <= len(self.origin_trace_id) <= 512 or any(character in "\r\n\x00" for character in self.origin_trace_id):
            raise ValueError("Workflow HTTP Job fence trace is invalid")
        if type(self.worker_id) is not uuid.UUID:
            raise TypeError("Workflow HTTP Job fence Worker ID must be a UUID")
        if type(self.execution_epoch) is not int or not 1 <= self.execution_epoch <= MAX_SAFE_JSON_INTEGER:
            raise ValueError("Workflow HTTP Job execution epoch is invalid")
        if type(self.attempt) is not int or not 1 <= self.attempt <= MAX_SAFE_JSON_INTEGER:
            raise ValueError("Workflow HTTP Job attempt is invalid")
        if type(self.lease_token) is not str or not 32 <= len(self.lease_token) <= 512 or any(character.isspace() for character in self.lease_token):
            raise ValueError("Workflow HTTP raw Job lease token is invalid")

    @property
    def lease_token_hash(self) -> str:
        return hashlib.sha256(self.lease_token.encode("utf-8")).hexdigest()


def require_workflow_http_settled_outcome(
    outcome: object,
) -> WorkflowHttpSettledOutcomeV1:
    """Accept only an already-typed strict outcome at the effect boundary."""

    if type(outcome) not in {
        WorkflowHttpSuccessOutcomeV1,
        WorkflowHttpErrorOutcomeV1,
        WorkflowHttpResponseInvalidOutcomeV1,
    }:
        raise TypeError("Workflow HTTP settled outcome must be a typed instance")
    return WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(
        outcome,
        strict=True,
    )


def workflow_http_settled_outcome_digest(
    outcome: WorkflowHttpSettledOutcomeV1,
) -> str:
    outcome = require_workflow_http_settled_outcome(outcome)
    payload = WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.dump_python(
        outcome,
        mode="json",
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class WorkflowHttpEffectRecordV1(_StrictHttpEffectContract):
    """One server-private effect row; never expose this DTO to the browser."""

    schema_version: StrictLiteralOne
    identity: WorkflowHttpEffectIdentityV1
    state: Literal[
        "prepared",
        "dispatching",
        "settled",
        "failed_safe",
        "unknown",
    ]
    revision: _PositiveInt
    dispatch_job_id: _CanonicalUuid | None
    dispatch_execution_epoch: _PositiveInt | None
    dispatch_attempt: _PositiveInt | None
    dispatch_owner: _StableId | None
    dispatch_lease_token_hash: _Sha256Hex | None
    dispatch_started_at: _CanonicalUtcDatetime | None
    outcome: WorkflowHttpSettledOutcomeV1 | None
    outcome_digest: _Sha256Hex | None
    safe_error_code: _SafeErrorCode | None
    updated_at: _CanonicalUtcDatetime

    @model_validator(mode="after")
    def validate_state_shape(self) -> Self:
        if self.state == "prepared":
            valid = (
                self.dispatch_owner is None
                and self.dispatch_job_id is None
                and self.dispatch_execution_epoch is None
                and self.dispatch_attempt is None
                and self.dispatch_lease_token_hash is None
                and self.dispatch_started_at is None
                and self.outcome is None
                and self.outcome_digest is None
                and self.safe_error_code is None
            )
        elif self.state == "dispatching":
            valid = (
                self.dispatch_owner is not None
                and self.dispatch_job_id is not None
                and self.dispatch_execution_epoch is not None
                and self.dispatch_attempt is not None
                and self.dispatch_lease_token_hash is not None
                and self.dispatch_started_at is not None
                and self.outcome is None
                and self.outcome_digest is None
                and self.safe_error_code is None
            )
        elif self.state == "settled":
            valid = (
                self.dispatch_owner is None
                and self.dispatch_job_id is not None
                and self.dispatch_execution_epoch is not None
                and self.dispatch_attempt is not None
                and self.dispatch_lease_token_hash is None
                and self.dispatch_started_at is not None
                and self.outcome is not None
                and self.outcome_digest is not None
                and self.safe_error_code is None
            )
            if valid and self.outcome_digest != workflow_http_settled_outcome_digest(self.outcome):
                raise ValueError("settled HTTP outcome digest does not match its payload")
        elif self.state == "failed_safe":
            valid = (
                self.dispatch_owner is None
                and self.dispatch_job_id is not None
                and self.dispatch_execution_epoch is not None
                and self.dispatch_attempt is not None
                and self.dispatch_lease_token_hash is None
                and self.dispatch_started_at is not None
                and self.outcome is None
                and self.outcome_digest is None
                and self.safe_error_code is not None
                and self.safe_error_code != "SIDE_EFFECT_STATE_UNKNOWN"
            )
        else:
            valid = (
                self.dispatch_owner is None
                and self.dispatch_job_id is not None
                and self.dispatch_execution_epoch is not None
                and self.dispatch_attempt is not None
                and self.dispatch_lease_token_hash is None
                and self.dispatch_started_at is not None
                and self.outcome is None
                and self.outcome_digest is None
                and self.safe_error_code == "SIDE_EFFECT_STATE_UNKNOWN"
            )
        if not valid:
            raise ValueError("Workflow HTTP effect fields do not match the effect state")
        return self


__all__ = [
    "SIDE_EFFECT_UNKNOWN_PUBLIC_RETRY_ALLOWED",
    "WorkflowHttpCredentialGrantLiveStateV1",
    "WorkflowHttpCredentialSlotRequirementV1",
    "WorkflowHttpDraftGrantIntentV1",
    "WorkflowHttpEffectIdentityV1",
    "WorkflowHttpEffectRecordV1",
    "WorkflowHttpJobExecutionFence",
    "WorkflowHttpRunCredentialSnapshotV1",
    "WorkflowHttpVersionGrantV1",
    "derive_workflow_http_idempotency_key",
    "derive_workflow_http_operation_key",
    "derive_workflow_http_request_fingerprint",
    "require_live_workflow_http_dispatch_grant",
    "require_workflow_http_settled_outcome",
    "workflow_http_settled_outcome_digest",
]
