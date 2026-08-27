"""Immutable, content-free Context Evidence contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contracts import ContextSubject, FinalRequestMeasurement, _StrictContract

_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DECIMAL_SEQUENCE_PATTERN = r"^(0|[1-9][0-9]*)$"
MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807


class ContextWindowGeneration(_StrictContract):
    generation_id: str = Field(pattern=_UUID_PATTERN)

    def __init__(self, *, generation_id: str | UUID) -> None:
        super().__init__(generation_id=str(generation_id))


def _provider_call_digest(
    *,
    subject: ContextSubject,
    generation: ContextWindowGeneration,
    source_checkpoint_id: str,
    graph_step: str,
    model_call_ordinal: int,
    request_fingerprint: str,
) -> str:
    material = {
        "generation_id": generation.generation_id,
        "graph_step": graph_step,
        "model_call_ordinal": model_call_ordinal,
        "request_fingerprint": request_fingerprint,
        "source_checkpoint_id": source_checkpoint_id,
        "subject": subject.to_safe_mapping(),
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProviderCallIdentity(_StrictContract):
    """Durable identity for one fully shaped Provider request."""

    provider_call_id: str = Field(pattern=_SHA256_PATTERN)
    idempotency_key: str = Field(pattern=_SHA256_PATTERN)
    subject: ContextSubject
    generation: ContextWindowGeneration
    source_checkpoint_id: str = Field(min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    graph_step: str = Field(min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    model_call_ordinal: int = Field(ge=0, le=2_147_483_647)
    request_fingerprint: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def derive(
        cls,
        *,
        subject: ContextSubject,
        generation: ContextWindowGeneration,
        source_checkpoint_id: str,
        graph_step: str,
        model_call_ordinal: int,
        request_fingerprint: str,
    ) -> Self:
        digest = _provider_call_digest(
            subject=subject,
            generation=generation,
            source_checkpoint_id=source_checkpoint_id,
            graph_step=graph_step,
            model_call_ordinal=model_call_ordinal,
            request_fingerprint=request_fingerprint,
        )
        return cls(
            provider_call_id=digest,
            idempotency_key=digest,
            subject=subject,
            generation=generation,
            source_checkpoint_id=source_checkpoint_id,
            graph_step=graph_step,
            model_call_ordinal=model_call_ordinal,
            request_fingerprint=request_fingerprint,
        )

    @model_validator(mode="after")
    def _validate_derived_identity(self) -> Self:
        expected = _provider_call_digest(
            subject=self.subject,
            generation=self.generation,
            source_checkpoint_id=self.source_checkpoint_id,
            graph_step=self.graph_step,
            model_call_ordinal=self.model_call_ordinal,
            request_fingerprint=self.request_fingerprint,
        )
        if self.provider_call_id != expected or self.idempotency_key != expected:
            raise ValueError("Provider call identity does not match its canonical material")
        return self

    def to_safe_mapping(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class ContextEvidenceType(StrEnum):
    WINDOW_OPENED = "context.window.opened.v1"
    REQUEST_PREPARED = "request.prepared.v1"
    REQUEST_DISPATCHED = "request.dispatched.v1"
    PROVIDER_OBSERVED = "provider.observed.v1"
    PROVIDER_USAGE_UNREPORTED = "provider.usage_unreported.v1"
    PROVIDER_FAILED = "provider.failed.v1"
    PROVIDER_AMBIGUOUS = "provider.ambiguous.v1"
    CHECKPOINT_LINKED = "checkpoint.linked.v1"
    COMPACTION_COMMITTED = "compaction.committed.v1"
    WINDOW_REBASED = "context.window.rebased.v1"


STABLE_CONTEXT_EVIDENCE_TYPES: tuple[ContextEvidenceType, ...] = tuple(ContextEvidenceType)


class WindowOpenedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.WINDOW_OPENED] = ContextEvidenceType.WINDOW_OPENED
    model_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    context_window_tokens: int | None = Field(default=None, ge=1, le=2_000_000)
    compaction_enabled: bool
    compaction_threshold_tokens: int | None = Field(default=None, ge=1)
    compaction_authority: Literal["frozen_run", "idle_history"] | None = None

    @model_validator(mode="after")
    def _validate_compaction_authority(self) -> Self:
        if self.compaction_enabled and self.compaction_authority is None:
            raise ValueError("enabled compaction requires a frozen authority")
        if not self.compaction_enabled and (self.compaction_threshold_tokens is not None or self.compaction_authority is not None):
            raise ValueError("disabled compaction cannot retain active policy facts")
        return self


class RequestPreparedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.REQUEST_PREPARED] = ContextEvidenceType.REQUEST_PREPARED
    provider_call: ProviderCallIdentity
    measurement: FinalRequestMeasurement

    @model_validator(mode="after")
    def _validate_request_fingerprint(self) -> Self:
        if self.provider_call.request_fingerprint != self.measurement.request_fingerprint:
            raise ValueError("prepared request identity and measurement fingerprints disagree")
        return self


class RequestDispatchedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.REQUEST_DISPATCHED] = ContextEvidenceType.REQUEST_DISPATCHED
    provider_call_id: str = Field(pattern=_SHA256_PATTERN)


class ProviderObservedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.PROVIDER_OBSERVED] = ContextEvidenceType.PROVIDER_OBSERVED
    provider_call_id: str = Field(pattern=_SHA256_PATTERN)
    input_tokens: int = Field(ge=0)


class ProviderUsageUnreportedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.PROVIDER_USAGE_UNREPORTED] = ContextEvidenceType.PROVIDER_USAGE_UNREPORTED
    provider_call_id: str = Field(pattern=_SHA256_PATTERN)


class ProviderRetrySafety(StrEnum):
    NO_RESPONSE_PROVEN = "no_response_proven"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class ProviderFailedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.PROVIDER_FAILED] = ContextEvidenceType.PROVIDER_FAILED
    provider_call_id: str = Field(pattern=_SHA256_PATTERN)
    failure_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    retry_safety: ProviderRetrySafety


class ProviderAmbiguityReason(StrEnum):
    DISPATCH_OUTCOME_UNKNOWN = "dispatch_outcome_unknown"
    OBSERVATION_PERSISTENCE_UNKNOWN = "observation_persistence_unknown"


class ProviderAmbiguousV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.PROVIDER_AMBIGUOUS] = ContextEvidenceType.PROVIDER_AMBIGUOUS
    provider_call_id: str = Field(pattern=_SHA256_PATTERN)
    reason: ProviderAmbiguityReason


class CheckpointLinkedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.CHECKPOINT_LINKED] = ContextEvidenceType.CHECKPOINT_LINKED
    provider_call_id: str = Field(pattern=_SHA256_PATTERN)
    checkpoint_id: str = Field(min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)


class CompactionCommittedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.COMPACTION_COMMITTED] = ContextEvidenceType.COMPACTION_COMMITTED
    receipt_id: str = Field(min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    source_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    result_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    source_state_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    result_state_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_generation: ContextWindowGeneration
    result_generation: ContextWindowGeneration
    source_tokens: int = Field(ge=0)
    result_tokens: int = Field(ge=0)
    summary_tokens: int | None = Field(default=None, ge=0)
    summary_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_compaction(self) -> Self:
        checkpoint_identity = self.source_checkpoint_id is not None or self.result_checkpoint_id is not None
        state_identity = self.source_state_digest is not None or self.result_state_digest is not None
        if checkpoint_identity == state_identity:
            raise ValueError("compaction requires exactly one identity domain")
        if checkpoint_identity and (self.source_checkpoint_id is None or self.result_checkpoint_id is None):
            raise ValueError("checkpoint compaction requires both checkpoint identities")
        if state_identity and (self.source_state_digest is None or self.result_state_digest is None):
            raise ValueError("ephemeral compaction requires both state digests")
        if state_identity and self.summary_tokens is None:
            raise ValueError("ephemeral compaction requires summary Tokens")
        if self.summary_tokens is not None and self.summary_tokens > self.result_tokens:
            raise ValueError("compaction summary Tokens exceed retained context")
        if self.source_generation == self.result_generation:
            raise ValueError("compaction must begin a new Context Window Generation")
        if self.result_tokens > self.source_tokens:
            raise ValueError("committed compaction cannot increase retained context")
        return self


class ContextRebaseReason(StrEnum):
    BRANCH = "branch"
    ROLLBACK = "rollback"
    REGENERATION = "regeneration"
    MESSAGE_EDIT = "message_edit"
    HISTORY_REPLACEMENT = "history_replacement"


class WindowRebasedV1(_StrictContract):
    event_type: Literal[ContextEvidenceType.WINDOW_REBASED] = ContextEvidenceType.WINDOW_REBASED
    reason: ContextRebaseReason
    source_thread_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    source_checkpoint_id: str = Field(min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    result_checkpoint_id: str = Field(min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    result_generation: ContextWindowGeneration
    history_digest: str = Field(pattern=_SHA256_PATTERN)


ContextEvidencePayload = Annotated[
    WindowOpenedV1 | RequestPreparedV1 | RequestDispatchedV1 | ProviderObservedV1 | ProviderUsageUnreportedV1 | ProviderFailedV1 | ProviderAmbiguousV1 | CheckpointLinkedV1 | CompactionCommittedV1 | WindowRebasedV1,
    Field(discriminator="event_type"),
]


class ContextEvidence(_StrictContract):
    contract_version: Literal[1] = 1
    evidence_seq: str = Field(pattern=_DECIMAL_SEQUENCE_PATTERN)
    subject: ContextSubject
    generation: ContextWindowGeneration
    origin_run_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    occurred_at: datetime
    payload: ContextEvidencePayload

    @field_validator("evidence_seq")
    @classmethod
    def _validate_evidence_seq(cls, value: str) -> str:
        if int(value) > MAX_SIGNED_BIGINT:
            raise ValueError("Evidence sequence exceeds signed BIGINT")
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _parse_occurred_at(cls, value: Any) -> Any:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("occurred_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Evidence timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_payload_scope(self) -> Self:
        if isinstance(self.payload, RequestPreparedV1):
            if self.payload.provider_call.subject != self.subject:
                raise ValueError("prepared Provider call subject differs from Evidence subject")
            if self.payload.provider_call.generation != self.generation:
                raise ValueError("prepared Provider call generation differs from Evidence generation")
        if isinstance(self.payload, (CompactionCommittedV1, WindowRebasedV1)):
            if self.payload.result_generation != self.generation:
                raise ValueError("replacement Evidence must use the result generation")
        return self

    @property
    def event_type(self) -> ContextEvidenceType:
        return self.payload.event_type

    @property
    def idempotency_key(self) -> str:
        """Return the event-command key; sequence and timestamp are excluded."""

        material = {
            "contract_version": self.contract_version,
            "subject": self.subject.to_safe_mapping(),
            "generation": self.generation.model_dump(mode="json"),
            "origin_run_id": self.origin_run_id,
            "payload": self.payload.model_dump(mode="json", exclude_none=True),
        }
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_safe_mapping(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_safe_mapping(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise TypeError("Context Evidence must be a mapping")
        return cls.model_validate_json(json.dumps(dict(value), separators=(",", ":")))


__all__ = [
    "CheckpointLinkedV1",
    "CompactionCommittedV1",
    "ContextEvidence",
    "ContextEvidencePayload",
    "ContextEvidenceType",
    "ContextRebaseReason",
    "ContextWindowGeneration",
    "MAX_SIGNED_BIGINT",
    "ProviderAmbiguityReason",
    "ProviderAmbiguousV1",
    "ProviderCallIdentity",
    "ProviderFailedV1",
    "ProviderObservedV1",
    "ProviderRetrySafety",
    "ProviderUsageUnreportedV1",
    "RequestDispatchedV1",
    "RequestPreparedV1",
    "STABLE_CONTEXT_EVIDENCE_TYPES",
    "WindowOpenedV1",
    "WindowRebasedV1",
]
