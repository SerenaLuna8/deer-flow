"""Application-transaction composition for Context Evidence and Projection Heads."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel

from deerflow.persistence.context_evidence import (
    ContextEvidenceAppend,
    ContextEvidenceRecord,
    ContextEvidenceRepository,
    ContextEvidenceScope,
    ContextProjectionHeadWrite,
    ContextSubjectRef,
)
from deerflow.runtime.context_evidence import (
    CheckpointLinkedV1,
    CompactionAuthority,
    CompactionCommittedV1,
    CompactionProjection,
    ContextCheckpointProjectionSnapshot,
    ContextContribution,
    ContextEvidence,
    ContextLane,
    ContextModelProjection,
    ContextProjectionHead,
    ContextProjectionSource,
    ContextProjector,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProjectionFreshness,
    ProjectionPhase,
    RequestPreparedV1,
    TokenEstimate,
    WindowOpenedV1,
    WindowRebasedV1,
)

CONTEXT_PROJECTOR_REVISION = "context-projector-v1"
_PAGE_SIZE = 1000


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _subject_ref(source: ContextProjectionSource) -> ContextSubjectRef:
    subject = source.subject
    if subject.execution_id is None:
        return ContextSubjectRef.lead_thread(subject.thread_id)
    return ContextSubjectRef.subagent_task(subject.execution_id)


def empty_lead_context_projection(
    thread_id: str,
    *,
    as_of: datetime | None = None,
) -> ContextProjectionHead:
    """Return a truthful zero reading before a Thread has measurable Evidence."""

    generation = ContextWindowGeneration(
        generation_id=uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"actweave-empty-context-v1:{thread_id}",
        )
    )
    measurement = FinalRequestMeasurement(
        request_fingerprint=hashlib.sha256(
            _canonical_json(
                {
                    "generation": generation.generation_id,
                    "thread_id": thread_id,
                }
            )
        ).hexdigest(),
        adapter_revision="empty-context-v1",
        contributions=(),
    )
    return ContextProjector.rebuild(
        source=ContextProjectionSource(
            subject=ContextSubject.lead_thread(thread_id=thread_id),
            phase=ProjectionPhase.IDLE,
            generation=generation,
            checkpoint_id=None,
            model=ContextModelProjection(
                identity_digest=hashlib.sha256(b"unknown-context-model-v1").hexdigest(),
                context_window_tokens=None,
            ),
            measurement=measurement,
            current_provider_call_id=None,
            compaction=CompactionProjection(enabled=False, reached=False),
            freshness=ProjectionFreshness.CURRENT,
        ),
        evidence=(),
        projection_seq="0",
        projector_revision=CONTEXT_PROJECTOR_REVISION,
        as_of=as_of or datetime.now(UTC),
    )


def _opened_compaction(
    opened: WindowOpenedV1,
    measurement: FinalRequestMeasurement,
) -> CompactionProjection:
    threshold = opened.compaction_threshold_tokens
    return CompactionProjection(
        enabled=opened.compaction_enabled,
        threshold_tokens=threshold,
        reached=bool(opened.compaction_enabled and threshold is not None and measurement.projected_tokens >= threshold),
        authority=(CompactionAuthority(opened.compaction_authority) if opened.compaction_authority is not None else None),
    )


def _payload_provider_call_id(payload: BaseModel) -> str | None:
    if isinstance(payload, RequestPreparedV1):
        return payload.provider_call.provider_call_id
    value = getattr(payload, "provider_call_id", None)
    return value if isinstance(value, str) and value else None


def _payload_checkpoint_id(payload: BaseModel) -> str | None:
    if isinstance(payload, CheckpointLinkedV1):
        return payload.checkpoint_id
    if isinstance(payload, (CompactionCommittedV1, WindowRebasedV1)):
        return payload.result_checkpoint_id
    return None


def _idempotency_key(
    *,
    source: ContextProjectionSource,
    origin_run_id: str | None,
    payload: dict[str, object],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "generation": source.generation.generation_id,
                "origin_run_id": origin_run_id,
                "payload": payload,
                "subject": source.subject.to_safe_mapping(),
            }
        )
    ).hexdigest()


def context_evidence_record_to_core(
    record: ContextEvidenceRecord,
) -> ContextEvidence:
    subject = (
        {
            "kind": "lead_thread",
            "thread_id": record.scope.thread_id,
            "execution_id": None,
        }
        if record.subject.kind == "lead_thread"
        else {
            "kind": "subagent_task",
            "thread_id": record.scope.thread_id,
            "execution_id": record.subject.subject_id,
        }
    )
    return ContextEvidence.from_safe_mapping(
        {
            "contract_version": 1,
            "evidence_seq": str(record.evidence_seq),
            "subject": subject,
            "generation": {
                "generation_id": str(record.context_window_generation),
            },
            "origin_run_id": record.origin_run_id,
            "occurred_at": record.occurred_at.isoformat(),
            "payload": record.payload,
        }
    )


class ContextProjectionTransaction:
    """Write minimized Evidence and its latest read model in one caller transaction."""

    def __init__(
        self,
        repository: ContextEvidenceRepository,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._now = now or (lambda: datetime.now(UTC))

    async def _provider_call_evidence(
        self,
        scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        provider_call_id: str,
    ) -> tuple[ContextEvidence, ...]:
        records: list[ContextEvidenceRecord] = []
        cursor = 0
        while True:
            page = await self._repository.page_provider_call_evidence(
                scope,
                subject,
                provider_call_id,
                after_seq=cursor,
                limit=_PAGE_SIZE,
            )
            records.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            cursor = page[-1].evidence_seq
        return tuple(context_evidence_record_to_core(record) for record in records)

    async def append_only(
        self,
        *,
        scope: ContextEvidenceScope,
        source: ContextProjectionSource,
        payloads: Sequence[BaseModel],
        origin_run_id: str | None,
    ) -> tuple[ContextEvidence, ...]:
        if not payloads:
            return ()
        reservation = await self._repository.reserve(
            scope,
            evidence_count=len(payloads),
            projection_count=0,
        )
        first = reservation.first_evidence_seq
        if first is None:
            raise RuntimeError("Context Evidence reservation is incomplete")
        subject = _subject_ref(source)
        occurred_at = self._now()
        appended: list[ContextEvidence] = []
        for offset, payload in enumerate(payloads):
            payload_mapping = payload.model_dump(mode="json", exclude_none=True)
            record = await self._repository.append(
                scope,
                ContextEvidenceAppend(
                    subject=subject,
                    context_window_generation=source.generation.generation_id,
                    event_type=payload_mapping["event_type"],
                    origin_run_id=origin_run_id,
                    provider_call_id=_payload_provider_call_id(payload),
                    checkpoint_id=_payload_checkpoint_id(payload),
                    idempotency_key=_idempotency_key(
                        source=source,
                        origin_run_id=origin_run_id,
                        payload=payload_mapping,
                    ),
                    payload=payload_mapping,
                    occurred_at=occurred_at,
                ),
                evidence_seq=first + offset,
            )
            appended.append(context_evidence_record_to_core(record))
        return tuple(appended)

    async def append_and_project(
        self,
        *,
        scope: ContextEvidenceScope,
        source: ContextProjectionSource,
        payloads: Sequence[BaseModel],
        origin_run_id: str | None,
        active_run_id: str | None,
    ) -> ContextProjectionHead:
        subject = _subject_ref(source)
        current_record = await self._repository.read_head(
            scope,
            subject,
            lock=True,
        )
        reservation = await self._repository.reserve(
            scope,
            evidence_count=len(payloads),
            projection_count=1,
        )
        first = reservation.first_evidence_seq
        if payloads and first is None:
            raise RuntimeError("Context Evidence reservation is incomplete")
        occurred_at = self._now()
        appended: list[ContextEvidence] = []
        for offset, payload in enumerate(payloads):
            payload_mapping = payload.model_dump(mode="json", exclude_none=True)
            record = await self._repository.append(
                scope,
                ContextEvidenceAppend(
                    subject=subject,
                    context_window_generation=source.generation.generation_id,
                    event_type=payload_mapping["event_type"],
                    origin_run_id=origin_run_id,
                    provider_call_id=_payload_provider_call_id(payload),
                    checkpoint_id=_payload_checkpoint_id(payload),
                    idempotency_key=_idempotency_key(
                        source=source,
                        origin_run_id=origin_run_id,
                        payload=payload_mapping,
                    ),
                    payload=payload_mapping,
                    occurred_at=occurred_at,
                ),
                evidence_seq=(first + offset if first is not None else None),
            )
            appended.append(context_evidence_record_to_core(record))
        provider_call_id = source.current_provider_call_id
        if provider_call_id is None:
            allowed_provider_free_payloads = (
                CompactionCommittedV1,
                WindowOpenedV1,
                WindowRebasedV1,
            )
            if any(not isinstance(item.payload, allowed_provider_free_payloads) for item in appended):
                raise RuntimeError(
                    "Provider-free Context Projection accepts only replacement or window Evidence",
                )
            evidence = tuple(appended)
        else:
            if any((call_id := _payload_provider_call_id(item.payload)) is not None and call_id != provider_call_id for item in appended):
                raise RuntimeError(
                    "Context Projection Evidence belongs to another Provider call",
                )
            lifecycle = await self._provider_call_evidence(
                scope,
                subject,
                provider_call_id,
            )
            by_sequence = {int(item.evidence_seq): item for item in lifecycle}
            for item in appended:
                sequence = int(item.evidence_seq)
                existing = by_sequence.get(sequence)
                if existing is not None and existing != item:
                    raise RuntimeError(
                        "Context Projection Evidence sequence is inconsistent",
                    )
                by_sequence[sequence] = item
            evidence = tuple(by_sequence[sequence] for sequence in sorted(by_sequence))
        projection_seq = reservation.first_projection_seq
        if projection_seq is None:
            raise RuntimeError("Context Projection reservation is incomplete")
        if current_record is None:
            projection = ContextProjector.rebuild(
                source=source,
                evidence=evidence,
                evidence_high_watermark=str(
                    reservation.evidence_high_watermark,
                ),
                projection_seq=str(projection_seq),
                projector_revision=CONTEXT_PROJECTOR_REVISION,
                as_of=occurred_at,
            )
        else:
            projection = ContextProjector.reduce(
                current=ContextProjectionHead.from_safe_mapping(
                    current_record.projection,
                ),
                source=source,
                evidence=evidence,
                evidence_high_watermark=str(
                    reservation.evidence_high_watermark,
                ),
                projection_seq=str(projection_seq),
                projector_revision=CONTEXT_PROJECTOR_REVISION,
                as_of=occurred_at,
            )
        await self._repository.upsert_head(
            scope,
            ContextProjectionHeadWrite(
                subject=subject,
                projection_seq=projection_seq,
                evidence_seq=int(projection.evidence_seq),
                projector_revision=projection.projector_revision,
                context_window_generation=source.generation.generation_id,
                checkpoint_id=source.checkpoint_id,
                active_run_id=active_run_id,
                phase=projection.phase.value,
                basis=projection.basis.value,
                coverage=projection.coverage.value,
                freshness=projection.freshness.value,
                projection=projection.to_safe_mapping(),
            ),
        )
        return projection


async def rebuild_context_projection_head(
    repository: ContextEvidenceRepository,
    *,
    scope: ContextEvidenceScope,
    subject: ContextSubjectRef,
    discard_existing_head: bool,
) -> ContextProjectionHead:
    """Repair one Head solely from safe Evidence after read-side validation."""

    records: list[ContextEvidenceRecord] = []
    cursor = 0
    while True:
        page = await repository.page_evidence(
            scope,
            after_seq=cursor,
            limit=_PAGE_SIZE,
        )
        records.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        cursor = page[-1].evidence_seq
    evidence = tuple(context_evidence_record_to_core(record) for record in records)
    subject_evidence = tuple(
        item for item in evidence if item.subject.thread_id == scope.thread_id and item.subject.kind.value == subject.kind and (item.subject.execution_id == (None if subject.kind == "lead_thread" else subject.subject_id))
    )
    prepared = next(
        (item for item in reversed(subject_evidence) if isinstance(item.payload, RequestPreparedV1)),
        None,
    )
    replacement = next(
        (
            item
            for item in reversed(subject_evidence)
            if isinstance(
                item.payload,
                (CompactionCommittedV1, WindowRebasedV1),
            )
        ),
        None,
    )
    effective_generation: ContextWindowGeneration | None = None
    effective_checkpoint_id: str | None = None
    effective_current_provider_call = True
    if replacement is not None and (prepared is None or int(replacement.evidence_seq) > int(prepared.evidence_seq)):
        if isinstance(replacement.payload, CompactionCommittedV1):
            if replacement.payload.source_state_digest is not None and replacement.payload.result_state_digest is not None and replacement.payload.summary_tokens is not None:
                opened_for_result = next(
                    (item.payload for item in reversed(subject_evidence) if item.generation == replacement.payload.result_generation and isinstance(item.payload, WindowOpenedV1)),
                    None,
                )
                if not isinstance(opened_for_result, WindowOpenedV1):
                    raise LookupError("Ephemeral compaction has no result model window")
                fixed = (
                    [
                        contribution
                        for contribution in prepared.payload.measurement.contributions
                        if contribution.lane
                        in {
                            ContextLane.SYSTEM_PROMPT,
                            ContextLane.AGENT_INSTRUCTIONS,
                            ContextLane.TOOL_DEFINITIONS,
                            ContextLane.SKILLS,
                            ContextLane.MCP_DYNAMIC_TOOLS,
                            ContextLane.SUBAGENT_DEFINITIONS,
                        }
                    ]
                    if prepared is not None and isinstance(prepared.payload, RequestPreparedV1)
                    else []
                )

                def dynamic_contribution(
                    lane: ContextLane,
                    tokens: int,
                    identity: str,
                ) -> ContextContribution | None:
                    if tokens <= 0:
                        return None
                    source_identity = hashlib.sha256(f"{lane.value}:{identity}".encode()).hexdigest()
                    return ContextContribution(
                        contribution_id=hashlib.sha256(f"subagent-compaction-repair-v1:{source_identity}".encode()).hexdigest(),
                        source_identity_digest=source_identity,
                        lane=lane,
                        model_visible_bytes=0,
                        token_estimate=TokenEstimate.bounded(
                            projected_tokens=tokens,
                            lower_bound_tokens=0,
                            safety_upper_bound_tokens=(tokens + max(1, (tokens + 3) // 4)),
                        ),
                    )

                summary_tokens = replacement.payload.summary_tokens
                conversation = dynamic_contribution(
                    ContextLane.CONVERSATION,
                    replacement.payload.result_tokens - summary_tokens,
                    replacement.payload.result_state_digest,
                )
                summary = dynamic_contribution(
                    ContextLane.SUMMARIZED_CONVERSATION,
                    summary_tokens,
                    replacement.payload.summary_digest,
                )
                contributions = fixed
                if conversation is not None:
                    contributions.append(conversation)
                if summary is not None:
                    contributions.append(summary)
                contributions.sort(
                    key=lambda item: (
                        tuple(ContextLane).index(item.lane),
                        item.contribution_id,
                    )
                )
                measurement = FinalRequestMeasurement(
                    request_fingerprint=(replacement.payload.result_state_digest),
                    adapter_revision="subagent-compaction-repair-v1",
                    contributions=tuple(contributions),
                )
                source = ContextProjectionSource(
                    subject=replacement.subject,
                    phase=ProjectionPhase.SETTLED,
                    generation=replacement.payload.result_generation,
                    checkpoint_id=None,
                    model=ContextModelProjection(
                        identity_digest=(opened_for_result.model_identity_digest),
                        context_window_tokens=(opened_for_result.context_window_tokens),
                    ),
                    measurement=measurement,
                    current_provider_call_id=None,
                    compaction=_opened_compaction(
                        opened_for_result,
                        measurement,
                    ),
                    freshness=ProjectionFreshness.STALE,
                )
                if discard_existing_head:
                    await repository.delete_head(scope, subject)
                return await ContextProjectionTransaction(
                    repository,
                ).append_and_project(
                    scope=scope,
                    source=source,
                    payloads=(),
                    origin_run_id=replacement.origin_run_id,
                    active_run_id=None,
                )
            # The compaction checkpoint receipt owns its result measurement;
            # Evidence deliberately stores only sizes and digests.
            raise LookupError("Compacted Context Projection requires its checkpoint receipt")
        result_checkpoint_id = replacement.payload.result_checkpoint_id
        linked_for_result = next(
            (item.payload for item in reversed(subject_evidence) if isinstance(item.payload, CheckpointLinkedV1) and item.payload.checkpoint_id == result_checkpoint_id),
            None,
        )
        if not isinstance(linked_for_result, CheckpointLinkedV1):
            # A branch intentionally copies no source Evidence; its target
            # checkpoint snapshot supplies the first Projection instead.
            raise LookupError("Rebased Context Projection requires its checkpoint snapshot")
        prepared = next(
            (item for item in reversed(subject_evidence) if isinstance(item.payload, RequestPreparedV1) and item.payload.provider_call.provider_call_id == linked_for_result.provider_call_id),
            None,
        )
        if prepared is None:
            raise LookupError("Rebased Context Projection has no linked request Evidence")
        effective_generation = replacement.payload.result_generation
        effective_checkpoint_id = result_checkpoint_id
        effective_current_provider_call = False
    if prepared is not None and isinstance(prepared.payload, RequestPreparedV1):
        provider_call = prepared.payload.provider_call
        opened = next(
            (item.payload for item in reversed(subject_evidence) if item.generation == provider_call.generation and isinstance(item.payload, WindowOpenedV1)),
            None,
        )
        if not isinstance(opened, WindowOpenedV1):
            raise ValueError("Context Window has no opening Evidence")
        linked = next(
            (item.payload for item in reversed(subject_evidence) if isinstance(item.payload, CheckpointLinkedV1) and item.payload.provider_call_id == provider_call.provider_call_id),
            None,
        )
        source = ContextProjectionSource(
            subject=provider_call.subject,
            phase=(ProjectionPhase.IDLE if subject.kind == "lead_thread" else ProjectionPhase.SETTLED),
            generation=effective_generation or provider_call.generation,
            checkpoint_id=(effective_checkpoint_id or (linked.checkpoint_id if isinstance(linked, CheckpointLinkedV1) else provider_call.source_checkpoint_id)),
            model=ContextModelProjection(
                identity_digest=opened.model_identity_digest,
                context_window_tokens=opened.context_window_tokens,
            ),
            measurement=prepared.payload.measurement,
            current_provider_call_id=(provider_call.provider_call_id if effective_current_provider_call else None),
            compaction=_opened_compaction(
                opened,
                prepared.payload.measurement,
            ),
            freshness=ProjectionFreshness.CURRENT,
        )
        origin_run_id = replacement.origin_run_id if effective_generation is not None and replacement is not None else prepared.origin_run_id
    else:
        # Provider-free Task termination records only the opened Context Window.
        # Rebuild its empty Head from that durable fact rather than declaring
        # the otherwise healthy Thread projection unavailable.
        opened_evidence = next(
            (item for item in reversed(subject_evidence) if isinstance(item.payload, WindowOpenedV1)),
            None,
        )
        if opened_evidence is None or not isinstance(
            opened_evidence.payload,
            WindowOpenedV1,
        ):
            raise LookupError("Context Projection has no rebuildable Evidence")
        fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "generation": opened_evidence.generation.generation_id,
                    "subject": opened_evidence.subject.to_safe_mapping(),
                }
            )
        ).hexdigest()
        empty_measurement = FinalRequestMeasurement(
            request_fingerprint=fingerprint,
            adapter_revision="empty-context-v1",
            contributions=(),
        )
        source = ContextProjectionSource(
            subject=opened_evidence.subject,
            phase=(ProjectionPhase.IDLE if subject.kind == "lead_thread" else ProjectionPhase.SETTLED),
            generation=opened_evidence.generation,
            checkpoint_id=None,
            model=ContextModelProjection(
                identity_digest=opened_evidence.payload.model_identity_digest,
                context_window_tokens=opened_evidence.payload.context_window_tokens,
            ),
            measurement=empty_measurement,
            current_provider_call_id=None,
            compaction=_opened_compaction(
                opened_evidence.payload,
                empty_measurement,
            ),
            freshness=ProjectionFreshness.CURRENT,
        )
        origin_run_id = opened_evidence.origin_run_id
    if discard_existing_head:
        await repository.delete_head(scope, subject)
    return await ContextProjectionTransaction(repository).append_and_project(
        scope=scope,
        source=source,
        payloads=(),
        origin_run_id=origin_run_id,
        active_run_id=None,
    )


async def align_checkpoint_snapshot_to_rebase(
    repository: ContextEvidenceRepository,
    *,
    scope: ContextEvidenceScope,
    subject: ContextSubjectRef,
    checkpoint_id: str,
    snapshot: ContextCheckpointProjectionSnapshot,
) -> ContextCheckpointProjectionSnapshot:
    """Apply a durable rebase generation to an older checkpoint snapshot."""

    records: list[ContextEvidenceRecord] = []
    cursor = 0
    while True:
        page = await repository.page_evidence(
            scope,
            after_seq=cursor,
            limit=_PAGE_SIZE,
        )
        records.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        cursor = page[-1].evidence_seq
    replacement = next(
        (
            WindowRebasedV1.model_validate_json(
                json.dumps(record.payload, separators=(",", ":")),
            )
            for record in reversed(records)
            if record.subject == subject and record.event_type == "context.window.rebased.v1" and record.checkpoint_id == checkpoint_id
        ),
        None,
    )
    if replacement is None:
        return snapshot
    return snapshot.without_provider_response_authority().model_copy(
        update={"generation": replacement.result_generation},
    )


__all__ = [
    "CONTEXT_PROJECTOR_REVISION",
    "ContextProjectionTransaction",
    "align_checkpoint_snapshot_to_rebase",
    "context_evidence_record_to_core",
    "empty_lead_context_projection",
    "rebuild_context_projection_head",
]
