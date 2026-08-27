from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.private_work.context_projection import (
    ContextProjectionTransaction,
    rebuild_context_projection_head,
)
from deerflow.persistence.context_evidence import (
    ContextEvidenceRecord,
    ContextEvidenceScope,
    ContextSequenceReservation,
    ContextSubjectRef,
)
from deerflow.runtime.context_evidence import (
    CompactionAuthority,
    CompactionCommittedV1,
    CompactionProjection,
    ContextContribution,
    ContextLane,
    ContextModelProjection,
    ContextProjectionSource,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProjectionFreshness,
    ProjectionPhase,
    ProviderCallIdentity,
    ProviderObservedV1,
    RequestDispatchedV1,
    RequestPreparedV1,
    TokenEstimate,
    WindowOpenedV1,
)

THREAD_ID = "11111111-1111-4111-8111-111111111111"
GENERATION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _measurement() -> FinalRequestMeasurement:
    return FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="test-adapter-v1",
        contributions=(
            ContextContribution(
                contribution_id="b" * 64,
                source_identity_digest="c" * 64,
                lane=ContextLane.CONVERSATION,
                model_visible_bytes=400,
                token_estimate=TokenEstimate.exact(100),
            ),
        ),
    )


class _Repository:
    def __init__(self) -> None:
        self.evidence: list[ContextEvidenceRecord] = []
        self.heads: list[object] = []
        self.head_by_subject: dict[tuple[str, str], object] = {}
        self.thread_page_calls = 0
        self.subject_page_calls: list[ContextSubjectRef] = []
        self.subject_page_sizes: list[int] = []
        self.provider_page_calls: list[tuple[ContextSubjectRef, str]] = []
        self.provider_page_sizes: list[int] = []
        self.evidence_high_watermark = 0
        self.projection_high_watermark = 0

    async def reserve(
        self,
        _scope: ContextEvidenceScope,
        *,
        evidence_count: int,
        projection_count: int,
    ) -> ContextSequenceReservation:
        first_evidence = self.evidence_high_watermark + 1 if evidence_count else None
        first_projection = self.projection_high_watermark + 1 if projection_count else None
        self.evidence_high_watermark += evidence_count
        self.projection_high_watermark += projection_count
        return ContextSequenceReservation(
            first_evidence_seq=first_evidence,
            evidence_count=evidence_count,
            first_projection_seq=first_projection,
            projection_count=projection_count,
            evidence_high_watermark=self.evidence_high_watermark,
            projection_high_watermark=self.projection_high_watermark,
        )

    async def append(
        self,
        scope: ContextEvidenceScope,
        command: object,
        *,
        evidence_seq: int | None,
    ) -> ContextEvidenceRecord:
        assert evidence_seq is not None
        record = ContextEvidenceRecord(
            scope=scope,
            evidence_seq=evidence_seq,
            subject=command.subject,
            context_window_generation=command.context_window_generation,
            event_type=command.event_type,
            origin_run_id=command.origin_run_id,
            provider_call_id=command.provider_call_id,
            checkpoint_id=command.checkpoint_id,
            idempotency_key=command.idempotency_key,
            payload_digest="d" * 64,
            payload=dict(command.payload),
            occurred_at=command.occurred_at,
        )
        self.evidence.append(record)
        return record

    async def page_evidence(
        self,
        _scope: ContextEvidenceScope,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        assert limit == 1000
        self.thread_page_calls += 1
        return tuple(item for item in self.evidence if item.evidence_seq > after_seq)

    async def page_subject_evidence(
        self,
        _scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        assert limit == 1000
        self.subject_page_calls.append(subject)
        page = tuple(item for item in self.evidence if item.subject == subject and item.evidence_seq > after_seq)[:limit]
        self.subject_page_sizes.append(len(page))
        return page

    async def page_provider_call_evidence(
        self,
        _scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        provider_call_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        assert limit == 1000
        self.provider_page_calls.append((subject, provider_call_id))
        page = tuple(item for item in self.evidence if item.subject == subject and item.provider_call_id == provider_call_id and item.evidence_seq > after_seq)[:limit]
        self.provider_page_sizes.append(len(page))
        return page

    async def read_head(
        self,
        _scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
        *,
        lock: bool,
    ) -> object | None:
        assert lock is True
        return self.head_by_subject.get((subject.kind, subject.subject_id))

    async def upsert_head(
        self,
        scope: ContextEvidenceScope,
        command: object,
    ) -> object:
        self.heads.append(command)
        record = SimpleNamespace(
            scope=scope,
            projection=dict(command.projection),
        )
        self.head_by_subject[(command.subject.kind, command.subject.subject_id)] = record
        return record

    async def delete_head(
        self,
        _scope: ContextEvidenceScope,
        subject: ContextSubjectRef,
    ) -> bool:
        return self.head_by_subject.pop((subject.kind, subject.subject_id), None) is not None


@pytest.mark.asyncio
async def test_evidence_and_projection_head_share_one_transaction_builder() -> None:
    scope = ContextEvidenceScope(
        project_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        owner_user_id="55555555-5555-4555-8555-555555555555",
        thread_id=THREAD_ID,
    )
    subject = ContextSubject.lead_thread(thread_id=THREAD_ID)
    generation = ContextWindowGeneration(generation_id=GENERATION_ID)
    measurement = _measurement()
    provider_call = ProviderCallIdentity.derive(
        subject=subject,
        generation=generation,
        source_checkpoint_id="checkpoint-1",
        graph_step="model",
        model_call_ordinal=0,
        request_fingerprint=measurement.request_fingerprint,
    )
    source = ContextProjectionSource(
        subject=subject,
        phase=ProjectionPhase.ACTIVE,
        generation=generation,
        checkpoint_id="checkpoint-1",
        model=ContextModelProjection(
            identity_digest="e" * 64,
            context_window_tokens=300_000,
        ),
        measurement=measurement,
        current_provider_call_id=provider_call.provider_call_id,
        compaction=CompactionProjection(
            enabled=True,
            threshold_tokens=240_000,
            reached=False,
            authority=CompactionAuthority.FROZEN_RUN,
        ),
        freshness=ProjectionFreshness.CURRENT,
    )
    repository = _Repository()
    transaction = ContextProjectionTransaction(repository, now=lambda: NOW)

    projection = await transaction.append_and_project(
        scope=scope,
        source=source,
        payloads=(
            WindowOpenedV1(
                model_identity_digest="e" * 64,
                context_window_tokens=300_000,
                compaction_enabled=True,
                compaction_threshold_tokens=240_000,
                compaction_authority="frozen_run",
            ),
            RequestPreparedV1(
                provider_call=provider_call,
                measurement=measurement,
            ),
            RequestDispatchedV1(provider_call_id=provider_call.provider_call_id),
            ProviderObservedV1(
                provider_call_id=provider_call.provider_call_id,
                input_tokens=103,
            ),
        ),
        origin_run_id="run-1",
        active_run_id="run-1",
    )

    assert [item.evidence_seq for item in repository.evidence] == [1, 2, 3, 4]
    assert projection.projection_seq == "1"
    assert projection.evidence_seq == "4"
    assert projection.totals.projected_tokens == 100
    assert projection.last_provider_observation is not None
    assert projection.last_provider_observation.input_tokens == 103
    assert len(repository.heads) == 1
    assert repository.heads[0].projection == projection.to_safe_mapping()
    rendered_evidence = repr([item.payload for item in repository.evidence])
    assert "prompt" not in rendered_evidence
    assert "message" not in rendered_evidence

    repaired = await rebuild_context_projection_head(
        repository,
        scope=scope,
        subject=ContextSubjectRef.lead_thread(THREAD_ID),
        discard_existing_head=False,
    )
    assert repaired.phase.value == "idle"
    assert repaired.projection_seq == "2"
    assert repaired.evidence_seq == "4"
    assert repaired.last_provider_observation is not None
    assert repaired.last_provider_observation.input_tokens == 103
    assert repaired.compaction.enabled is True
    assert repaired.compaction.threshold_tokens == 240_000
    assert repaired.compaction.authority is CompactionAuthority.FROZEN_RUN


@pytest.mark.asyncio
async def test_online_projection_reads_only_current_call_and_keeps_thread_high_watermark() -> None:
    scope = ContextEvidenceScope(
        project_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        owner_user_id="55555555-5555-4555-8555-555555555555",
        thread_id=THREAD_ID,
    )
    execution_id = uuid.UUID("66666666-6666-4666-8666-666666666666")
    subject = ContextSubject.subagent_task(
        thread_id=THREAD_ID,
        execution_id=execution_id,
    )
    subject_ref = ContextSubjectRef.subagent_task(execution_id)
    generation = ContextWindowGeneration(generation_id=GENERATION_ID)
    measurement = _measurement()
    provider_call = ProviderCallIdentity.derive(
        subject=subject,
        generation=generation,
        source_checkpoint_id="task-state-1",
        graph_step="subagent:model",
        model_call_ordinal=0,
        request_fingerprint=measurement.request_fingerprint,
    )
    source = ContextProjectionSource(
        subject=subject,
        phase=ProjectionPhase.ACTIVE,
        generation=generation,
        checkpoint_id=None,
        model=ContextModelProjection(
            identity_digest="e" * 64,
            context_window_tokens=300_000,
        ),
        measurement=measurement,
        current_provider_call_id=provider_call.provider_call_id,
        compaction=CompactionProjection(
            enabled=True,
            threshold_tokens=240_000,
            reached=False,
            authority=CompactionAuthority.FROZEN_RUN,
        ),
        freshness=ProjectionFreshness.CURRENT,
    )
    repository = _Repository()
    transaction = ContextProjectionTransaction(repository, now=lambda: NOW)

    first = await transaction.append_and_project(
        scope=scope,
        source=source,
        payloads=(
            WindowOpenedV1(
                model_identity_digest="e" * 64,
                context_window_tokens=300_000,
                compaction_enabled=True,
                compaction_threshold_tokens=240_000,
                compaction_authority="frozen_run",
            ),
            RequestPreparedV1(
                provider_call=provider_call,
                measurement=measurement,
            ),
            RequestDispatchedV1(provider_call_id=provider_call.provider_call_id),
        ),
        origin_run_id="run-1",
        active_run_id="run-1",
    )
    assert first.evidence_seq == "3"

    opened_payload = WindowOpenedV1(
        model_identity_digest="f" * 64,
        context_window_tokens=300_000,
        compaction_enabled=False,
    ).model_dump(mode="json", exclude_none=True)
    for index in range(1500):
        evidence_seq = repository.evidence_high_watermark + 1
        repository.evidence.append(
            ContextEvidenceRecord(
                scope=scope,
                evidence_seq=evidence_seq,
                subject=subject_ref,
                context_window_generation=GENERATION_ID,
                event_type="context.window.opened.v1",
                origin_run_id="run-1",
                provider_call_id=None,
                checkpoint_id=None,
                idempotency_key=f"{index + 1:064x}",
                payload_digest="d" * 64,
                payload=dict(opened_payload),
                occurred_at=NOW,
            )
        )
        repository.evidence_high_watermark = evidence_seq

    settled = await transaction.append_and_project(
        scope=scope,
        source=source.model_copy(update={"phase": ProjectionPhase.SETTLED}),
        payloads=(
            ProviderObservedV1(
                provider_call_id=provider_call.provider_call_id,
                input_tokens=103,
            ),
        ),
        origin_run_id="run-1",
        active_run_id=None,
    )

    assert settled.evidence_seq == "1504"
    assert settled.last_provider_observation is not None
    assert settled.last_provider_observation.input_tokens == 103
    assert repository.thread_page_calls == 0
    assert repository.subject_page_calls == []
    assert repository.subject_page_sizes == []
    assert repository.provider_page_calls == [
        (subject_ref, provider_call.provider_call_id),
        (subject_ref, provider_call.provider_call_id),
    ]
    assert repository.provider_page_sizes == [2, 3]


@pytest.mark.asyncio
async def test_repair_rebuilds_an_empty_settled_task_from_window_evidence() -> None:
    scope = ContextEvidenceScope(
        project_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        owner_user_id="55555555-5555-4555-8555-555555555555",
        thread_id=THREAD_ID,
    )
    subject = ContextSubject.subagent_task(
        thread_id=THREAD_ID,
        execution_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
    )
    source = ContextProjectionSource(
        subject=subject,
        phase=ProjectionPhase.SETTLED,
        generation=ContextWindowGeneration(generation_id=GENERATION_ID),
        checkpoint_id=None,
        model=ContextModelProjection(
            identity_digest="e" * 64,
            context_window_tokens=300_000,
        ),
        measurement=FinalRequestMeasurement(
            request_fingerprint="f" * 64,
            adapter_revision="empty-context-v1",
            contributions=(),
        ),
        current_provider_call_id=None,
        compaction=CompactionProjection(
            enabled=True,
            threshold_tokens=240_000,
            reached=False,
            authority=CompactionAuthority.FROZEN_RUN,
        ),
        freshness=ProjectionFreshness.CURRENT,
    )
    repository = _Repository()
    transaction = ContextProjectionTransaction(repository, now=lambda: NOW)
    await transaction.append_and_project(
        scope=scope,
        source=source,
        payloads=(
            WindowOpenedV1(
                model_identity_digest="e" * 64,
                context_window_tokens=300_000,
                compaction_enabled=True,
                compaction_threshold_tokens=240_000,
                compaction_authority="frozen_run",
            ),
        ),
        origin_run_id="run-1",
        active_run_id=None,
    )

    repaired = await rebuild_context_projection_head(
        repository,
        scope=scope,
        subject=ContextSubjectRef.subagent_task("66666666-6666-4666-8666-666666666666"),
        discard_existing_head=True,
    )

    assert repaired.phase is ProjectionPhase.SETTLED
    assert repaired.basis.value == "empty"
    assert repaired.lanes == ()
    assert repaired.totals.projected_tokens == 0
    assert repaired.compaction.enabled is True
    assert repaired.compaction.threshold_tokens == 240_000


@pytest.mark.asyncio
async def test_repair_rebuilds_settled_task_after_ephemeral_compaction() -> None:
    scope = ContextEvidenceScope(
        project_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        owner_user_id="55555555-5555-4555-8555-555555555555",
        thread_id=THREAD_ID,
    )
    subject = ContextSubject.subagent_task(
        thread_id=THREAD_ID,
        execution_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
    )
    source_generation = ContextWindowGeneration(generation_id=GENERATION_ID)
    result_generation = ContextWindowGeneration(
        generation_id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
    )
    measurement = FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="test-adapter-v1",
        contributions=(
            ContextContribution(
                contribution_id="1" * 64,
                source_identity_digest="2" * 64,
                lane=ContextLane.TOOL_DEFINITIONS,
                model_visible_bytes=40,
                token_estimate=TokenEstimate.exact(10),
            ),
            ContextContribution(
                contribution_id="3" * 64,
                source_identity_digest="4" * 64,
                lane=ContextLane.CONVERSATION,
                model_visible_bytes=360,
                token_estimate=TokenEstimate.exact(90),
            ),
        ),
    )
    provider_call = ProviderCallIdentity.derive(
        subject=subject,
        generation=source_generation,
        source_checkpoint_id="task-state:source",
        graph_step="subagent:model",
        model_call_ordinal=0,
        request_fingerprint=measurement.request_fingerprint,
    )
    repository = _Repository()
    transaction = ContextProjectionTransaction(repository, now=lambda: NOW)
    await transaction.append_and_project(
        scope=scope,
        source=ContextProjectionSource(
            subject=subject,
            phase=ProjectionPhase.ACTIVE,
            generation=source_generation,
            checkpoint_id=None,
            model=ContextModelProjection(
                identity_digest="e" * 64,
                context_window_tokens=300_000,
            ),
            measurement=measurement,
            current_provider_call_id=provider_call.provider_call_id,
            compaction=CompactionProjection(
                enabled=True,
                threshold_tokens=240_000,
                reached=False,
                authority=CompactionAuthority.FROZEN_RUN,
            ),
            freshness=ProjectionFreshness.CURRENT,
        ),
        payloads=(
            WindowOpenedV1(
                model_identity_digest="e" * 64,
                context_window_tokens=300_000,
                compaction_enabled=True,
                compaction_threshold_tokens=240_000,
                compaction_authority="frozen_run",
            ),
            RequestPreparedV1(
                provider_call=provider_call,
                measurement=measurement,
            ),
            RequestDispatchedV1(provider_call_id=provider_call.provider_call_id),
            ProviderObservedV1(
                provider_call_id=provider_call.provider_call_id,
                input_tokens=100,
            ),
        ),
        origin_run_id="run-1",
        active_run_id="run-1",
    )
    result_measurement = FinalRequestMeasurement(
        request_fingerprint="6" * 64,
        adapter_revision="subagent-compaction-v1",
        contributions=(
            measurement.contributions[0],
            ContextContribution(
                contribution_id="7" * 64,
                source_identity_digest="8" * 64,
                lane=ContextLane.SUMMARIZED_CONVERSATION,
                model_visible_bytes=0,
                token_estimate=TokenEstimate.exact(20),
            ),
            ContextContribution(
                contribution_id="9" * 64,
                source_identity_digest="0" * 64,
                lane=ContextLane.CONVERSATION,
                model_visible_bytes=0,
                token_estimate=TokenEstimate.exact(30),
            ),
        ),
    )
    await transaction.append_and_project(
        scope=scope,
        source=ContextProjectionSource(
            subject=subject,
            phase=ProjectionPhase.ACTIVE,
            generation=result_generation,
            checkpoint_id=None,
            model=ContextModelProjection(
                identity_digest="e" * 64,
                context_window_tokens=300_000,
            ),
            measurement=result_measurement,
            current_provider_call_id=None,
            compaction=CompactionProjection(
                enabled=True,
                threshold_tokens=240_000,
                reached=False,
                authority=CompactionAuthority.FROZEN_RUN,
            ),
            freshness=ProjectionFreshness.STALE,
        ),
        payloads=(
            WindowOpenedV1(
                model_identity_digest="e" * 64,
                context_window_tokens=300_000,
                compaction_enabled=True,
                compaction_threshold_tokens=240_000,
                compaction_authority="frozen_run",
            ),
            CompactionCommittedV1(
                receipt_id="5" * 64,
                source_state_digest="a" * 64,
                result_state_digest="6" * 64,
                source_generation=source_generation,
                result_generation=result_generation,
                source_tokens=90,
                result_tokens=50,
                summary_tokens=20,
                summary_digest="8" * 64,
            ),
        ),
        origin_run_id="run-1",
        active_run_id="run-1",
    )

    repaired = await rebuild_context_projection_head(
        repository,
        scope=scope,
        subject=ContextSubjectRef.subagent_task("66666666-6666-4666-8666-666666666666"),
        discard_existing_head=True,
    )

    assert repaired.context_window_generation == result_generation.generation_id
    assert repaired.phase is ProjectionPhase.SETTLED
    assert repaired.freshness is ProjectionFreshness.STALE
    assert repaired.checkpoint_id is None
    assert repaired.last_provider_observation is None
    assert [(lane.lane, lane.projected_tokens) for lane in repaired.lanes] == [
        (ContextLane.TOOL_DEFINITIONS, 10),
        (ContextLane.SUMMARIZED_CONVERSATION, 20),
        (ContextLane.CONVERSATION, 30),
    ]
    assert repaired.totals.projected_tokens == 60
