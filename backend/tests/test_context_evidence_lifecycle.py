from datetime import UTC, datetime, timedelta
from uuid import UUID

from deerflow.runtime.context_evidence import (
    CheckpointLinkedV1,
    ContextEvidence,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderAmbiguityReason,
    ProviderAmbiguousV1,
    ProviderCallDisposition,
    ProviderCallIdentity,
    ProviderFailedV1,
    ProviderObservedV1,
    ProviderRetrySafety,
    RequestDispatchedV1,
    RequestPreparedV1,
    resolve_provider_call,
)

SUBJECT = ContextSubject.lead_thread(thread_id="11111111-1111-4111-8111-111111111111")
GENERATION = ContextWindowGeneration(generation_id=UUID("44444444-4444-4444-8444-444444444444"))
NOW = datetime(2026, 8, 27, tzinfo=UTC)
CALL = ProviderCallIdentity.derive(
    subject=SUBJECT,
    generation=GENERATION,
    source_checkpoint_id="checkpoint-7",
    graph_step="lead:model",
    model_call_ordinal=1,
    request_fingerprint="a" * 64,
)


def _event(sequence: int, payload: object) -> ContextEvidence:
    return ContextEvidence(
        evidence_seq=str(sequence),
        subject=SUBJECT,
        generation=GENERATION,
        origin_run_id="run-7",
        occurred_at=NOW + timedelta(seconds=sequence),
        payload=payload,
    )


PREPARED = _event(
    1,
    RequestPreparedV1(
        provider_call=CALL,
        measurement=FinalRequestMeasurement(
            request_fingerprint="a" * 64,
            adapter_revision="text-v1",
            contributions=(),
        ),
    ),
)
DISPATCHED = _event(2, RequestDispatchedV1(provider_call_id=CALL.provider_call_id))


def test_provider_call_recovery_never_repeats_an_ambiguous_dispatch() -> None:
    prepared = resolve_provider_call((PREPARED,), CALL.provider_call_id)
    dispatched = resolve_provider_call((PREPARED, DISPATCHED), CALL.provider_call_id)
    ambiguous = resolve_provider_call(
        (
            PREPARED,
            DISPATCHED,
            _event(
                3,
                ProviderAmbiguousV1(
                    provider_call_id=CALL.provider_call_id,
                    reason=ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN,
                ),
            ),
        ),
        CALL.provider_call_id,
    )

    assert prepared.disposition is ProviderCallDisposition.DISPATCH
    assert dispatched.disposition is ProviderCallDisposition.MARK_AMBIGUOUS
    assert ambiguous.disposition is ProviderCallDisposition.TERMINAL_AMBIGUOUS


def test_observed_provider_call_is_repaired_then_reused_without_dispatch() -> None:
    observed = _event(
        3,
        ProviderObservedV1(
            provider_call_id=CALL.provider_call_id,
            input_tokens=123,
        ),
    )
    needs_link = resolve_provider_call(
        (PREPARED, DISPATCHED, observed),
        CALL.provider_call_id,
    )
    settled = resolve_provider_call(
        (
            PREPARED,
            DISPATCHED,
            observed,
            _event(
                4,
                CheckpointLinkedV1(
                    provider_call_id=CALL.provider_call_id,
                    checkpoint_id="checkpoint-8",
                ),
            ),
        ),
        CALL.provider_call_id,
    )

    assert needs_link.disposition is ProviderCallDisposition.REPAIR_CHECKPOINT_LINK
    assert needs_link.input_tokens == 123
    assert settled.disposition is ProviderCallDisposition.REUSE_RESULT
    assert settled.checkpoint_id == "checkpoint-8"


def test_only_a_proven_no_response_failure_can_be_retried() -> None:
    safe_failure = resolve_provider_call(
        (
            PREPARED,
            DISPATCHED,
            _event(
                3,
                ProviderFailedV1(
                    provider_call_id=CALL.provider_call_id,
                    failure_code="CONNECT_REJECTED",
                    retry_safety=ProviderRetrySafety.NO_RESPONSE_PROVEN,
                ),
            ),
        ),
        CALL.provider_call_id,
    )

    assert safe_failure.disposition is ProviderCallDisposition.RETRY_PROVEN_SAFE_FAILURE
    assert safe_failure.failure_code == "CONNECT_REJECTED"


def test_adapter_proven_failed_response_can_be_retried() -> None:
    safe_failure = resolve_provider_call(
        (
            PREPARED,
            DISPATCHED,
            _event(
                3,
                ProviderFailedV1(
                    provider_call_id=CALL.provider_call_id,
                    failure_code="PROVIDER_HTTP_429",
                    retry_safety=(ProviderRetrySafety.FAILED_RESPONSE_RETRY_SAFE),
                ),
            ),
        ),
        CALL.provider_call_id,
    )

    assert safe_failure.disposition is ProviderCallDisposition.RETRY_PROVEN_SAFE_FAILURE
    assert safe_failure.failure_code == "PROVIDER_HTTP_429"
