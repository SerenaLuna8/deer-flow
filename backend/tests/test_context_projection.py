from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from deerflow.runtime.context_evidence import (
    CompactionAuthority,
    CompactionProjection,
    ContextContribution,
    ContextCoverage,
    ContextEvidence,
    ContextLane,
    ContextModelProjection,
    ContextNoticeCode,
    ContextProjectionHead,
    ContextProjectionSource,
    ContextProjector,
    ContextRebaseReason,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProjectionBasis,
    ProjectionFreshness,
    ProjectionPhase,
    ProviderCallIdentity,
    ProviderObservedV1,
    RequestDispatchedV1,
    RequestPreparedV1,
    TokenEstimate,
    VisualCostStrategy,
    VisualDetail,
    VisualMeasurementMetadata,
    WindowRebasedV1,
)
from deerflow.runtime.context_evidence.projector import ContextEvidenceSequenceError, ContextProjectionRegressionError

THREAD_ID = "11111111-1111-4111-8111-111111111111"
GENERATION = ContextWindowGeneration(generation_id=UUID("44444444-4444-4444-8444-444444444444"))
NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _contribution(
    *,
    lane: ContextLane,
    identity: str,
    estimate: TokenEstimate,
    visual: VisualMeasurementMetadata | None = None,
) -> ContextContribution:
    source_identity = {"1": "a", "2": "b", "3": "c", "4": "d"}[identity]
    return ContextContribution(
        contribution_id=identity * 64,
        source_identity_digest=source_identity * 64,
        lane=lane,
        model_visible_bytes=estimate.projected_tokens * 4,
        token_estimate=estimate,
        visual=visual,
    )


def _request_lifecycle(
    measurement: FinalRequestMeasurement,
) -> tuple[ProviderCallIdentity, tuple[ContextEvidence, ...]]:
    subject = ContextSubject.lead_thread(thread_id=THREAD_ID)
    call = ProviderCallIdentity.derive(
        subject=subject,
        generation=GENERATION,
        source_checkpoint_id="checkpoint-7",
        graph_step="lead:model",
        model_call_ordinal=1,
        request_fingerprint=measurement.request_fingerprint,
    )
    common = {
        "subject": subject,
        "generation": GENERATION,
        "origin_run_id": "run-7",
    }
    return call, (
        ContextEvidence(
            evidence_seq="1",
            occurred_at=NOW,
            payload=RequestPreparedV1(
                provider_call=call,
                measurement=measurement,
            ),
            **common,
        ),
        ContextEvidence(
            evidence_seq="2",
            occurred_at=NOW + timedelta(seconds=1),
            payload=RequestDispatchedV1(provider_call_id=call.provider_call_id),
            **common,
        ),
        ContextEvidence(
            evidence_seq="3",
            occurred_at=NOW + timedelta(seconds=2),
            payload=ProviderObservedV1(
                provider_call_id=call.provider_call_id,
                input_tokens=97,
            ),
            **common,
        ),
    )


def _source(
    measurement: FinalRequestMeasurement,
    *,
    provider_call_id: str | None = None,
    capacity: int | None = 300,
    freshness: ProjectionFreshness = ProjectionFreshness.CURRENT,
) -> ContextProjectionSource:
    return ContextProjectionSource(
        subject=ContextSubject.lead_thread(thread_id=THREAD_ID),
        phase=ProjectionPhase.ACTIVE,
        generation=GENERATION,
        checkpoint_id="checkpoint-7",
        model=ContextModelProjection(
            identity_digest="f" * 64,
            context_window_tokens=capacity,
        ),
        measurement=measurement,
        current_provider_call_id=provider_call_id,
        compaction=CompactionProjection(
            enabled=True,
            threshold_tokens=240,
            reached=False,
            authority=CompactionAuthority.FROZEN_RUN,
        ),
        freshness=freshness,
    )


def test_rebuild_projection_keeps_lane_total_separate_from_provider_observation() -> None:
    measurement = FinalRequestMeasurement(
        request_fingerprint="c" * 64,
        adapter_revision="openai-cost-v1",
        contributions=(
            _contribution(
                lane=ContextLane.SYSTEM_PROMPT,
                identity="1",
                estimate=TokenEstimate.exact(20),
            ),
            _contribution(
                lane=ContextLane.CONVERSATION,
                identity="2",
                estimate=TokenEstimate.bounded(
                    projected_tokens=80,
                    lower_bound_tokens=75,
                    safety_upper_bound_tokens=90,
                ),
            ),
        ),
    )
    call, evidence = _request_lifecycle(measurement)

    head = ContextProjector.rebuild(
        source=_source(measurement, provider_call_id=call.provider_call_id),
        evidence=evidence,
        projection_seq="5",
        projector_revision="context-projector-v1",
        as_of=NOW + timedelta(seconds=3),
    )

    assert head.basis is ProjectionBasis.HYBRID
    assert head.coverage is ContextCoverage.COMPLETE
    assert head.totals.projected_tokens == 100
    assert head.totals.lower_bound_tokens == 95
    assert head.totals.safety_upper_bound_tokens == 110
    assert [lane.lane for lane in head.lanes] == [
        ContextLane.SYSTEM_PROMPT,
        ContextLane.CONVERSATION,
    ]
    assert sum(lane.projected_tokens for lane in head.lanes) == 100
    assert head.last_provider_observation is not None
    assert head.last_provider_observation.input_tokens == 97
    assert head.totals.projected_tokens != head.last_provider_observation.input_tokens

    safe = head.to_safe_mapping()
    assert safe["projection_seq"] == "5"
    assert safe["evidence_seq"] == "3"
    assert safe["totals"]["progress_percent"] == 33.3
    assert ContextProjectionHead.from_safe_mapping(safe) == head


def test_unmeasured_visual_cost_produces_a_visible_partial_lower_bound() -> None:
    measurement = FinalRequestMeasurement(
        request_fingerprint="d" * 64,
        adapter_revision="unknown-vision-v1",
        contributions=(
            _contribution(
                lane=ContextLane.CONVERSATION,
                identity="3",
                estimate=TokenEstimate.exact(134_100),
            ),
            _contribution(
                lane=ContextLane.VISUAL_MEDIA,
                identity="4",
                estimate=TokenEstimate.unmeasured(item_count=2),
                visual=VisualMeasurementMetadata(
                    image_digest="e" * 64,
                    mime_type="image/png",
                    detail=VisualDetail.AUTO,
                    strategy=VisualCostStrategy.UNMEASURED,
                ),
            ),
        ),
    )

    head = ContextProjector.rebuild(
        source=_source(measurement, capacity=300_000),
        evidence=(),
        projection_seq="1",
        projector_revision="context-projector-v1",
        as_of=NOW,
    )

    assert head.basis is ProjectionBasis.ESTIMATED
    assert head.coverage is ContextCoverage.PARTIAL
    assert head.totals.projected_tokens == 134_100
    assert head.totals.lower_bound_tokens == 134_100
    assert head.totals.safety_upper_bound_tokens is None
    assert head.totals.progress_percent == 44.7
    assert [notice.model_dump(mode="json", exclude_none=True) for notice in head.notices] == [
        {
            "code": ContextNoticeCode.VISUAL_COST_UNMEASURED,
            "count": 2,
            "lane": ContextLane.VISUAL_MEDIA,
        }
    ]


def test_stale_projection_with_unknown_capacity_remains_serializable() -> None:
    measurement = FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="text-v1",
        contributions=(),
    )

    head = ContextProjector.rebuild(
        source=_source(
            measurement,
            capacity=None,
            freshness=ProjectionFreshness.STALE,
        ),
        evidence=(),
        projection_seq="8",
        projector_revision="context-projector-v1",
        as_of=NOW,
    )

    assert head.basis is ProjectionBasis.EMPTY
    assert head.freshness is ProjectionFreshness.STALE
    assert head.totals.context_window_tokens is None
    assert head.totals.remaining_tokens is None
    assert head.totals.progress_percent is None
    assert [notice.code for notice in head.notices] == [
        ContextNoticeCode.PROJECTION_STALE,
        ContextNoticeCode.CAPACITY_UNKNOWN,
    ]


def test_over_capacity_projection_clamps_display_indicators_without_hiding_total() -> None:
    measurement = FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="text-v1",
        contributions=(
            _contribution(
                lane=ContextLane.CONVERSATION,
                identity="1",
                estimate=TokenEstimate.exact(350),
            ),
        ),
    )

    head = ContextProjector.rebuild(
        source=_source(measurement, capacity=300),
        evidence=(),
        projection_seq="1",
        projector_revision="context-projector-v1",
        as_of=NOW,
    )

    assert head.totals.projected_tokens == 350
    assert head.totals.remaining_tokens == 0
    assert head.totals.progress_percent == 100.0


def test_rebuild_rejects_out_of_order_or_impossible_provider_lifecycle() -> None:
    measurement = FinalRequestMeasurement(
        request_fingerprint="b" * 64,
        adapter_revision="text-v1",
        contributions=(),
    )
    _, evidence = _request_lifecycle(measurement)

    with pytest.raises(ContextEvidenceSequenceError, match="strictly increasing"):
        ContextProjector.rebuild(
            source=_source(measurement),
            evidence=(evidence[1], evidence[0]),
            projection_seq="1",
            projector_revision="context-projector-v1",
            as_of=NOW,
        )

    with pytest.raises(ContextEvidenceSequenceError, match="before preparation"):
        ContextProjector.rebuild(
            source=_source(measurement),
            evidence=(evidence[1],),
            projection_seq="1",
            projector_revision="context-projector-v1",
            as_of=NOW,
        )


def test_projection_head_rejects_unknown_public_fields() -> None:
    measurement = FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="text-v1",
        contributions=(),
    )
    head = ContextProjector.rebuild(
        source=_source(measurement),
        evidence=(),
        projection_seq="1",
        projector_revision="context-projector-v1",
        as_of=NOW,
    )
    unsafe = {**head.to_safe_mapping(), "prompt": "secret text"}

    with pytest.raises(ValidationError):
        ContextProjectionHead.from_safe_mapping(unsafe)


def test_projection_reducer_advances_heads_monotonically() -> None:
    measurement = FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="text-v1",
        contributions=(),
    )
    source = _source(measurement)
    current = ContextProjector.rebuild(
        source=source,
        evidence=(),
        projection_seq="5",
        projector_revision="context-projector-v2",
        as_of=NOW,
    )

    advanced = ContextProjector.reduce(
        current=current,
        source=source,
        evidence=(),
        projection_seq="6",
        projector_revision="context-projector-v2",
        as_of=NOW + timedelta(seconds=1),
    )
    assert advanced.projection_seq == "6"
    assert advanced.as_of == NOW + timedelta(seconds=1)

    with pytest.raises(ContextProjectionRegressionError, match="projection_seq"):
        ContextProjector.reduce(
            current=current,
            source=source,
            evidence=(),
            projection_seq="5",
            projector_revision="context-projector-v2",
            as_of=NOW,
        )

    with pytest.raises(ContextProjectionRegressionError, match="projector revision"):
        ContextProjector.reduce(
            current=current,
            source=source,
            evidence=(),
            projection_seq="6",
            projector_revision="context-projector-v1",
            as_of=NOW,
        )


def test_reducer_retains_previous_observation_only_within_the_same_generation() -> None:
    first_measurement = FinalRequestMeasurement(
        request_fingerprint="c" * 64,
        adapter_revision="text-v1",
        contributions=(
            _contribution(
                lane=ContextLane.CONVERSATION,
                identity="1",
                estimate=TokenEstimate.exact(100),
            ),
        ),
    )
    first_call, first_evidence = _request_lifecycle(first_measurement)
    current = ContextProjector.rebuild(
        source=_source(
            first_measurement,
            provider_call_id=first_call.provider_call_id,
        ),
        evidence=first_evidence,
        projection_seq="5",
        projector_revision="context-projector-v1",
        as_of=NOW + timedelta(seconds=3),
    )
    next_measurement = FinalRequestMeasurement(
        request_fingerprint="d" * 64,
        adapter_revision="text-v1",
        contributions=(
            _contribution(
                lane=ContextLane.CONVERSATION,
                identity="2",
                estimate=TokenEstimate.exact(120),
            ),
        ),
    )
    next_call = ProviderCallIdentity.derive(
        subject=current.subject,
        generation=GENERATION,
        source_checkpoint_id="checkpoint-7",
        graph_step="lead:model",
        model_call_ordinal=2,
        request_fingerprint=next_measurement.request_fingerprint,
    )
    next_evidence = (
        ContextEvidence(
            evidence_seq="4",
            subject=current.subject,
            generation=GENERATION,
            origin_run_id="run-7",
            occurred_at=NOW + timedelta(seconds=4),
            payload=RequestPreparedV1(
                provider_call=next_call,
                measurement=next_measurement,
            ),
        ),
        ContextEvidence(
            evidence_seq="5",
            subject=current.subject,
            generation=GENERATION,
            origin_run_id="run-7",
            occurred_at=NOW + timedelta(seconds=5),
            payload=RequestDispatchedV1(
                provider_call_id=next_call.provider_call_id,
            ),
        ),
    )

    advanced = ContextProjector.reduce(
        current=current,
        source=_source(
            next_measurement,
            provider_call_id=next_call.provider_call_id,
        ),
        evidence=next_evidence,
        evidence_high_watermark="5",
        projection_seq="6",
        projector_revision="context-projector-v1",
        as_of=NOW + timedelta(seconds=6),
    )

    assert advanced.last_provider_observation == current.last_provider_observation
    assert advanced.basis is ProjectionBasis.HYBRID
    assert advanced.basis is not ProjectionBasis.PROVIDER_CONFIRMED

    next_generation = ContextWindowGeneration(
        generation_id=UUID("77777777-7777-4777-8777-777777777777"),
    )
    rebased_source = _source(next_measurement).model_copy(
        update={
            "generation": next_generation,
            "checkpoint_id": "checkpoint-new",
            "current_provider_call_id": None,
        }
    )
    rebased = ContextProjector.reduce(
        current=advanced,
        source=rebased_source,
        evidence=(
            ContextEvidence(
                evidence_seq="6",
                subject=current.subject,
                generation=next_generation,
                origin_run_id="run-7",
                occurred_at=NOW + timedelta(seconds=7),
                payload=WindowRebasedV1(
                    reason=ContextRebaseReason.HISTORY_REPLACEMENT,
                    source_checkpoint_id="checkpoint-7",
                    result_checkpoint_id="checkpoint-new",
                    result_generation=next_generation,
                    history_digest="e" * 64,
                ),
            ),
        ),
        evidence_high_watermark="6",
        projection_seq="7",
        projector_revision="context-projector-v1",
        as_of=NOW + timedelta(seconds=7),
    )

    assert rebased.last_provider_observation is None
    assert rebased.basis is ProjectionBasis.ESTIMATED


def test_thread_wide_evidence_sequence_does_not_combine_subagent_context() -> None:
    lead_measurement = FinalRequestMeasurement(
        request_fingerprint="c" * 64,
        adapter_revision="text-v1",
        contributions=(
            _contribution(
                lane=ContextLane.CONVERSATION,
                identity="1",
                estimate=TokenEstimate.exact(100),
            ),
        ),
    )
    lead_call, lead_evidence = _request_lifecycle(lead_measurement)
    task_subject = ContextSubject.subagent_task(
        thread_id=THREAD_ID,
        execution_id=UUID("22222222-2222-4222-8222-222222222222"),
    )
    task_generation = ContextWindowGeneration(generation_id=UUID("55555555-5555-4555-8555-555555555555"))
    task_measurement = FinalRequestMeasurement(
        request_fingerprint="d" * 64,
        adapter_revision="text-v1",
        contributions=(),
    )
    task_call = ProviderCallIdentity.derive(
        subject=task_subject,
        generation=task_generation,
        source_checkpoint_id="task-state-1",
        graph_step="subagent:model",
        model_call_ordinal=1,
        request_fingerprint=task_measurement.request_fingerprint,
    )
    task_common = {
        "subject": task_subject,
        "generation": task_generation,
        "origin_run_id": "run-7",
    }
    interleaved = lead_evidence + (
        ContextEvidence(
            evidence_seq="4",
            occurred_at=NOW + timedelta(seconds=4),
            payload=RequestPreparedV1(
                provider_call=task_call,
                measurement=task_measurement,
            ),
            **task_common,
        ),
        ContextEvidence(
            evidence_seq="5",
            occurred_at=NOW + timedelta(seconds=5),
            payload=RequestDispatchedV1(provider_call_id=task_call.provider_call_id),
            **task_common,
        ),
        ContextEvidence(
            evidence_seq="6",
            occurred_at=NOW + timedelta(seconds=6),
            payload=ProviderObservedV1(
                provider_call_id=task_call.provider_call_id,
                input_tokens=999,
            ),
            **task_common,
        ),
    )

    head = ContextProjector.rebuild(
        source=_source(
            lead_measurement,
            provider_call_id=lead_call.provider_call_id,
        ),
        evidence=interleaved,
        projection_seq="9",
        projector_revision="context-projector-v1",
        as_of=NOW + timedelta(seconds=7),
    )

    assert head.evidence_seq == "6"
    assert head.totals.projected_tokens == 100
    assert head.last_provider_observation is not None
    assert head.last_provider_observation.input_tokens == 97


def test_subject_only_projection_accepts_a_valid_thread_high_watermark() -> None:
    measurement = FinalRequestMeasurement(
        request_fingerprint="c" * 64,
        adapter_revision="text-v1",
        contributions=(),
    )
    call, evidence = _request_lifecycle(measurement)

    head = ContextProjector.rebuild(
        source=_source(
            measurement,
            provider_call_id=call.provider_call_id,
        ),
        evidence=evidence,
        evidence_high_watermark="1503",
        projection_seq="10",
        projector_revision="context-projector-v1",
        as_of=NOW + timedelta(seconds=4),
    )

    assert head.evidence_seq == "1503"

    with pytest.raises(
        ContextEvidenceSequenceError,
        match="cannot precede loaded Evidence",
    ):
        ContextProjector.rebuild(
            source=_source(
                measurement,
                provider_call_id=call.provider_call_id,
            ),
            evidence=evidence,
            evidence_high_watermark="2",
            projection_seq="11",
            projector_revision="context-projector-v1",
            as_of=NOW + timedelta(seconds=5),
        )


def test_new_generation_does_not_inherit_old_provider_observation() -> None:
    old_measurement = FinalRequestMeasurement(
        request_fingerprint="c" * 64,
        adapter_revision="text-v1",
        contributions=(
            _contribution(
                lane=ContextLane.CONVERSATION,
                identity="1",
                estimate=TokenEstimate.exact(100),
            ),
        ),
    )
    _old_call, old_evidence = _request_lifecycle(old_measurement)
    new_generation = ContextWindowGeneration(generation_id=UUID("77777777-7777-4777-8777-777777777777"))
    new_source = _source(
        FinalRequestMeasurement(
            request_fingerprint="d" * 64,
            adapter_revision="text-v1",
            contributions=(),
        )
    ).model_copy(
        update={
            "generation": new_generation,
            "checkpoint_id": "checkpoint-new",
            "current_provider_call_id": None,
        }
    )

    head = ContextProjector.rebuild(
        source=new_source,
        evidence=old_evidence,
        projection_seq="7",
        projector_revision="context-projector-v1",
        as_of=NOW + timedelta(seconds=4),
    )

    assert head.context_window_generation == new_generation.generation_id
    assert head.basis is ProjectionBasis.EMPTY
    assert head.last_provider_observation is None
