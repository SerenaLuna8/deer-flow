"""Content-free Context Projection receipts persisted with checkpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from .contracts import ContextSubject, FinalRequestMeasurement, _StrictContract
from .evidence import ContextWindowGeneration
from .projection import (
    CompactionProjection,
    ContextModelProjection,
    ProjectionPhase,
)

_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ContextCheckpointEstimator(_StrictContract):
    """Safe declarations needed to remeasure replacement checkpoint state."""

    error_allowance_ratio: float = Field(ge=0, le=1)
    provider_fixed_overhead_tokens: int = Field(ge=0)
    provider_per_message_overhead_tokens: int = Field(ge=0)
    provider_per_tool_overhead_tokens: int = Field(ge=0)
    fixed_message_count: int = Field(ge=0)
    tool_count: int = Field(ge=0)


class ContextCheckpointProjectionSnapshot(_StrictContract):
    """Rebuild input stored beside content, without retaining that content."""

    contract_version: Literal[1] = 1
    generation: ContextWindowGeneration
    model: ContextModelProjection
    measurement: FinalRequestMeasurement
    compaction: CompactionProjection
    estimator: ContextCheckpointEstimator
    provider_call_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    provider_subject: ContextSubject | None = None
    origin_run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    provider_response_message_start: int | None = Field(default=None, ge=0)
    provider_response_message_count: int | None = Field(default=None, ge=1)
    provider_response_digest: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def _validate_provider_response_authority(self) -> Self:
        authority = (
            self.provider_call_id,
            self.provider_subject,
            self.origin_run_id,
            self.provider_response_message_start,
            self.provider_response_message_count,
            self.provider_response_digest,
        )
        if any(value is not None for value in authority) and not all(value is not None for value in authority):
            raise ValueError(
                "checkpoint Provider response authority must be complete",
            )
        return self

    def without_provider_response_authority(self) -> Self:
        """Clear linkage proof when history changes or crosses Thread scope."""

        return self.model_copy(
            update={
                "provider_call_id": None,
                "provider_subject": None,
                "origin_run_id": None,
                "provider_response_message_start": None,
                "provider_response_message_count": None,
                "provider_response_digest": None,
            },
        )

    def to_safe_mapping(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_safe_mapping(cls, value: Mapping[str, object]) -> Self:
        if not isinstance(value, Mapping):
            raise TypeError("Context checkpoint Projection snapshot must be a mapping")
        return cls.model_validate_json(
            json.dumps(dict(value), separators=(",", ":")),
        )


class ContextCompactionCheckpointReceipt(_StrictContract):
    """Idempotent repair authority written into a compacted checkpoint."""

    contract_version: Literal[1] = 1
    receipt_id: str = Field(min_length=1, max_length=128, pattern=_SAFE_ID_PATTERN)
    source_checkpoint_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_SAFE_ID_PATTERN,
    )
    source_generation: ContextWindowGeneration
    result_generation: ContextWindowGeneration
    source_tokens: int = Field(ge=0)
    result_tokens: int = Field(ge=0)
    summary_digest: str = Field(pattern=_SHA256_PATTERN)
    projection_snapshot: ContextCheckpointProjectionSnapshot
    phase: ProjectionPhase = ProjectionPhase.IDLE
    origin_run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )

    @model_validator(mode="after")
    def _validate_replacement(self) -> Self:
        if self.source_generation == self.result_generation:
            raise ValueError("compaction receipt must begin a new generation")
        if self.result_tokens > self.source_tokens:
            raise ValueError("compaction receipt cannot increase retained context")
        if self.projection_snapshot.generation != self.result_generation:
            raise ValueError("compaction receipt and Projection snapshot disagree")
        if self.projection_snapshot.measurement.projected_tokens != self.result_tokens:
            raise ValueError("compaction result size and Projection snapshot disagree")
        if self.projection_snapshot.provider_call_id is not None:
            raise ValueError(
                "compaction result cannot retain Provider response authority",
            )
        if (self.phase is ProjectionPhase.ACTIVE) != (self.origin_run_id is not None):
            raise ValueError("active compaction receipts require their origin Run")
        return self

    def to_safe_mapping(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_safe_mapping(cls, value: Mapping[str, object]) -> Self:
        if not isinstance(value, Mapping):
            raise TypeError("Context compaction receipt must be a mapping")
        return cls.model_validate_json(
            json.dumps(dict(value), separators=(",", ":")),
        )


__all__ = [
    "ContextCheckpointEstimator",
    "ContextCheckpointProjectionSnapshot",
    "ContextCompactionCheckpointReceipt",
]
