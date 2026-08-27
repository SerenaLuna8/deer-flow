"""Session-bound persistence for Thread-owned Context Evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.context_evidence.model import (
    ContextEvidenceRow,
    ContextEvidenceSequenceRow,
    ContextProjectionHeadRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope

ContextEvidenceType = Literal[
    "context.window.opened.v1",
    "request.prepared.v1",
    "request.dispatched.v1",
    "provider.observed.v1",
    "provider.usage_unreported.v1",
    "provider.failed.v1",
    "provider.ambiguous.v1",
    "checkpoint.linked.v1",
    "compaction.committed.v1",
    "context.window.rebased.v1",
]
ContextSubjectKind = Literal["lead_thread", "subagent_task"]
ContextProjectionPhase = Literal["idle", "active", "settled"]
ContextProjectionBasis = Literal[
    "provider_confirmed",
    "hybrid",
    "estimated",
    "empty",
]
ContextProjectionCoverage = Literal["complete", "partial"]
ContextProjectionFreshness = Literal["current", "stale"]

CONTEXT_EVIDENCE_TYPES = frozenset(
    {
        "context.window.opened.v1",
        "request.prepared.v1",
        "request.dispatched.v1",
        "provider.observed.v1",
        "provider.usage_unreported.v1",
        "provider.failed.v1",
        "provider.ambiguous.v1",
        "checkpoint.linked.v1",
        "compaction.committed.v1",
        "context.window.rebased.v1",
    }
)
_PHASES = frozenset({"idle", "active", "settled"})
_BASES = frozenset({"provider_confirmed", "hybrid", "estimated", "empty"})
_COVERAGES = frozenset({"complete", "partial"})
_FRESHNESS = frozenset({"current", "stale"})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PROJECTOR_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*-v(?P<ordinal>[1-9][0-9]*)$")
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "base64",
        "body",
        "ciphertext",
        "content",
        "credential",
        "credentials",
        "file_content",
        "file_path",
        "file_ref",
        "headers",
        "message",
        "messages",
        "nonce",
        "prompt",
        "prompt_text",
        "schema_text",
        "secret",
        "secrets",
        "summary",
        "summary_text",
        "tool_schema",
    }
)
_MAX_PAYLOAD_BYTES = 256 * 1024
_MAX_JSON_DEPTH = 10
_MAX_JSON_ITEMS = 4096
_MAX_STRING_CHARS = 4096


class ContextEvidencePersistenceError(RuntimeError):
    """Base persistence error without private identifiers or payloads."""


class ContextEvidenceScopeNotFound(ContextEvidencePersistenceError):
    """The complete owner-private Thread scope does not exist."""


class ContextEvidenceIdempotencyConflict(ContextEvidencePersistenceError):
    """An idempotency key already names different safe Evidence."""


class ContextEvidenceSequenceConflict(ContextEvidencePersistenceError):
    """A caller used an unreserved or already occupied sequence."""


class ContextProjectionConflict(ContextEvidencePersistenceError):
    """A Projection write would move its materialized state backwards."""


class ContextPayloadUnsafe(ContextEvidencePersistenceError, ValueError):
    """A payload is not within the minimized Context persistence contract."""


@dataclass(frozen=True, slots=True)
class ContextEvidenceScope:
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str

    def __post_init__(self) -> None:
        try:
            project_id = uuid.UUID(str(self.project_id))
            owner_user_id = str(uuid.UUID(str(self.owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("Context Evidence private scope is invalid") from None
        thread_id = str(self.thread_id)
        if not thread_id or len(thread_id) > 64:
            raise ValueError("Context Evidence Thread identity is invalid")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)
        object.__setattr__(self, "thread_id", thread_id)

    @classmethod
    def from_resource(
        cls,
        resource_scope: PrivateResourceScope,
        thread_id: str,
    ) -> ContextEvidenceScope:
        if type(resource_scope) is not PrivateResourceScope:
            raise ValueError("private resource scope is required")
        return cls(
            project_id=uuid.UUID(resource_scope.project_id),
            owner_user_id=resource_scope.owner_user_id,
            thread_id=thread_id,
        )


@dataclass(frozen=True, slots=True)
class ContextSubjectRef:
    kind: ContextSubjectKind
    subject_id: str

    @classmethod
    def lead_thread(cls, thread_id: str) -> ContextSubjectRef:
        if not thread_id or len(thread_id) > 64:
            raise ValueError("Lead Context Subject identity is invalid")
        return cls(kind="lead_thread", subject_id=thread_id)

    @classmethod
    def subagent_task(cls, execution_id: uuid.UUID | str) -> ContextSubjectRef:
        try:
            normalized = str(uuid.UUID(str(execution_id)))
        except (TypeError, ValueError):
            raise ValueError("Sub-Agent Context Subject identity is invalid") from None
        return cls(kind="subagent_task", subject_id=normalized)


@dataclass(frozen=True, slots=True)
class ContextSequenceReservation:
    first_evidence_seq: int | None
    evidence_count: int
    first_projection_seq: int | None
    projection_count: int
    evidence_high_watermark: int
    projection_high_watermark: int


@dataclass(frozen=True, slots=True)
class ContextEvidenceAppend:
    subject: ContextSubjectRef
    context_window_generation: uuid.UUID
    event_type: ContextEvidenceType
    origin_run_id: str | None
    provider_call_id: str | None
    checkpoint_id: str | None
    idempotency_key: str
    payload: Mapping[str, object]
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_safe_contract(
        cls,
        evidence: object,
        *,
        idempotency_key: str | None = None,
        checkpoint_id: str | None = None,
    ) -> tuple[ContextEvidenceAppend, int]:
        """Adapt the core contract through its explicit minimized mapping."""

        from deerflow.runtime.context_evidence import (
            ContextEvidence as RuntimeContextEvidence,
        )

        if type(evidence) is not RuntimeContextEvidence:
            raise TypeError("core Context Evidence contract is required")
        safe = evidence.to_safe_mapping()
        subject = evidence.subject
        subject_ref = ContextSubjectRef.lead_thread(subject.thread_id) if subject.execution_id is None else ContextSubjectRef.subagent_task(subject.execution_id)
        payload = safe.get("payload")
        if not isinstance(payload, Mapping):  # pragma: no cover - core invariant
            raise ContextPayloadUnsafe("core Context Evidence payload is invalid")
        event_type = str(payload.get("event_type", ""))
        provider_call = payload.get("provider_call")
        provider_call_id = payload.get("provider_call_id")
        if provider_call_id is None and isinstance(provider_call, Mapping):
            provider_call_id = provider_call.get("provider_call_id")
        if checkpoint_id is None:
            for key in (
                "checkpoint_id",
                "result_checkpoint_id",
                "source_checkpoint_id",
            ):
                candidate = payload.get(key)
                if isinstance(candidate, str):
                    checkpoint_id = candidate
                    break
        if checkpoint_id is None and isinstance(provider_call, Mapping):
            candidate = provider_call.get("source_checkpoint_id")
            if isinstance(candidate, str):
                checkpoint_id = candidate
        return (
            cls(
                subject=subject_ref,
                context_window_generation=uuid.UUID(evidence.generation.generation_id),
                event_type=cast(ContextEvidenceType, event_type),
                origin_run_id=evidence.origin_run_id,
                provider_call_id=(provider_call_id if isinstance(provider_call_id, str) else None),
                checkpoint_id=checkpoint_id,
                idempotency_key=(evidence.idempotency_key if idempotency_key is None else idempotency_key),
                payload=cast(Mapping[str, object], payload),
                occurred_at=evidence.occurred_at,
            ),
            int(evidence.evidence_seq),
        )


@dataclass(frozen=True, slots=True)
class ContextEvidenceRecord:
    scope: ContextEvidenceScope
    evidence_seq: int
    subject: ContextSubjectRef
    context_window_generation: uuid.UUID
    event_type: ContextEvidenceType
    origin_run_id: str | None
    provider_call_id: str | None
    checkpoint_id: str | None
    idempotency_key: str
    payload_digest: str
    payload: dict[str, object]
    occurred_at: datetime

    def to_safe_contract(self) -> object:
        """Rebuild and strictly validate the core Evidence contract."""

        from deerflow.runtime.context_evidence import ContextEvidence

        subject: dict[str, object] = {
            "kind": self.subject.kind,
            "thread_id": self.scope.thread_id,
        }
        if self.subject.kind == "subagent_task":
            subject["execution_id"] = self.subject.subject_id
        return ContextEvidence.from_safe_mapping(
            {
                "contract_version": 1,
                "evidence_seq": str(self.evidence_seq),
                "subject": subject,
                "generation": {
                    "generation_id": str(self.context_window_generation),
                },
                "origin_run_id": self.origin_run_id,
                "occurred_at": self.occurred_at.isoformat(),
                "payload": self.payload,
            }
        )


@dataclass(frozen=True, slots=True)
class ContextProjectionHeadWrite:
    subject: ContextSubjectRef
    projection_seq: int | None
    evidence_seq: int
    projector_revision: str
    context_window_generation: uuid.UUID
    checkpoint_id: str | None
    active_run_id: str | None
    phase: ContextProjectionPhase
    basis: ContextProjectionBasis
    coverage: ContextProjectionCoverage
    freshness: ContextProjectionFreshness
    projection: Mapping[str, object]

    @classmethod
    def from_safe_contract(
        cls,
        head: object,
        *,
        active_run_id: str | None = None,
    ) -> ContextProjectionHeadWrite:
        """Adapt the core Projection through its explicit safe mapping."""

        from deerflow.runtime.context_evidence import (
            ContextProjectionHead as RuntimeContextProjectionHead,
        )

        if type(head) is not RuntimeContextProjectionHead:
            raise TypeError("core Context Projection Head contract is required")
        subject = head.subject
        subject_ref = ContextSubjectRef.lead_thread(subject.thread_id) if subject.execution_id is None else ContextSubjectRef.subagent_task(subject.execution_id)
        return cls(
            subject=subject_ref,
            projection_seq=int(head.projection_seq),
            evidence_seq=int(head.evidence_seq),
            projector_revision=head.projector_revision,
            context_window_generation=uuid.UUID(head.context_window_generation),
            checkpoint_id=head.checkpoint_id,
            active_run_id=active_run_id,
            phase=cast(ContextProjectionPhase, head.phase.value),
            basis=cast(ContextProjectionBasis, head.basis.value),
            coverage=cast(ContextProjectionCoverage, head.coverage.value),
            freshness=cast(ContextProjectionFreshness, head.freshness.value),
            projection=head.to_safe_mapping(),
        )


@dataclass(frozen=True, slots=True)
class ContextProjectionHeadRecord:
    scope: ContextEvidenceScope
    subject: ContextSubjectRef
    projection_seq: int
    evidence_seq: int
    projector_revision: str
    context_window_generation: uuid.UUID
    checkpoint_id: str | None
    active_run_id: str | None
    phase: ContextProjectionPhase
    basis: ContextProjectionBasis
    coverage: ContextProjectionCoverage
    freshness: ContextProjectionFreshness
    payload_digest: str
    projection: dict[str, object]
    updated_at: datetime

    def to_safe_contract(self) -> object:
        """Rebuild and strictly validate the core Projection contract."""

        from deerflow.runtime.context_evidence import ContextProjectionHead

        return ContextProjectionHead.from_safe_mapping(self.projection)


@dataclass(frozen=True, slots=True)
class ContextRetentionPurgeCounts:
    evidence: int
    projection_heads: int
    sequence: int


def _validate_subject(
    scope: ContextEvidenceScope,
    subject: ContextSubjectRef,
) -> None:
    if type(subject) is not ContextSubjectRef:
        raise ValueError("Context Subject is invalid")
    if subject.kind == "lead_thread":
        if subject.subject_id != scope.thread_id:
            raise ValueError("Lead Context Subject does not belong to the Thread")
        return
    if subject.kind == "subagent_task":
        try:
            if str(uuid.UUID(subject.subject_id)) == subject.subject_id:
                return
        except (TypeError, ValueError):
            pass
    raise ValueError("Context Subject is invalid")


def _validate_optional_identity(value: str | None, *, label: str, limit: int) -> None:
    if value is not None and (not value or len(value) > limit):
        raise ValueError(f"{label} is invalid")


def _safe_json(
    value: Mapping[str, object],
    *,
    contract_version: int | None,
) -> tuple[dict[str, object], str]:
    if not isinstance(value, Mapping):
        raise ContextPayloadUnsafe("Context payload must be an object")
    item_count = 0

    def visit(item: object, *, depth: int) -> Any:
        nonlocal item_count
        if depth > _MAX_JSON_DEPTH:
            raise ContextPayloadUnsafe("Context payload nesting is too deep")
        item_count += 1
        if item_count > _MAX_JSON_ITEMS:
            raise ContextPayloadUnsafe("Context payload has too many values")
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ContextPayloadUnsafe("Context payload has a non-finite number")
            return item
        if isinstance(item, str):
            if len(item) > _MAX_STRING_CHARS:
                raise ContextPayloadUnsafe("Context payload string is too large")
            return item
        if isinstance(item, Mapping):
            normalized: dict[str, object] = {}
            for raw_key, child in item.items():
                if not isinstance(raw_key, str) or _SAFE_KEY.fullmatch(raw_key) is None:
                    raise ContextPayloadUnsafe("Context payload key is invalid")
                if raw_key in _FORBIDDEN_PAYLOAD_KEYS:
                    raise ContextPayloadUnsafe("Context payload contains private material")
                normalized[raw_key] = visit(child, depth=depth + 1)
            return normalized
        if isinstance(item, Sequence) and not isinstance(
            item,
            (str, bytes, bytearray),
        ):
            return [visit(child, depth=depth + 1) for child in item]
        raise ContextPayloadUnsafe("Context payload value is not JSON-safe")

    normalized = visit(value, depth=0)
    if not isinstance(normalized, dict):  # pragma: no cover - guarded above
        raise ContextPayloadUnsafe("Context payload must be an object")
    if contract_version is not None and normalized.get("contract_version") != contract_version:
        raise ContextPayloadUnsafe("Context payload contract version is invalid")
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ContextPayloadUnsafe("Context payload is too large")
    return normalized, hashlib.sha256(encoded).hexdigest()


class ContextEvidenceRepository:
    """Caller-transaction-owned Context Evidence and Projection persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _scope_predicates(row_type: type, scope: ContextEvidenceScope):
        return (
            row_type.project_id == scope.project_id,
            row_type.owner_user_id == scope.owner_user_id,
            row_type.thread_id == scope.thread_id,
        )

    async def _lock_sequence(
        self,
        scope: ContextEvidenceScope,
    ) -> ContextEvidenceSequenceRow:
        if type(scope) is not ContextEvidenceScope:
            raise ValueError("Context Evidence scope is required")
        bind = self.session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)"),
                {"thread_id": scope.thread_id},
            )
        row = (await self.session.execute(select(ContextEvidenceSequenceRow).where(*self._scope_predicates(ContextEvidenceSequenceRow, scope)).with_for_update(of=ContextEvidenceSequenceRow))).scalar_one_or_none()
        if row is not None:
            return row
        thread_exists = await self.session.scalar(
            select(ThreadMetaRow.thread_id).where(
                *self._scope_predicates(ThreadMetaRow, scope),
            )
        )
        if thread_exists is None:
            raise ContextEvidenceScopeNotFound("Context Evidence Thread was not found")
        row = ContextEvidenceSequenceRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            thread_id=scope.thread_id,
            evidence_high_watermark=0,
            projection_high_watermark=0,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def reserve(
        self,
        scope: ContextEvidenceScope,
        *,
        evidence_count: int = 0,
        projection_count: int = 0,
    ) -> ContextSequenceReservation:
        for value in (evidence_count, projection_count):
            if type(value) is not int or value < 0:
                raise ValueError("Context sequence reservation counts must be non-negative")
        if evidence_count == 0 and projection_count == 0:
            raise ValueError("Context sequence reservation cannot be empty")
        row = await self._lock_sequence(scope)
        first_evidence = row.evidence_high_watermark + 1 if evidence_count else None
        first_projection = row.projection_high_watermark + 1 if projection_count else None
        row.evidence_high_watermark += evidence_count
        row.projection_high_watermark += projection_count
        await self.session.flush()
        return ContextSequenceReservation(
            first_evidence_seq=first_evidence,
            evidence_count=evidence_count,
            first_projection_seq=first_projection,
            projection_count=projection_count,
            evidence_high_watermark=row.evidence_high_watermark,
            projection_high_watermark=row.projection_high_watermark,
        )

    async def append(
        self,
        scope: ContextEvidenceScope,
        evidence: ContextEvidenceAppend,
        *,
        evidence_seq: int | None = None,
    ) -> ContextEvidenceRecord:
        if type(evidence) is not ContextEvidenceAppend:
            raise ValueError("Context Evidence append command is required")
        _validate_subject(scope, evidence.subject)
        if evidence.event_type not in CONTEXT_EVIDENCE_TYPES:
            raise ValueError("Context Evidence type is invalid")
        try:
            generation = uuid.UUID(str(evidence.context_window_generation))
        except (TypeError, ValueError):
            raise ValueError("Context Window Generation is invalid") from None
        _validate_optional_identity(
            evidence.origin_run_id,
            label="Context Evidence origin Run identity",
            limit=64,
        )
        _validate_optional_identity(
            evidence.checkpoint_id,
            label="Context Evidence Checkpoint identity",
            limit=128,
        )
        if _SHA256_HEX.fullmatch(evidence.idempotency_key) is None:
            raise ValueError("Context Evidence idempotency key is invalid")
        if evidence.provider_call_id is not None and _SHA256_HEX.fullmatch(evidence.provider_call_id) is None:
            raise ValueError("Context Evidence Provider call identity is invalid")
        if evidence.occurred_at.tzinfo is None or evidence.occurred_at.utcoffset() is None:
            raise ValueError("Context Evidence occurrence time must be timezone-aware")
        payload, payload_digest = _safe_json(evidence.payload, contract_version=None)
        if payload.get("event_type") != evidence.event_type:
            raise ContextPayloadUnsafe("Context Evidence payload type is invalid")
        from deerflow.runtime.context_evidence import ContextEvidence

        subject_mapping: dict[str, object] = {
            "kind": evidence.subject.kind,
            "thread_id": scope.thread_id,
        }
        if evidence.subject.kind == "subagent_task":
            subject_mapping["execution_id"] = evidence.subject.subject_id
        try:
            ContextEvidence.from_safe_mapping(
                {
                    "contract_version": 1,
                    "evidence_seq": str(evidence_seq or 0),
                    "subject": subject_mapping,
                    "generation": {"generation_id": str(generation)},
                    "origin_run_id": evidence.origin_run_id,
                    "occurred_at": evidence.occurred_at.isoformat(),
                    "payload": payload,
                }
            )
        except (TypeError, ValueError):
            raise ContextPayloadUnsafe("Context Evidence payload violates its versioned contract") from None

        sequence = await self._lock_sequence(scope)
        existing = (
            await self.session.execute(
                select(ContextEvidenceRow).where(
                    *self._scope_predicates(ContextEvidenceRow, scope),
                    ContextEvidenceRow.idempotency_key == evidence.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not self._same_evidence(
                existing,
                evidence=evidence,
                generation=generation,
                payload_digest=payload_digest,
            ):
                raise ContextEvidenceIdempotencyConflict("Context Evidence idempotency authority conflict")
            return self._evidence_record(existing)

        if evidence_seq is None:
            evidence_seq = sequence.evidence_high_watermark + 1
            sequence.evidence_high_watermark = evidence_seq
        elif type(evidence_seq) is not int or evidence_seq < 1 or evidence_seq > sequence.evidence_high_watermark:
            raise ContextEvidenceSequenceConflict("Context Evidence sequence was not reserved")
        occupied = await self.session.scalar(
            select(ContextEvidenceRow.evidence_seq).where(
                *self._scope_predicates(ContextEvidenceRow, scope),
                ContextEvidenceRow.evidence_seq == evidence_seq,
            )
        )
        if occupied is not None:
            raise ContextEvidenceSequenceConflict("Context Evidence sequence is already occupied")
        row = ContextEvidenceRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            thread_id=scope.thread_id,
            evidence_seq=evidence_seq,
            subject_kind=evidence.subject.kind,
            subject_id=evidence.subject.subject_id,
            context_window_generation=generation,
            event_type=evidence.event_type,
            payload_schema_version=1,
            origin_run_id=evidence.origin_run_id,
            provider_call_id=evidence.provider_call_id,
            checkpoint_id=evidence.checkpoint_id,
            idempotency_key=evidence.idempotency_key,
            payload_digest=payload_digest,
            payload_json=payload,
            created_at=evidence.occurred_at.astimezone(UTC),
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            raise ContextEvidenceSequenceConflict("Context Evidence append conflicted") from None
        return self._evidence_record(row)

    async def append_safe_contract(
        self,
        scope: ContextEvidenceScope,
        evidence: object,
        *,
        checkpoint_id: str | None = None,
    ) -> ContextEvidenceRecord:
        """Append an already validated core Evidence contract at its reserved seq."""

        command, evidence_seq = ContextEvidenceAppend.from_safe_contract(
            evidence,
            checkpoint_id=checkpoint_id,
        )
        return await self.append(
            scope,
            command,
            evidence_seq=evidence_seq,
        )

    async def upsert_head(
        self,
        scope: ContextEvidenceScope,
        head: ContextProjectionHeadWrite,
    ) -> ContextProjectionHeadRecord:
        if type(head) is not ContextProjectionHeadWrite:
            raise ValueError("Context Projection Head write is required")
        _validate_subject(scope, head.subject)
        if head.phase not in _PHASES:
            raise ValueError("Context Projection phase is invalid")
        if head.basis not in _BASES:
            raise ValueError("Context Projection basis is invalid")
        if head.coverage not in _COVERAGES:
            raise ValueError("Context Projection coverage is invalid")
        if head.freshness not in _FRESHNESS:
            raise ValueError("Context Projection freshness is invalid")
        if type(head.evidence_seq) is not int or head.evidence_seq < 0:
            raise ValueError("Context Projection Evidence sequence is invalid")
        projector_match = _PROJECTOR_REVISION.fullmatch(head.projector_revision)
        if projector_match is None:
            raise ValueError("Context projector revision is invalid")
        projector_ordinal = int(projector_match.group("ordinal"))
        try:
            generation = uuid.UUID(str(head.context_window_generation))
        except (TypeError, ValueError):
            raise ValueError("Context Window Generation is invalid") from None
        _validate_optional_identity(
            head.checkpoint_id,
            label="Context Projection Checkpoint identity",
            limit=128,
        )
        _validate_optional_identity(
            head.active_run_id,
            label="Context Projection active Run identity",
            limit=64,
        )
        sequence = await self._lock_sequence(scope)
        projection_seq = head.projection_seq
        if projection_seq is None:
            projection_seq = sequence.projection_high_watermark + 1
            sequence.projection_high_watermark = projection_seq
        elif type(projection_seq) is not int or projection_seq < 1 or projection_seq > sequence.projection_high_watermark:
            raise ContextEvidenceSequenceConflict("Context Projection sequence was not reserved")
        if head.evidence_seq > sequence.evidence_high_watermark:
            raise ContextEvidenceSequenceConflict("Context Projection references unreserved Evidence")
        projection_input = dict(head.projection)
        if head.projection_seq is None:
            projection_input["projection_seq"] = str(projection_seq)
        projection, payload_digest = _safe_json(
            projection_input,
            contract_version=2,
        )
        from deerflow.runtime.context_evidence import ContextProjectionHead

        try:
            safe_head = ContextProjectionHead.from_safe_mapping(projection)
        except (TypeError, ValueError):
            raise ContextPayloadUnsafe("Context Projection violates its versioned contract") from None
        expected_execution_id = None if head.subject.kind == "lead_thread" else head.subject.subject_id
        if (
            safe_head.thread_id != scope.thread_id
            or safe_head.subject.kind.value != head.subject.kind
            or safe_head.subject.execution_id != expected_execution_id
            or int(safe_head.projection_seq) != projection_seq
            or int(safe_head.evidence_seq) != head.evidence_seq
            or safe_head.projector_revision != head.projector_revision
            or safe_head.context_window_generation != str(generation)
            or safe_head.checkpoint_id != head.checkpoint_id
            or safe_head.phase.value != head.phase
            or safe_head.basis.value != head.basis
            or safe_head.coverage.value != head.coverage
            or safe_head.freshness.value != head.freshness
        ):
            raise ContextPayloadUnsafe("Context Projection columns disagree with its safe payload")

        existing = (
            await self.session.execute(
                select(ContextProjectionHeadRow)
                .where(
                    *self._scope_predicates(ContextProjectionHeadRow, scope),
                    ContextProjectionHeadRow.subject_kind == head.subject.kind,
                    ContextProjectionHeadRow.subject_id == head.subject.subject_id,
                )
                .with_for_update(of=ContextProjectionHeadRow)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if projection_seq == existing.projection_seq:
                if self._same_head(
                    existing,
                    head=head,
                    generation=generation,
                    payload_digest=payload_digest,
                ):
                    return self._head_record(existing)
                raise ContextProjectionConflict("Context Projection sequence identifies different state")
            if projection_seq < existing.projection_seq or head.evidence_seq < existing.evidence_seq or projector_ordinal < self._projector_ordinal(existing.projector_revision):
                raise ContextProjectionConflict("Context Projection cannot move backwards")
            row = existing
        else:
            row = ContextProjectionHeadRow(
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                thread_id=scope.thread_id,
                subject_kind=head.subject.kind,
                subject_id=head.subject.subject_id,
            )
            self.session.add(row)

        row.projection_seq = projection_seq
        row.evidence_seq = head.evidence_seq
        row.projector_revision = head.projector_revision
        row.projection_schema_version = 2
        row.context_window_generation = generation
        row.checkpoint_id = head.checkpoint_id
        row.active_run_id = head.active_run_id
        row.phase = head.phase
        row.basis = head.basis
        row.coverage = head.coverage
        row.freshness = head.freshness
        row.payload_digest = payload_digest
        row.projection_json = projection
        await self.session.flush()
        return self._head_record(row)

    async def upsert_safe_head_contract(
        self,
        scope: ContextEvidenceScope,
        head: object,
        *,
        active_run_id: str | None = None,
    ) -> ContextProjectionHeadRecord:
        """Persist the latest validated core Projection Head contract."""

        return await self.upsert_head(
            scope,
            ContextProjectionHeadWrite.from_safe_contract(
                head,
                active_run_id=active_run_id,
            ),
        )

    async def read_head(
        self,
        scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        *,
        lock: bool = False,
    ) -> ContextProjectionHeadRecord | None:
        _validate_subject(scope, subject)
        statement = select(ContextProjectionHeadRow).where(
            *self._scope_predicates(ContextProjectionHeadRow, scope),
            ContextProjectionHeadRow.subject_kind == subject.kind,
            ContextProjectionHeadRow.subject_id == subject.subject_id,
        )
        if lock:
            statement = statement.with_for_update(of=ContextProjectionHeadRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self._head_record(row)

    async def page_evidence(
        self,
        scope: ContextEvidenceScope,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("Context Evidence cursor is invalid")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Context Evidence page limit is invalid")
        rows = (
            await self.session.execute(
                select(ContextEvidenceRow)
                .where(
                    *self._scope_predicates(ContextEvidenceRow, scope),
                    ContextEvidenceRow.evidence_seq > after_seq,
                )
                .order_by(ContextEvidenceRow.evidence_seq)
                .limit(limit)
            )
        ).scalars()
        return tuple(self._evidence_record(row) for row in rows)

    async def page_subject_evidence(
        self,
        scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        """Page one Subject's Evidence while preserving Thread-wide sequence order."""

        if type(scope) is not ContextEvidenceScope:
            raise ValueError("Context Evidence scope is required")
        _validate_subject(scope, subject)
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("Context Evidence cursor is invalid")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Context Evidence page limit is invalid")
        rows = (
            await self.session.execute(
                select(ContextEvidenceRow)
                .where(
                    *self._scope_predicates(ContextEvidenceRow, scope),
                    ContextEvidenceRow.subject_kind == subject.kind,
                    ContextEvidenceRow.subject_id == subject.subject_id,
                    ContextEvidenceRow.evidence_seq > after_seq,
                )
                .order_by(ContextEvidenceRow.evidence_seq)
                .limit(limit)
            )
        ).scalars()
        return tuple(self._evidence_record(row) for row in rows)

    async def page_provider_call_evidence(
        self,
        scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        provider_call_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        """Page one Provider lifecycle, scoped to its owning Context Subject."""

        if type(scope) is not ContextEvidenceScope:
            raise ValueError("Context Evidence scope is required")
        _validate_subject(scope, subject)
        if not isinstance(provider_call_id, str) or _SHA256_HEX.fullmatch(provider_call_id) is None:
            raise ValueError("Context Evidence Provider call identity is invalid")
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("Context Evidence cursor is invalid")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Context Evidence page limit is invalid")
        rows = (
            await self.session.execute(
                select(ContextEvidenceRow)
                .where(
                    *self._scope_predicates(ContextEvidenceRow, scope),
                    ContextEvidenceRow.provider_call_id == provider_call_id,
                    ContextEvidenceRow.subject_kind == subject.kind,
                    ContextEvidenceRow.subject_id == subject.subject_id,
                    ContextEvidenceRow.evidence_seq > after_seq,
                )
                .order_by(ContextEvidenceRow.evidence_seq)
                .limit(limit)
            )
        ).scalars()
        records = tuple(self._evidence_record(row) for row in rows)
        if any(record.subject != subject for record in records):  # pragma: no cover - SQL invariant
            raise ContextEvidencePersistenceError(
                "Provider lifecycle Evidence belongs to another Context Subject",
            )
        return records

    async def page_subject_event_evidence(
        self,
        scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        event_type: ContextEvidenceType,
        *,
        origin_run_id: str | None = None,
        generation_id: uuid.UUID | None = None,
        checkpoint_id: str | None = None,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        """Page one Subject/event with optional durable authority filters."""

        if type(scope) is not ContextEvidenceScope:
            raise ValueError("Context Evidence scope is required")
        _validate_subject(scope, subject)
        if not isinstance(event_type, str) or event_type not in CONTEXT_EVIDENCE_TYPES:
            raise ValueError("Context Evidence type is invalid")
        if origin_run_id is not None and not isinstance(origin_run_id, str):
            raise ValueError("Context Evidence origin Run identity is invalid")
        if checkpoint_id is not None and not isinstance(checkpoint_id, str):
            raise ValueError("Context Evidence Checkpoint identity is invalid")
        _validate_optional_identity(
            origin_run_id,
            label="Context Evidence origin Run identity",
            limit=64,
        )
        _validate_optional_identity(
            checkpoint_id,
            label="Context Evidence Checkpoint identity",
            limit=128,
        )
        generation = None
        if generation_id is not None:
            try:
                generation = uuid.UUID(str(generation_id))
            except (TypeError, ValueError):
                raise ValueError("Context Window Generation is invalid") from None
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("Context Evidence cursor is invalid")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Context Evidence page limit is invalid")
        predicates = [
            *self._scope_predicates(ContextEvidenceRow, scope),
            ContextEvidenceRow.subject_kind == subject.kind,
            ContextEvidenceRow.subject_id == subject.subject_id,
            ContextEvidenceRow.event_type == event_type,
            ContextEvidenceRow.evidence_seq > after_seq,
        ]
        if origin_run_id is not None:
            predicates.append(
                ContextEvidenceRow.origin_run_id == origin_run_id,
            )
        if generation is not None:
            predicates.append(
                ContextEvidenceRow.context_window_generation == generation,
            )
        if checkpoint_id is not None:
            predicates.append(ContextEvidenceRow.checkpoint_id == checkpoint_id)
        rows = (await self.session.execute(select(ContextEvidenceRow).where(*predicates).order_by(ContextEvidenceRow.evidence_seq).limit(limit))).scalars()
        return tuple(self._evidence_record(row) for row in rows)

    async def read_latest_subject_evidence(
        self,
        scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
    ) -> ContextEvidenceRecord | None:
        """Read the latest durable fact for one Context Subject."""

        if type(scope) is not ContextEvidenceScope:
            raise ValueError("Context Evidence scope is required")
        _validate_subject(scope, subject)
        row = (
            await self.session.execute(
                select(ContextEvidenceRow)
                .where(
                    *self._scope_predicates(ContextEvidenceRow, scope),
                    ContextEvidenceRow.subject_kind == subject.kind,
                    ContextEvidenceRow.subject_id == subject.subject_id,
                )
                .order_by(ContextEvidenceRow.evidence_seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else self._evidence_record(row)

    async def count_subject_run_prepared_requests(
        self,
        scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        origin_run_id: str,
    ) -> int:
        """Count durable Provider-call ordinals for one Run and Subject."""

        if type(scope) is not ContextEvidenceScope:
            raise ValueError("Context Evidence scope is required")
        _validate_subject(scope, subject)
        if not isinstance(origin_run_id, str):
            raise ValueError("Context Evidence origin Run identity is invalid")
        _validate_optional_identity(
            origin_run_id,
            label="Context Evidence origin Run identity",
            limit=64,
        )
        if not origin_run_id:
            raise ValueError("Context Evidence origin Run identity is required")
        count = await self.session.scalar(
            select(func.count())
            .select_from(ContextEvidenceRow)
            .where(
                *self._scope_predicates(ContextEvidenceRow, scope),
                ContextEvidenceRow.subject_kind == subject.kind,
                ContextEvidenceRow.subject_id == subject.subject_id,
                ContextEvidenceRow.origin_run_id == origin_run_id,
                ContextEvidenceRow.event_type == "request.prepared.v1",
            )
        )
        return int(count or 0)

    async def page_heads_after(
        self,
        scope: ContextEvidenceScope,
        *,
        after_projection_seq: int,
        limit: int,
    ) -> tuple[ContextProjectionHeadRecord, ...]:
        """Return current Subject heads published after a Thread-wide cursor."""

        if type(after_projection_seq) is not int or after_projection_seq < 0:
            raise ValueError("Context Projection cursor is invalid")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Context Projection page limit is invalid")
        rows = (
            await self.session.execute(
                select(ContextProjectionHeadRow)
                .where(
                    *self._scope_predicates(ContextProjectionHeadRow, scope),
                    ContextProjectionHeadRow.projection_seq > after_projection_seq,
                )
                .order_by(ContextProjectionHeadRow.projection_seq)
                .limit(limit)
            )
        ).scalars()
        return tuple(self._head_record(row) for row in rows)

    async def delete_head(
        self,
        scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
    ) -> bool:
        """Discard one rebuildable Subject Head without touching Evidence."""

        _validate_subject(scope, subject)
        result = await self.session.execute(
            delete(ContextProjectionHeadRow).where(
                *self._scope_predicates(ContextProjectionHeadRow, scope),
                ContextProjectionHeadRow.subject_kind == subject.kind,
                ContextProjectionHeadRow.subject_id == subject.subject_id,
            )
        )
        return bool(result.rowcount)

    async def purge_thread(
        self,
        scope: ContextEvidenceScope,
    ) -> ContextRetentionPurgeCounts:
        """Delete one exact Thread scope through the retention-only authority."""

        if type(scope) is not ContextEvidenceScope:
            raise ValueError("Context Evidence scope is required")
        bind = self.session.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            raise ContextEvidencePersistenceError("Context Evidence retention requires PostgreSQL")
        await self.session.execute(
            text(
                """CREATE TEMPORARY TABLE IF NOT EXISTS
                   context_evidence_retention_authority (
                       project_id UUID NOT NULL,
                       owner_user_id VARCHAR(36) NOT NULL,
                       thread_id VARCHAR(64) NOT NULL,
                       PRIMARY KEY (project_id, owner_user_id, thread_id)
                   ) ON COMMIT DELETE ROWS"""
            )
        )
        await self.session.execute(text("DELETE FROM pg_temp.context_evidence_retention_authority"))
        await self.session.execute(
            text(
                """INSERT INTO pg_temp.context_evidence_retention_authority
                   (project_id, owner_user_id, thread_id)
                   VALUES (:project_id, :owner_user_id, :thread_id)"""
            ),
            {
                "project_id": scope.project_id,
                "owner_user_id": scope.owner_user_id,
                "thread_id": scope.thread_id,
            },
        )
        evidence_result = await self.session.execute(delete(ContextEvidenceRow).where(*self._scope_predicates(ContextEvidenceRow, scope)))
        head_result = await self.session.execute(delete(ContextProjectionHeadRow).where(*self._scope_predicates(ContextProjectionHeadRow, scope)))
        sequence_result = await self.session.execute(delete(ContextEvidenceSequenceRow).where(*self._scope_predicates(ContextEvidenceSequenceRow, scope)))
        return ContextRetentionPurgeCounts(
            evidence=max(int(evidence_result.rowcount or 0), 0),
            projection_heads=max(int(head_result.rowcount or 0), 0),
            sequence=max(int(sequence_result.rowcount or 0), 0),
        )

    @staticmethod
    def _projector_ordinal(revision: str) -> int:
        match = _PROJECTOR_REVISION.fullmatch(revision)
        if match is None:  # pragma: no cover - database constraint invariant
            raise ContextProjectionConflict("Stored Context projector is invalid")
        return int(match.group("ordinal"))

    @staticmethod
    def _same_evidence(
        row: ContextEvidenceRow,
        *,
        evidence: ContextEvidenceAppend,
        generation: uuid.UUID,
        payload_digest: str,
    ) -> bool:
        return (
            row.subject_kind == evidence.subject.kind
            and row.subject_id == evidence.subject.subject_id
            and row.context_window_generation == generation
            and row.event_type == evidence.event_type
            and row.origin_run_id == evidence.origin_run_id
            and row.provider_call_id == evidence.provider_call_id
            and row.checkpoint_id == evidence.checkpoint_id
            and row.payload_digest == payload_digest
        )

    @staticmethod
    def _same_head(
        row: ContextProjectionHeadRow,
        *,
        head: ContextProjectionHeadWrite,
        generation: uuid.UUID,
        payload_digest: str,
    ) -> bool:
        return (
            row.subject_kind == head.subject.kind
            and row.subject_id == head.subject.subject_id
            and row.evidence_seq == head.evidence_seq
            and row.projector_revision == head.projector_revision
            and row.context_window_generation == generation
            and row.checkpoint_id == head.checkpoint_id
            and row.active_run_id == head.active_run_id
            and row.phase == head.phase
            and row.basis == head.basis
            and row.coverage == head.coverage
            and row.freshness == head.freshness
            and row.payload_digest == payload_digest
        )

    @staticmethod
    def _evidence_record(row: ContextEvidenceRow) -> ContextEvidenceRecord:
        return ContextEvidenceRecord(
            scope=ContextEvidenceScope(
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                thread_id=row.thread_id,
            ),
            evidence_seq=row.evidence_seq,
            subject=ContextSubjectRef(
                kind=row.subject_kind,
                subject_id=row.subject_id,
            ),
            context_window_generation=row.context_window_generation,
            event_type=row.event_type,
            origin_run_id=row.origin_run_id,
            provider_call_id=row.provider_call_id,
            checkpoint_id=row.checkpoint_id,
            idempotency_key=row.idempotency_key,
            payload_digest=row.payload_digest,
            payload=dict(row.payload_json),
            occurred_at=row.created_at,
        )

    @staticmethod
    def _head_record(row: ContextProjectionHeadRow) -> ContextProjectionHeadRecord:
        return ContextProjectionHeadRecord(
            scope=ContextEvidenceScope(
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                thread_id=row.thread_id,
            ),
            subject=ContextSubjectRef(
                kind=row.subject_kind,
                subject_id=row.subject_id,
            ),
            projection_seq=row.projection_seq,
            evidence_seq=row.evidence_seq,
            projector_revision=row.projector_revision,
            context_window_generation=row.context_window_generation,
            checkpoint_id=row.checkpoint_id,
            active_run_id=row.active_run_id,
            phase=row.phase,
            basis=row.basis,
            coverage=row.coverage,
            freshness=row.freshness,
            payload_digest=row.payload_digest,
            projection=dict(row.projection_json),
            updated_at=row.updated_at,
        )


__all__ = [
    "CONTEXT_EVIDENCE_TYPES",
    "ContextEvidenceAppend",
    "ContextEvidenceIdempotencyConflict",
    "ContextEvidencePersistenceError",
    "ContextEvidenceRecord",
    "ContextEvidenceRepository",
    "ContextEvidenceScope",
    "ContextEvidenceScopeNotFound",
    "ContextEvidenceSequenceConflict",
    "ContextEvidenceType",
    "ContextPayloadUnsafe",
    "ContextProjectionConflict",
    "ContextProjectionHeadRecord",
    "ContextProjectionHeadWrite",
    "ContextRetentionPurgeCounts",
    "ContextSequenceReservation",
    "ContextSubjectKind",
    "ContextSubjectRef",
]
