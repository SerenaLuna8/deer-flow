"""Pure rebuild and reduction logic for Context Projection Heads."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from .contracts import STABLE_CONTEXT_LANES, ContextLane, TokenEstimateKind
from .evidence import (
    CompactionCommittedV1,
    ContextEvidence,
    ProviderAmbiguousV1,
    ProviderFailedV1,
    ProviderObservedV1,
    ProviderUsageUnreportedV1,
    RequestPreparedV1,
    WindowRebasedV1,
)
from .lifecycle import (
    ContextEvidenceSequenceError,
    validate_context_evidence_history,
)
from .projection import (
    ContextCoverage,
    ContextNoticeCode,
    ContextProjectionHead,
    ContextProjectionSource,
    LastProviderObservation,
    ProjectionBasis,
    ProjectionLane,
    ProjectionNotice,
    ProjectionTotals,
)


class ContextProjectionRegressionError(ValueError):
    """A reducer candidate would replace a newer Projection Head."""


def _projector_revision_number(value: str) -> int:
    match = re.search(r"-v([1-9][0-9]*)$", value)
    if match is None:
        raise ContextProjectionRegressionError("projector revision is not monotonic")
    return int(match.group(1))


def _lanes(source: ContextProjectionSource) -> tuple[ProjectionLane, ...]:
    grouped: dict[ContextLane, list] = {lane: [] for lane in STABLE_CONTEXT_LANES}
    for contribution in source.measurement.contributions:
        grouped[contribution.lane].append(contribution.token_estimate)
    lanes: list[ProjectionLane] = []
    for lane in STABLE_CONTEXT_LANES:
        estimates = grouped[lane]
        if not estimates:
            continue
        upper_bounds = [estimate.safety_upper_bound_tokens for estimate in estimates]
        lanes.append(
            ProjectionLane(
                lane=lane,
                projected_tokens=sum(estimate.projected_tokens for estimate in estimates),
                lower_bound_tokens=sum(estimate.lower_bound_tokens for estimate in estimates),
                safety_upper_bound_tokens=(None if any(value is None for value in upper_bounds) else sum(value or 0 for value in upper_bounds)),
            )
        )
    return tuple(lanes)


def _notices(
    source: ContextProjectionSource,
    subject_evidence: Sequence[ContextEvidence],
) -> tuple[ProjectionNotice, ...]:
    notices: list[ProjectionNotice] = []
    visual_unmeasured = sum(
        contribution.token_estimate.unmeasured_items for contribution in source.measurement.contributions if contribution.lane is ContextLane.VISUAL_MEDIA and contribution.token_estimate.kind is TokenEstimateKind.UNMEASURED
    )
    if visual_unmeasured:
        notices.append(
            ProjectionNotice(
                code=ContextNoticeCode.VISUAL_COST_UNMEASURED,
                count=visual_unmeasured,
                lane=ContextLane.VISUAL_MEDIA,
            )
        )
    if source.freshness.value == "stale":
        notices.append(ProjectionNotice(code=ContextNoticeCode.PROJECTION_STALE))
    if source.model.context_window_tokens is None:
        notices.append(ProjectionNotice(code=ContextNoticeCode.CAPACITY_UNKNOWN))
    terminal_payloads = [item.payload for item in subject_evidence if isinstance(item.payload, (ProviderObservedV1, ProviderUsageUnreportedV1, ProviderFailedV1, ProviderAmbiguousV1))]
    if terminal_payloads:
        latest_terminal = terminal_payloads[-1]
        if isinstance(latest_terminal, ProviderUsageUnreportedV1):
            notices.append(ProjectionNotice(code=ContextNoticeCode.PROVIDER_USAGE_UNREPORTED))
        elif isinstance(latest_terminal, ProviderAmbiguousV1):
            notices.append(ProjectionNotice(code=ContextNoticeCode.PROVIDER_CALL_AMBIGUOUS))
    return tuple(notices)


class ContextProjector:
    """Rebuild a safe read model from current content facts and immutable Evidence."""

    @staticmethod
    def rebuild(
        *,
        source: ContextProjectionSource,
        evidence: Sequence[ContextEvidence],
        evidence_high_watermark: str | None = None,
        projection_seq: str,
        projector_revision: str,
        as_of: datetime,
    ) -> ContextProjectionHead:
        validate_context_evidence_history(evidence)
        latest_evidence_seq = int(evidence[-1].evidence_seq) if evidence else 0
        if evidence_high_watermark is None:
            evidence_seq = str(latest_evidence_seq)
        else:
            if re.fullmatch(r"^(0|[1-9][0-9]*)$", evidence_high_watermark) is None:
                raise ContextEvidenceSequenceError(
                    "Evidence high-water mark must be a canonical sequence",
                )
            if int(evidence_high_watermark) < latest_evidence_seq:
                raise ContextEvidenceSequenceError(
                    "Evidence high-water mark cannot precede loaded Evidence",
                )
            evidence_seq = evidence_high_watermark
        subject_evidence = tuple(item for item in evidence if item.subject == source.subject)
        generation_evidence = tuple(item for item in subject_evidence if item.generation == source.generation)
        prepared_by_call = {item.payload.provider_call.provider_call_id: item.payload for item in generation_evidence if isinstance(item.payload, RequestPreparedV1)}
        if source.current_provider_call_id is not None:
            prepared = prepared_by_call.get(source.current_provider_call_id)
            if prepared is None:
                raise ContextEvidenceSequenceError("current Provider call has no prepared Evidence")
            if prepared.measurement.request_fingerprint != source.measurement.request_fingerprint:
                raise ContextEvidenceSequenceError("current Projection and prepared request fingerprints disagree")

        lanes = _lanes(source)
        projected_tokens = sum(lane.projected_tokens for lane in lanes)
        lower_bound_tokens = sum(lane.lower_bound_tokens for lane in lanes)
        upper_bounds = [lane.safety_upper_bound_tokens for lane in lanes]
        safety_upper_bound_tokens = None if any(value is None for value in upper_bounds) else sum(value or 0 for value in upper_bounds)
        capacity = source.model.context_window_tokens
        totals = ProjectionTotals(
            projected_tokens=projected_tokens,
            lower_bound_tokens=lower_bound_tokens,
            safety_upper_bound_tokens=safety_upper_bound_tokens,
            context_window_tokens=capacity,
            remaining_tokens=None if capacity is None else max(capacity - projected_tokens, 0),
            progress_percent=None if capacity is None else min(round(projected_tokens * 100 / capacity, 1), 100.0),
        )
        observations = [item for item in generation_evidence if isinstance(item.payload, ProviderObservedV1)]
        latest_observation = observations[-1] if observations else None
        public_observation = (
            None
            if latest_observation is None
            else LastProviderObservation(
                provider_call_id=latest_observation.payload.provider_call_id,
                input_tokens=latest_observation.payload.input_tokens,
                observed_at=latest_observation.occurred_at,
            )
        )
        if not source.measurement.contributions:
            basis = ProjectionBasis.EMPTY
        elif public_observation is None:
            basis = ProjectionBasis.ESTIMATED
        else:
            all_exact = all(contribution.token_estimate.kind is TokenEstimateKind.EXACT for contribution in source.measurement.contributions)
            matches_current = public_observation.provider_call_id == source.current_provider_call_id
            basis = ProjectionBasis.PROVIDER_CONFIRMED if matches_current and all_exact and public_observation.input_tokens == projected_tokens else ProjectionBasis.HYBRID
        coverage = ContextCoverage.PARTIAL if safety_upper_bound_tokens is None else ContextCoverage.COMPLETE
        return ContextProjectionHead(
            thread_id=source.subject.thread_id,
            subject=source.subject,
            phase=source.phase,
            projection_seq=projection_seq,
            evidence_seq=evidence_seq,
            context_window_generation=source.generation.generation_id,
            checkpoint_id=source.checkpoint_id,
            projector_revision=projector_revision,
            model=source.model,
            basis=basis,
            coverage=coverage,
            freshness=source.freshness,
            totals=totals,
            lanes=lanes,
            last_provider_observation=public_observation,
            compaction=source.compaction,
            notices=_notices(source, generation_evidence),
            as_of=as_of,
        )

    @staticmethod
    def reduce(
        *,
        current: ContextProjectionHead,
        source: ContextProjectionSource,
        evidence: Sequence[ContextEvidence],
        evidence_high_watermark: str | None = None,
        projection_seq: str,
        projector_revision: str,
        as_of: datetime,
    ) -> ContextProjectionHead:
        """Build and accept one candidate only when every Head clock advances."""

        if source.subject != current.subject:
            raise ContextProjectionRegressionError("Context Subject cannot change during Head reduction")
        if int(projection_seq) <= int(current.projection_seq):
            raise ContextProjectionRegressionError("projection_seq must advance monotonically")
        if _projector_revision_number(projector_revision) < _projector_revision_number(current.projector_revision):
            raise ContextProjectionRegressionError("older projector revision cannot replace a newer Head")
        candidate = ContextProjector.rebuild(
            source=source,
            evidence=evidence,
            evidence_high_watermark=evidence_high_watermark,
            projection_seq=projection_seq,
            projector_revision=projector_revision,
            as_of=as_of,
        )
        if candidate.context_window_generation == current.context_window_generation and candidate.last_provider_observation is None and current.last_provider_observation is not None:
            candidate = candidate.model_copy(
                update={
                    "basis": (ProjectionBasis.EMPTY if not source.measurement.contributions else ProjectionBasis.HYBRID),
                    "last_provider_observation": (current.last_provider_observation),
                }
            )
        if int(candidate.evidence_seq) < int(current.evidence_seq):
            raise ContextProjectionRegressionError("Evidence high-water mark cannot move backwards")
        if candidate.context_window_generation != current.context_window_generation:
            replacement_generations = {item.payload.result_generation.generation_id for item in evidence if isinstance(item.payload, (CompactionCommittedV1, WindowRebasedV1)) and item.subject == source.subject}
            if candidate.context_window_generation not in replacement_generations:
                raise ContextProjectionRegressionError("Context Window Generation changed without replacement Evidence")
        return candidate


__all__ = [
    "ContextEvidenceSequenceError",
    "ContextProjectionRegressionError",
    "ContextProjector",
]
