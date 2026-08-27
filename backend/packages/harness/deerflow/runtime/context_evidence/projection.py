"""Strict public and rebuild-input contracts for Context Projection v2."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from .contracts import (
    STABLE_CONTEXT_LANES,
    ContextLane,
    ContextSubject,
    FinalRequestMeasurement,
    _StrictContract,
)
from .evidence import MAX_SIGNED_BIGINT, ContextWindowGeneration

_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DECIMAL_SEQUENCE_PATTERN = r"^(0|[1-9][0-9]*)$"


class ProjectionPhase(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    SETTLED = "settled"


class ProjectionBasis(StrEnum):
    PROVIDER_CONFIRMED = "provider_confirmed"
    HYBRID = "hybrid"
    ESTIMATED = "estimated"
    EMPTY = "empty"


class ContextCoverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class ProjectionFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class CompactionAuthority(StrEnum):
    FROZEN_RUN = "frozen_run"
    IDLE_HISTORY = "idle_history"


class ContextNoticeCode(StrEnum):
    VISUAL_COST_UNMEASURED = "VISUAL_COST_UNMEASURED"
    PROJECTION_STALE = "PROJECTION_STALE"
    CAPACITY_UNKNOWN = "CAPACITY_UNKNOWN"
    PROVIDER_USAGE_UNREPORTED = "PROVIDER_USAGE_UNREPORTED"
    PROVIDER_CALL_AMBIGUOUS = "PROVIDER_CALL_AMBIGUOUS"


class ContextModelProjection(_StrictContract):
    identity_digest: str = Field(pattern=_SHA256_PATTERN)
    context_window_tokens: int | None = Field(default=None, ge=1, le=2_000_000)


class ProjectionLane(_StrictContract):
    lane: ContextLane
    projected_tokens: int = Field(ge=0)
    lower_bound_tokens: int = Field(ge=0)
    safety_upper_bound_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.lower_bound_tokens > self.projected_tokens:
            raise ValueError("lane lower bound cannot exceed its point projection")
        if self.safety_upper_bound_tokens is not None and self.projected_tokens > self.safety_upper_bound_tokens:
            raise ValueError("lane point projection cannot exceed its safety upper bound")
        return self


class ProjectionTotals(_StrictContract):
    projected_tokens: int = Field(ge=0)
    lower_bound_tokens: int = Field(ge=0)
    safety_upper_bound_tokens: int | None = Field(default=None, ge=0)
    context_window_tokens: int | None = Field(default=None, ge=1, le=2_000_000)
    remaining_tokens: int | None = Field(default=None, ge=0)
    progress_percent: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.lower_bound_tokens > self.projected_tokens:
            raise ValueError("total lower bound cannot exceed the point projection")
        if self.safety_upper_bound_tokens is not None and self.projected_tokens > self.safety_upper_bound_tokens:
            raise ValueError("total point projection cannot exceed the safety upper bound")
        if self.context_window_tokens is None:
            if self.remaining_tokens is not None or self.progress_percent is not None:
                raise ValueError("unknown capacity cannot expose remaining Tokens or percentage")
        else:
            if self.remaining_tokens != max(self.context_window_tokens - self.projected_tokens, 0):
                raise ValueError("remaining Tokens must be based on the point projection")
            expected_percent = min(round(self.projected_tokens * 100 / self.context_window_tokens, 1), 100.0)
            if self.progress_percent != expected_percent:
                raise ValueError("progress percentage must be based on the point projection")
        return self


class LastProviderObservation(_StrictContract):
    provider_call_id: str = Field(pattern=_SHA256_PATTERN)
    input_tokens: int = Field(ge=0)
    observed_at: datetime

    @field_validator("observed_at", mode="before")
    @classmethod
    def _parse_observed_at(cls, value: Any) -> Any:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("observed_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Provider observation timestamps must be timezone-aware")
        return value.astimezone(UTC)


class CompactionProjection(_StrictContract):
    enabled: bool
    threshold_tokens: int | None = Field(default=None, ge=1)
    reached: bool
    authority: CompactionAuthority | None = None
    blocked_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )

    @model_validator(mode="after")
    def _validate_policy(self) -> Self:
        if not self.enabled and self.reached:
            raise ValueError("disabled compaction cannot report a reached trigger")
        if self.enabled and self.authority is None:
            raise ValueError("enabled compaction requires an authority")
        return self


class ProjectionNotice(_StrictContract):
    code: ContextNoticeCode
    count: int | None = Field(default=None, ge=1)
    lane: ContextLane | None = None

    @model_validator(mode="after")
    def _validate_notice(self) -> Self:
        if self.code is ContextNoticeCode.VISUAL_COST_UNMEASURED:
            if self.count is None or self.lane is not ContextLane.VISUAL_MEDIA:
                raise ValueError("visual-cost notices require a count and visual_media lane")
        elif self.count is not None or self.lane is not None:
            raise ValueError("only visual-cost notices carry lane counts")
        return self


class ContextProjectionSource(_StrictContract):
    """Safe current-content facts supplied by a Checkpoint/request Adapter."""

    subject: ContextSubject
    phase: ProjectionPhase
    generation: ContextWindowGeneration
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    model: ContextModelProjection
    measurement: FinalRequestMeasurement
    current_provider_call_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    compaction: CompactionProjection
    freshness: ProjectionFreshness = ProjectionFreshness.CURRENT


class ContextProjectionHead(_StrictContract):
    contract_version: Literal[2] = 2
    thread_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    subject: ContextSubject
    phase: ProjectionPhase
    projection_seq: str = Field(pattern=_DECIMAL_SEQUENCE_PATTERN)
    evidence_seq: str = Field(pattern=_DECIMAL_SEQUENCE_PATTERN)
    context_window_generation: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    projector_revision: str = Field(
        min_length=4,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*-v[1-9][0-9]*$",
    )
    model: ContextModelProjection
    basis: ProjectionBasis
    coverage: ContextCoverage
    freshness: ProjectionFreshness
    totals: ProjectionTotals
    lanes: tuple[ProjectionLane, ...] = Field(max_length=len(STABLE_CONTEXT_LANES))
    last_provider_observation: LastProviderObservation | None = None
    compaction: CompactionProjection
    notices: tuple[ProjectionNotice, ...] = Field(max_length=len(ContextNoticeCode))
    as_of: datetime

    @field_validator("projection_seq", "evidence_seq")
    @classmethod
    def _validate_sequence(cls, value: str) -> str:
        if int(value) > MAX_SIGNED_BIGINT:
            raise ValueError("projection sequence exceeds signed BIGINT")
        return value

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: Any) -> Any:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("as_of")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Projection timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        if self.thread_id != self.subject.thread_id:
            raise ValueError("Projection Thread differs from its Context Subject")
        lane_order = {lane: index for index, lane in enumerate(STABLE_CONTEXT_LANES)}
        actual_lanes = [lane.lane for lane in self.lanes]
        if len(set(actual_lanes)) != len(actual_lanes):
            raise ValueError("Projection lanes must be unique")
        if actual_lanes != sorted(actual_lanes, key=lane_order.__getitem__):
            raise ValueError("Projection lanes must retain their stable order")
        if self.totals.projected_tokens != sum(lane.projected_tokens for lane in self.lanes):
            raise ValueError("Projection total must equal the visible lane sum")
        if self.totals.lower_bound_tokens != sum(lane.lower_bound_tokens for lane in self.lanes):
            raise ValueError("Projection lower bound must equal the lane sum")
        lane_upper_bounds = [lane.safety_upper_bound_tokens for lane in self.lanes]
        expected_upper = None if any(value is None for value in lane_upper_bounds) else sum(value or 0 for value in lane_upper_bounds)
        if self.totals.safety_upper_bound_tokens != expected_upper:
            raise ValueError("Projection safety bound must equal the lane bounds")
        expected_coverage = ContextCoverage.PARTIAL if expected_upper is None else ContextCoverage.COMPLETE
        if self.coverage is not expected_coverage:
            raise ValueError("Projection coverage disagrees with its lane bounds")
        if self.model.context_window_tokens != self.totals.context_window_tokens:
            raise ValueError("Projection model capacity disagrees with its totals")
        notice_codes = [notice.code for notice in self.notices]
        if len(set(notice_codes)) != len(notice_codes):
            raise ValueError("Projection notice codes must be unique")
        if self.freshness is ProjectionFreshness.STALE and ContextNoticeCode.PROJECTION_STALE not in notice_codes:
            raise ValueError("stale Projections require a stale notice")
        if self.model.context_window_tokens is None and ContextNoticeCode.CAPACITY_UNKNOWN not in notice_codes:
            raise ValueError("unknown capacity requires an explicit notice")
        return self

    def to_safe_mapping(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

    @classmethod
    def from_safe_mapping(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise TypeError("Context Projection Head must be a mapping")
        return cls.model_validate_json(json.dumps(dict(value), separators=(",", ":")))


__all__ = [
    "CompactionAuthority",
    "CompactionProjection",
    "ContextCoverage",
    "ContextModelProjection",
    "ContextNoticeCode",
    "ContextProjectionHead",
    "ContextProjectionSource",
    "LastProviderObservation",
    "ProjectionBasis",
    "ProjectionFreshness",
    "ProjectionLane",
    "ProjectionNotice",
    "ProjectionPhase",
    "ProjectionTotals",
]
