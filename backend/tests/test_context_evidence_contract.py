from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from deerflow.runtime.context_evidence import (
    STABLE_CONTEXT_EVIDENCE_TYPES,
    STABLE_CONTEXT_LANES,
    CompactionCommittedV1,
    ContextContribution,
    ContextEvidence,
    ContextEvidenceType,
    ContextLane,
    ContextSubject,
    ContextSubjectKind,
    ContextTokenCostContractError,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderCallIdentity,
    RequestPreparedV1,
    TokenEstimate,
    TokenEstimateKind,
    VisualCostStrategy,
    VisualDetail,
    VisualMeasurementMetadata,
    VisualTokenCostContractError,
)

THREAD_ID = UUID("11111111-1111-4111-8111-111111111111")
EXECUTION_ID = UUID("22222222-2222-4222-8222-222222222222")
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
GENERATION_ID = UUID("44444444-4444-4444-8444-444444444444")


def test_context_subject_distinguishes_lead_from_each_subagent_execution() -> None:
    lead = ContextSubject.lead_thread(thread_id=THREAD_ID)
    first_task = ContextSubject.subagent_task(
        thread_id=THREAD_ID,
        execution_id=EXECUTION_ID,
    )
    second_task = ContextSubject.subagent_task(
        thread_id=THREAD_ID,
        execution_id=UUID("33333333-3333-4333-8333-333333333333"),
    )

    assert lead.kind is ContextSubjectKind.LEAD_THREAD
    assert lead.execution_id is None
    assert first_task.kind is ContextSubjectKind.SUBAGENT_TASK
    assert first_task != second_task
    assert first_task.to_safe_mapping() == {
        "kind": "subagent_task",
        "thread_id": str(THREAD_ID),
        "execution_id": str(EXECUTION_ID),
    }


def test_context_subject_rejects_cross_kind_execution_identifiers() -> None:
    with pytest.raises(ValidationError):
        ContextSubject.model_validate(
            {
                "kind": "lead_thread",
                "thread_id": str(THREAD_ID),
                "execution_id": str(EXECUTION_ID),
            }
        )

    with pytest.raises(ValidationError):
        ContextSubject.model_validate(
            {
                "kind": "subagent_task",
                "thread_id": str(THREAD_ID),
            }
        )


def test_context_contributions_use_the_closed_ordered_lane_vocabulary() -> None:
    assert tuple(lane.value for lane in STABLE_CONTEXT_LANES) == (
        "system_prompt",
        "agent_instructions",
        "tool_definitions",
        "skills",
        "mcp_dynamic_tools",
        "subagent_definitions",
        "summarized_conversation",
        "conversation",
        "visual_media",
        "provider_overhead",
    )

    contribution = ContextContribution(
        contribution_id=DIGEST_A,
        source_identity_digest=DIGEST_B,
        lane=ContextLane.SYSTEM_PROMPT,
        model_visible_bytes=80,
        token_estimate=TokenEstimate.bounded(
            projected_tokens=20,
            lower_bound_tokens=18,
            safety_upper_bound_tokens=23,
        ),
    )

    assert contribution.token_estimate.kind is TokenEstimateKind.BOUNDED
    assert contribution.to_safe_mapping() == {
        "contribution_id": DIGEST_A,
        "source_identity_digest": DIGEST_B,
        "lane": "system_prompt",
        "model_visible_bytes": 80,
        "token_estimate": {
            "kind": "bounded",
            "projected_tokens": 20,
            "lower_bound_tokens": 18,
            "safety_upper_bound_tokens": 23,
            "unmeasured_items": 0,
        },
    }


def test_final_request_measurement_rejects_duplicate_source_attribution() -> None:
    first = ContextContribution(
        contribution_id=DIGEST_A,
        source_identity_digest=DIGEST_B,
        lane=ContextLane.SKILLS,
        model_visible_bytes=40,
        token_estimate=TokenEstimate.exact(10),
    )
    duplicate = ContextContribution(
        contribution_id="c" * 64,
        source_identity_digest=DIGEST_B,
        lane=ContextLane.CONVERSATION,
        model_visible_bytes=40,
        token_estimate=TokenEstimate.exact(10),
    )

    with pytest.raises(ValidationError, match="source identity"):
        FinalRequestMeasurement(
            request_fingerprint="d" * 64,
            adapter_revision="openai-cost-v1",
            contributions=(first, duplicate),
        )


def test_visual_contribution_records_only_safe_metadata_and_bounded_cost() -> None:
    visual = VisualMeasurementMetadata(
        image_digest=DIGEST_A,
        mime_type="image/png",
        width=1024,
        height=768,
        detail=VisualDetail.HIGH,
        strategy=VisualCostStrategy.DIMENSION_DETAIL,
    )
    contribution = ContextContribution(
        contribution_id=DIGEST_B,
        source_identity_digest=DIGEST_A,
        lane=ContextLane.VISUAL_MEDIA,
        model_visible_bytes=0,
        token_estimate=TokenEstimate.bounded(
            projected_tokens=765,
            lower_bound_tokens=765,
            safety_upper_bound_tokens=800,
        ),
        visual=visual,
    )

    safe = contribution.to_safe_mapping()
    assert safe["visual"] == {
        "image_digest": DIGEST_A,
        "mime_type": "image/png",
        "width": 1024,
        "height": 768,
        "detail": "high",
        "strategy": "dimension_detail",
    }
    assert "data" not in safe
    assert "path" not in safe["visual"]


def test_unmeasured_visual_cost_is_an_explicit_lower_bound() -> None:
    estimate = TokenEstimate.unmeasured(item_count=2)

    assert estimate.kind is TokenEstimateKind.UNMEASURED
    assert estimate.projected_tokens == 0
    assert estimate.lower_bound_tokens == 0
    assert estimate.safety_upper_bound_tokens is None
    assert estimate.unmeasured_items == 2


def test_final_guard_can_require_a_complete_safety_upper_bound() -> None:
    visual = ContextContribution(
        contribution_id=DIGEST_A,
        source_identity_digest=DIGEST_B,
        lane=ContextLane.VISUAL_MEDIA,
        model_visible_bytes=0,
        token_estimate=TokenEstimate.unmeasured(item_count=2),
        visual=VisualMeasurementMetadata(
            image_digest="c" * 64,
            mime_type="image/png",
            detail=VisualDetail.AUTO,
            strategy=VisualCostStrategy.UNMEASURED,
        ),
    )
    measurement = FinalRequestMeasurement(
        request_fingerprint="d" * 64,
        adapter_revision="unknown-vision-v1",
        contributions=(visual,),
    )

    assert measurement.projected_tokens == 0
    assert measurement.safety_upper_bound_tokens is None
    with pytest.raises(
        VisualTokenCostContractError,
        match="VISUAL_TOKEN_UPPER_BOUND_UNAVAILABLE",
    ):
        measurement.require_safety_upper_bound()


def test_fixed_closure_sentinel_requires_a_complete_safety_upper_bound() -> None:
    fixed_closure = ContextContribution(
        contribution_id=DIGEST_A,
        source_identity_digest=DIGEST_B,
        lane=ContextLane.SYSTEM_PROMPT,
        model_visible_bytes=0,
        token_estimate=TokenEstimate.unmeasured(item_count=1),
    )
    measurement = FinalRequestMeasurement(
        request_fingerprint="d" * 64,
        adapter_revision="checkpoint-bootstrap-v1",
        contributions=(fixed_closure,),
    )

    assert measurement.safety_upper_bound_tokens is None
    with pytest.raises(
        ContextTokenCostContractError,
        match="CONTEXT_TOKEN_UPPER_BOUND_UNAVAILABLE",
    ) as caught:
        measurement.require_safety_upper_bound()
    assert caught.value.unmeasured_items == 1


def test_provider_call_identity_is_stable_and_content_free() -> None:
    identity = ProviderCallIdentity.derive(
        subject=ContextSubject.lead_thread(thread_id=THREAD_ID),
        generation=ContextWindowGeneration(generation_id=GENERATION_ID),
        source_checkpoint_id="checkpoint-7",
        graph_step="lead:model",
        model_call_ordinal=3,
        request_fingerprint="c" * 64,
    )

    assert identity.provider_call_id == "8a8481325085dc077995ebad38fb4b37a6d69b7ecd1425a9a9d7be04071c9cf8"
    assert identity.idempotency_key == identity.provider_call_id
    assert set(identity.to_safe_mapping()) == {
        "provider_call_id",
        "idempotency_key",
        "subject",
        "generation",
        "source_checkpoint_id",
        "graph_step",
        "model_call_ordinal",
        "request_fingerprint",
    }


def test_context_evidence_uses_a_closed_v1_vocabulary_and_round_trips_safe_json() -> None:
    assert tuple(item.value for item in STABLE_CONTEXT_EVIDENCE_TYPES) == (
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
    )
    subject = ContextSubject.lead_thread(thread_id=THREAD_ID)
    generation = ContextWindowGeneration(generation_id=GENERATION_ID)
    identity = ProviderCallIdentity.derive(
        subject=subject,
        generation=generation,
        source_checkpoint_id="checkpoint-7",
        graph_step="lead:model",
        model_call_ordinal=3,
        request_fingerprint="c" * 64,
    )
    measurement = FinalRequestMeasurement(
        request_fingerprint="c" * 64,
        adapter_revision="openai-cost-v1",
        contributions=(),
    )
    evidence = ContextEvidence(
        evidence_seq="17",
        subject=subject,
        generation=generation,
        origin_run_id="run-7",
        occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
        payload=RequestPreparedV1(
            provider_call=identity,
            measurement=measurement,
        ),
    )

    safe = evidence.to_safe_mapping()
    assert evidence.event_type is ContextEvidenceType.REQUEST_PREPARED
    assert safe["payload"]["event_type"] == "request.prepared.v1"
    assert ContextEvidence.from_safe_mapping(safe) == evidence
    assert "prompt" not in repr(safe).lower()

    replay = ContextEvidence(
        evidence_seq="18",
        subject=subject,
        generation=generation,
        origin_run_id="run-7",
        occurred_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        payload=evidence.payload,
    )
    assert replay.idempotency_key == evidence.idempotency_key

    unsafe = {**safe, "message_text": "do not persist me"}
    with pytest.raises(ValidationError):
        ContextEvidence.from_safe_mapping(unsafe)

    unsafe_payload = {
        **safe,
        "payload": {**safe["payload"], "prompt": "do not persist me"},
    }
    with pytest.raises(ValidationError):
        ContextEvidence.from_safe_mapping(unsafe_payload)


def test_compaction_identity_distinguishes_lead_checkpoints_from_ephemeral_task_state() -> None:
    source_generation = ContextWindowGeneration(generation_id=GENERATION_ID)
    result_generation = ContextWindowGeneration(
        generation_id=UUID("55555555-5555-4555-8555-555555555555"),
    )
    task_event = CompactionCommittedV1(
        receipt_id="e" * 64,
        source_state_digest="f" * 64,
        result_state_digest="1" * 64,
        source_generation=source_generation,
        result_generation=result_generation,
        source_tokens=1000,
        result_tokens=300,
        summary_tokens=80,
        summary_digest="2" * 64,
    )

    assert task_event.source_checkpoint_id is None
    assert task_event.result_checkpoint_id is None
    assert task_event.source_state_digest == "f" * 64

    with pytest.raises(ValidationError, match="identity domain"):
        CompactionCommittedV1(
            receipt_id="e" * 64,
            source_checkpoint_id="checkpoint-source",
            result_checkpoint_id="checkpoint-result",
            source_state_digest="f" * 64,
            result_state_digest="1" * 64,
            source_generation=source_generation,
            result_generation=result_generation,
            source_tokens=1000,
            result_tokens=300,
            summary_tokens=80,
            summary_digest="2" * 64,
        )

    with pytest.raises(ValidationError, match="both state digests"):
        CompactionCommittedV1(
            receipt_id="e" * 64,
            source_state_digest="f" * 64,
            source_generation=source_generation,
            result_generation=result_generation,
            source_tokens=1000,
            result_tokens=300,
            summary_digest="2" * 64,
        )
