from __future__ import annotations

import uuid
from datetime import UTC, datetime

from langchain_core.messages import HumanMessage

from app.private_work.context_replacement import (
    bootstrap_checkpoint_projection_snapshot,
    branch_projection_source,
    checkpoint_snapshot_from_evidence,
    compaction_checkpoint_receipt,
    deterministic_generation_id,
    history_digest,
)
from deerflow.persistence.context_evidence import (
    ContextEvidenceRecord,
    ContextEvidenceScope,
    ContextSubjectRef,
)
from deerflow.runtime.context_evidence import (
    CheckpointLinkedV1,
    CompactionProjection,
    ContextCheckpointEstimator,
    ContextCheckpointProjectionSnapshot,
    ContextContribution,
    ContextCoverage,
    ContextLane,
    ContextModelProjection,
    ContextProjector,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderCallIdentity,
    RequestPreparedV1,
    TokenEstimate,
    TokenEstimateKind,
    WindowOpenedV1,
)

THREAD_ID = "11111111-1111-4111-8111-111111111111"
TARGET_THREAD_ID = "22222222-2222-4222-8222-222222222222"
SOURCE_GENERATION = ContextWindowGeneration(
    generation_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
)


def _contribution(
    lane: ContextLane,
    *,
    identity: str,
    tokens: int,
) -> ContextContribution:
    return ContextContribution(
        contribution_id=identity * 64,
        source_identity_digest=chr(ord(identity) + 1) * 64,
        lane=lane,
        model_visible_bytes=tokens * 4,
        token_estimate=TokenEstimate.bounded(
            projected_tokens=tokens,
            lower_bound_tokens=tokens,
            safety_upper_bound_tokens=tokens + 5,
        ),
    )


def _snapshot() -> ContextCheckpointProjectionSnapshot:
    return ContextCheckpointProjectionSnapshot(
        generation=SOURCE_GENERATION,
        model=ContextModelProjection(
            identity_digest="f" * 64,
            context_window_tokens=300_000,
        ),
        measurement=FinalRequestMeasurement(
            request_fingerprint="a" * 64,
            adapter_revision="test-v1",
            contributions=(
                _contribution(ContextLane.SYSTEM_PROMPT, identity="b", tokens=100),
                _contribution(ContextLane.CONVERSATION, identity="c", tokens=900),
                _contribution(ContextLane.PROVIDER_OVERHEAD, identity="d", tokens=20),
            ),
        ),
        compaction=CompactionProjection(enabled=False, reached=False),
        estimator=ContextCheckpointEstimator(
            error_allowance_ratio=0.2,
            provider_fixed_overhead_tokens=10,
            provider_per_message_overhead_tokens=2,
            provider_per_tool_overhead_tokens=3,
            fixed_message_count=1,
            tool_count=2,
        ),
    )


def test_provider_free_checkpoint_bootstrap_is_a_partial_content_only_projection() -> None:
    generation = ContextWindowGeneration(
        generation_id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
    )

    snapshot = bootstrap_checkpoint_projection_snapshot(
        generation=generation,
        checkpoint_values={
            "messages": [
                HumanMessage(
                    id="provider-free-human",
                    content="Only persisted history is measurable before Provider shaping",
                )
            ],
        },
    )

    by_lane = {contribution.lane: contribution for contribution in snapshot.measurement.contributions}
    assert snapshot.generation == generation
    assert snapshot.model.context_window_tokens is None
    assert by_lane[ContextLane.CONVERSATION].token_estimate.projected_tokens > 0
    assert by_lane[ContextLane.SYSTEM_PROMPT].token_estimate.kind is TokenEstimateKind.UNMEASURED
    assert snapshot.measurement.safety_upper_bound_tokens is None
    assert snapshot.provider_call_id is None
    assert snapshot.compaction.enabled is False
    assert "Only persisted history" not in repr(snapshot.to_safe_mapping())
    head = ContextProjector.rebuild(
        source=branch_projection_source(
            snapshot,
            target_thread_id=TARGET_THREAD_ID,
            result_checkpoint_id="provider-free-checkpoint",
            generation=generation,
        ),
        evidence=(),
        projection_seq="1",
        projector_revision="context-projector-v1",
        as_of=datetime(2026, 8, 27, tzinfo=UTC),
    )
    assert head.coverage is ContextCoverage.PARTIAL
    assert head.totals.projected_tokens > 0
    assert head.totals.context_window_tokens is None


def test_empty_provider_free_checkpoint_remains_zero_but_partial() -> None:
    snapshot = bootstrap_checkpoint_projection_snapshot(
        generation=ContextWindowGeneration(
            generation_id=uuid.UUID(
                "88888888-8888-4888-8888-888888888888",
            ),
        ),
        checkpoint_values={},
    )

    assert snapshot.measurement.projected_tokens == 0
    assert snapshot.measurement.lower_bound_tokens == 0
    assert snapshot.measurement.safety_upper_bound_tokens is None
    assert len(snapshot.measurement.contributions) == 1
    assert snapshot.measurement.contributions[0].token_estimate.kind is TokenEstimateKind.UNMEASURED


def test_compaction_receipt_remeasures_only_retained_context_without_content() -> None:
    result_generation = ContextWindowGeneration(
        generation_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
    )
    source = _snapshot().model_copy(
        update={
            "provider_call_id": "e" * 64,
            "provider_subject": ContextSubject.lead_thread(thread_id=THREAD_ID),
            "origin_run_id": "run-1",
            "provider_response_message_start": 0,
            "provider_response_message_count": 1,
            "provider_response_digest": "f" * 64,
        },
    )
    receipt = compaction_checkpoint_receipt(
        source,
        source_checkpoint_id="checkpoint-source",
        checkpoint_values={
            "messages": [HumanMessage(id="human-tail", content="retained tail")],
            "summary_text": "compact continuity",
        },
        result_generation=result_generation,
    )

    lanes = {item.lane: item for item in receipt.projection_snapshot.measurement.contributions}
    assert lanes[ContextLane.SYSTEM_PROMPT].token_estimate.projected_tokens == 100
    assert ContextLane.CONVERSATION in lanes
    assert ContextLane.SUMMARIZED_CONVERSATION in lanes
    assert receipt.result_tokens < receipt.source_tokens
    assert receipt.result_generation == result_generation
    assert receipt.projection_snapshot.provider_call_id is None
    assert receipt.projection_snapshot.provider_response_digest is None
    rendered = repr(receipt.to_safe_mapping())
    assert "retained tail" not in rendered
    assert "compact continuity" not in rendered


def test_branch_projection_uses_new_subject_generation_and_no_provider_observation() -> None:
    generation_id = deterministic_generation_id(
        thread_id=TARGET_THREAD_ID,
        operation="branch",
        source_checkpoint_id="checkpoint-source",
        result_checkpoint_id=TARGET_THREAD_ID,
    )
    generation = ContextWindowGeneration(generation_id=generation_id)

    source = branch_projection_source(
        _snapshot(),
        target_thread_id=TARGET_THREAD_ID,
        result_checkpoint_id="checkpoint-target",
        generation=generation,
    )

    assert source.subject == ContextSubject.lead_thread(thread_id=TARGET_THREAD_ID)
    assert source.generation == generation
    assert source.checkpoint_id == "checkpoint-target"
    assert source.current_provider_call_id is None
    assert history_digest(
        source_thread_id=THREAD_ID,
        source_checkpoint_id="checkpoint-source",
        result_checkpoint_id="checkpoint-target",
        checkpoint_values={"messages": []},
    ) == history_digest(
        source_thread_id=THREAD_ID,
        source_checkpoint_id="checkpoint-source",
        result_checkpoint_id="checkpoint-target",
        checkpoint_values={"messages": []},
    )


def test_checkpoint_snapshot_can_be_recovered_from_linked_source_evidence() -> None:
    subject = ContextSubject.lead_thread(thread_id=THREAD_ID)
    measurement = _snapshot().measurement
    provider_call = ProviderCallIdentity.derive(
        subject=subject,
        generation=SOURCE_GENERATION,
        source_checkpoint_id="checkpoint-before",
        graph_step="model",
        model_call_ordinal=0,
        request_fingerprint=measurement.request_fingerprint,
    )
    scope = ContextEvidenceScope(
        project_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        owner_user_id="66666666-6666-4666-8666-666666666666",
        thread_id=THREAD_ID,
    )
    now = datetime(2026, 8, 27, tzinfo=UTC)

    def record(
        seq: int,
        payload: object,
        *,
        provider_call_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> ContextEvidenceRecord:
        mapping = payload.model_dump(mode="json", exclude_none=True)
        return ContextEvidenceRecord(
            scope=scope,
            evidence_seq=seq,
            subject=ContextSubjectRef.lead_thread(THREAD_ID),
            context_window_generation=uuid.UUID(SOURCE_GENERATION.generation_id),
            event_type=mapping["event_type"],
            origin_run_id="run-1",
            provider_call_id=provider_call_id,
            checkpoint_id=checkpoint_id,
            idempotency_key=str(seq) * 64,
            payload_digest="e" * 64,
            payload=mapping,
            occurred_at=now,
        )

    records = (
        record(
            1,
            WindowOpenedV1(
                model_identity_digest="f" * 64,
                context_window_tokens=300_000,
                compaction_enabled=True,
                compaction_threshold_tokens=240_000,
                compaction_authority="frozen_run",
            ),
        ),
        record(
            2,
            RequestPreparedV1(
                provider_call=provider_call,
                measurement=measurement,
            ),
            provider_call_id=provider_call.provider_call_id,
        ),
        record(
            3,
            CheckpointLinkedV1(
                provider_call_id=provider_call.provider_call_id,
                checkpoint_id="checkpoint-result",
            ),
            provider_call_id=provider_call.provider_call_id,
            checkpoint_id="checkpoint-result",
        ),
    )

    recovered = checkpoint_snapshot_from_evidence(
        records,
        subject=ContextSubjectRef.lead_thread(THREAD_ID),
        checkpoint_id="checkpoint-result",
        estimator=_snapshot().estimator,
    )

    assert recovered.measurement == measurement
    assert recovered.model.identity_digest == "f" * 64
    assert recovered.generation == SOURCE_GENERATION
