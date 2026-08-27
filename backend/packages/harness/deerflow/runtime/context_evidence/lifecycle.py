"""Pure Provider-call idempotency and recovery decisions over Context Evidence."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field

from .contracts import _StrictContract
from .evidence import (
    CheckpointLinkedV1,
    ContextEvidence,
    ProviderAmbiguousV1,
    ProviderFailedV1,
    ProviderObservedV1,
    ProviderRetrySafety,
    ProviderUsageUnreportedV1,
    RequestDispatchedV1,
    RequestPreparedV1,
)


class ContextEvidenceSequenceError(ValueError):
    """The append-only Evidence history violates its closed lifecycle."""


def validate_context_evidence_history(
    evidence: Sequence[ContextEvidence],
) -> None:
    sequences = [int(item.evidence_seq) for item in evidence]
    if any(current <= previous for previous, current in zip(sequences, sequences[1:], strict=False)):
        raise ContextEvidenceSequenceError("Evidence sequences must be strictly increasing")
    calls: dict[str, dict[str, bool]] = {}
    for item in evidence:
        payload = item.payload
        if isinstance(payload, RequestPreparedV1):
            call_id = payload.provider_call.provider_call_id
            if call_id in calls:
                raise ContextEvidenceSequenceError("Provider call was prepared more than once")
            calls[call_id] = {
                "dispatched": False,
                "terminal": False,
                "result": False,
                "linked": False,
            }
            continue
        call_id = getattr(payload, "provider_call_id", None)
        if call_id is None:
            continue
        state = calls.get(call_id)
        if state is None:
            raise ContextEvidenceSequenceError("Provider lifecycle event occurred before preparation")
        if isinstance(payload, RequestDispatchedV1):
            if state["dispatched"]:
                raise ContextEvidenceSequenceError("Provider call was dispatched more than once")
            state["dispatched"] = True
        elif isinstance(
            payload,
            (
                ProviderObservedV1,
                ProviderUsageUnreportedV1,
                ProviderFailedV1,
                ProviderAmbiguousV1,
            ),
        ):
            if not state["dispatched"]:
                raise ContextEvidenceSequenceError("Provider outcome occurred before dispatch")
            if state["terminal"]:
                raise ContextEvidenceSequenceError("Provider call has conflicting terminal Evidence")
            state["terminal"] = True
            state["result"] = isinstance(
                payload,
                (ProviderObservedV1, ProviderUsageUnreportedV1),
            )
        elif isinstance(payload, CheckpointLinkedV1):
            if not state["result"]:
                raise ContextEvidenceSequenceError("Checkpoint was linked before a Provider result")
            if state["linked"]:
                raise ContextEvidenceSequenceError("Provider result was linked more than once")
            state["linked"] = True


class ProviderCallDisposition(StrEnum):
    DISPATCH = "dispatch"
    MARK_AMBIGUOUS = "mark_ambiguous"
    REPAIR_CHECKPOINT_LINK = "repair_checkpoint_link"
    REUSE_RESULT = "reuse_result"
    RETRY_PROVEN_SAFE_FAILURE = "retry_proven_safe_failure"
    TERMINAL_FAILURE = "terminal_failure"
    TERMINAL_AMBIGUOUS = "terminal_ambiguous"


class ProviderCallRecoveryPlan(_StrictContract):
    disposition: ProviderCallDisposition
    provider_call_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    latest_evidence_seq: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    input_tokens: int | None = Field(default=None, ge=0)
    checkpoint_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )


def resolve_provider_call(
    evidence: Sequence[ContextEvidence],
    provider_call_id: str,
) -> ProviderCallRecoveryPlan:
    """Return the only safe next action for a durable Provider-call identity."""

    validate_context_evidence_history(evidence)
    matching: list[ContextEvidence] = []
    for item in evidence:
        payload = item.payload
        call_id = payload.provider_call.provider_call_id if isinstance(payload, RequestPreparedV1) else getattr(payload, "provider_call_id", None)
        if call_id == provider_call_id:
            matching.append(item)
    if not matching or not isinstance(matching[0].payload, RequestPreparedV1):
        raise ContextEvidenceSequenceError("Provider call has no prepared Evidence")
    latest_seq = matching[-1].evidence_seq
    dispatched = any(isinstance(item.payload, RequestDispatchedV1) for item in matching)
    outcome = next(
        (
            item.payload
            for item in reversed(matching)
            if isinstance(
                item.payload,
                (
                    ProviderObservedV1,
                    ProviderUsageUnreportedV1,
                    ProviderFailedV1,
                    ProviderAmbiguousV1,
                ),
            )
        ),
        None,
    )
    linked = next(
        (item.payload for item in reversed(matching) if isinstance(item.payload, CheckpointLinkedV1)),
        None,
    )
    if not dispatched:
        disposition = ProviderCallDisposition.DISPATCH
    elif outcome is None:
        disposition = ProviderCallDisposition.MARK_AMBIGUOUS
    elif isinstance(outcome, ProviderAmbiguousV1):
        disposition = ProviderCallDisposition.TERMINAL_AMBIGUOUS
    elif isinstance(outcome, ProviderFailedV1):
        disposition = ProviderCallDisposition.RETRY_PROVEN_SAFE_FAILURE if outcome.retry_safety is ProviderRetrySafety.NO_RESPONSE_PROVEN else ProviderCallDisposition.TERMINAL_FAILURE
    elif linked is None:
        disposition = ProviderCallDisposition.REPAIR_CHECKPOINT_LINK
    else:
        disposition = ProviderCallDisposition.REUSE_RESULT
    return ProviderCallRecoveryPlan(
        disposition=disposition,
        provider_call_id=provider_call_id,
        latest_evidence_seq=latest_seq,
        input_tokens=outcome.input_tokens if isinstance(outcome, ProviderObservedV1) else None,
        checkpoint_id=linked.checkpoint_id if isinstance(linked, CheckpointLinkedV1) else None,
        failure_code=outcome.failure_code if isinstance(outcome, ProviderFailedV1) else None,
    )


__all__ = [
    "ContextEvidenceSequenceError",
    "ProviderCallDisposition",
    "ProviderCallRecoveryPlan",
    "resolve_provider_call",
    "validate_context_evidence_history",
]
