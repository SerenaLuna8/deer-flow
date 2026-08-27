"""Strict, content-free contracts for Context Evidence and projections."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ContextSubjectKind(StrEnum):
    LEAD_THREAD = "lead_thread"
    SUBAGENT_TASK = "subagent_task"


class ContextSubject(_StrictContract):
    """One independently measured Lead Thread or Sub-Agent Task execution."""

    kind: ContextSubjectKind
    thread_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    execution_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )

    @model_validator(mode="after")
    def _validate_kind_identity(self) -> Self:
        if self.kind is ContextSubjectKind.LEAD_THREAD and self.execution_id is not None:
            raise ValueError("Lead Thread subjects cannot carry an execution_id")
        if self.kind is ContextSubjectKind.SUBAGENT_TASK and self.execution_id is None:
            raise ValueError("Sub-Agent Task subjects require an execution_id")
        return self

    @classmethod
    def lead_thread(cls, *, thread_id: str | UUID) -> Self:
        return cls(kind=ContextSubjectKind.LEAD_THREAD, thread_id=str(thread_id))

    @classmethod
    def subagent_task(cls, *, thread_id: str | UUID, execution_id: str | UUID) -> Self:
        return cls(
            kind=ContextSubjectKind.SUBAGENT_TASK,
            thread_id=str(thread_id),
            execution_id=str(execution_id),
        )

    def to_safe_mapping(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class ContextLane(StrEnum):
    SYSTEM_PROMPT = "system_prompt"
    AGENT_INSTRUCTIONS = "agent_instructions"
    TOOL_DEFINITIONS = "tool_definitions"
    SKILLS = "skills"
    MCP_DYNAMIC_TOOLS = "mcp_dynamic_tools"
    SUBAGENT_DEFINITIONS = "subagent_definitions"
    SUMMARIZED_CONVERSATION = "summarized_conversation"
    CONVERSATION = "conversation"
    VISUAL_MEDIA = "visual_media"
    PROVIDER_OVERHEAD = "provider_overhead"


STABLE_CONTEXT_LANES: tuple[ContextLane, ...] = tuple(ContextLane)


class TokenEstimateKind(StrEnum):
    EXACT = "exact"
    BOUNDED = "bounded"
    UNMEASURED = "unmeasured"


class TokenEstimate(_StrictContract):
    """One point estimate plus the bounds used for capacity safety."""

    kind: TokenEstimateKind
    projected_tokens: int = Field(ge=0)
    lower_bound_tokens: int = Field(ge=0)
    safety_upper_bound_tokens: int | None = Field(default=None, ge=0)
    unmeasured_items: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.lower_bound_tokens > self.projected_tokens:
            raise ValueError("lower bound cannot exceed the point projection")
        if self.safety_upper_bound_tokens is not None and self.projected_tokens > self.safety_upper_bound_tokens:
            raise ValueError("point projection cannot exceed the safety upper bound")
        if self.kind is TokenEstimateKind.EXACT:
            if self.lower_bound_tokens != self.projected_tokens or self.safety_upper_bound_tokens != self.projected_tokens or self.unmeasured_items != 0:
                raise ValueError("exact Token estimates must have identical bounds")
        elif self.kind is TokenEstimateKind.BOUNDED:
            if self.safety_upper_bound_tokens is None or self.unmeasured_items != 0:
                raise ValueError("bounded Token estimates require a complete upper bound")
        elif self.projected_tokens != 0 or self.lower_bound_tokens != 0 or self.safety_upper_bound_tokens is not None or self.unmeasured_items < 1:
            raise ValueError("unmeasured Token estimates carry only a positive item count")
        return self

    @classmethod
    def exact(cls, tokens: int) -> Self:
        return cls(
            kind=TokenEstimateKind.EXACT,
            projected_tokens=tokens,
            lower_bound_tokens=tokens,
            safety_upper_bound_tokens=tokens,
        )

    @classmethod
    def bounded(
        cls,
        *,
        projected_tokens: int,
        lower_bound_tokens: int,
        safety_upper_bound_tokens: int,
    ) -> Self:
        return cls(
            kind=TokenEstimateKind.BOUNDED,
            projected_tokens=projected_tokens,
            lower_bound_tokens=lower_bound_tokens,
            safety_upper_bound_tokens=safety_upper_bound_tokens,
        )

    @classmethod
    def unmeasured(cls, *, item_count: int) -> Self:
        return cls(
            kind=TokenEstimateKind.UNMEASURED,
            projected_tokens=0,
            lower_bound_tokens=0,
            safety_upper_bound_tokens=None,
            unmeasured_items=item_count,
        )


class VisualDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"
    ORIGINAL = "original"


class VisualCostStrategy(StrEnum):
    DIMENSION_DETAIL = "dimension_detail"
    MAX_PER_IMAGE = "max_per_image"
    UNMEASURED = "unmeasured"


class VisualMeasurementMetadata(_StrictContract):
    """Safe image facts; paths, file references, and bytes are intentionally absent."""

    image_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["image/jpeg", "image/png", "image/webp", "image/gif"]
    size_bytes: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1, le=8_192)
    height: int | None = Field(default=None, ge=1, le=8_192)
    detail: VisualDetail
    strategy: VisualCostStrategy

    @model_validator(mode="after")
    def _validate_dimensions(self) -> Self:
        if (self.width is None) != (self.height is None):
            raise ValueError("visual width and height must be present together")
        if self.strategy is VisualCostStrategy.DIMENSION_DETAIL and self.width is None:
            raise ValueError("dimension/detail visual cost requires dimensions")
        return self


class ContextContribution(_StrictContract):
    """One positively attributed, model-visible piece of a final request."""

    contribution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane: ContextLane
    model_visible_bytes: int = Field(ge=0)
    token_estimate: TokenEstimate
    visual: VisualMeasurementMetadata | None = None

    @model_validator(mode="after")
    def _validate_visual_lane(self) -> Self:
        unmeasured_fixed_closure = self.token_estimate.kind is TokenEstimateKind.UNMEASURED and self.lane is ContextLane.SYSTEM_PROMPT and self.model_visible_bytes == 0 and self.visual is None
        if self.token_estimate.kind is TokenEstimateKind.UNMEASURED and self.lane is not ContextLane.VISUAL_MEDIA and not unmeasured_fixed_closure:
            raise ValueError(
                "unmeasured Token estimates require visual media or the fixed-closure sentinel",
            )
        if self.lane is ContextLane.VISUAL_MEDIA and self.visual is None:
            raise ValueError("visual_media contributions require safe visual metadata")
        if self.lane is not ContextLane.VISUAL_MEDIA and self.visual is not None:
            raise ValueError("visual metadata belongs only to the visual_media lane")
        if self.visual is not None:
            expected = {
                VisualCostStrategy.DIMENSION_DETAIL: TokenEstimateKind.BOUNDED,
                VisualCostStrategy.MAX_PER_IMAGE: TokenEstimateKind.BOUNDED,
                VisualCostStrategy.UNMEASURED: TokenEstimateKind.UNMEASURED,
            }[self.visual.strategy]
            if self.token_estimate.kind is not expected:
                raise ValueError("visual strategy and Token estimate kind disagree")
        return self

    def to_safe_mapping(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class VisualTokenCostContractError(ValueError):
    """The final request has visual material without a safe Token upper bound."""

    code = "VISUAL_TOKEN_UPPER_BOUND_UNAVAILABLE"

    def __init__(self, *, unmeasured_items: int) -> None:
        self.unmeasured_items = unmeasured_items
        super().__init__(self.code)


class ContextTokenCostContractError(ValueError):
    """A non-visual Context contribution has no safe upper bound."""

    code = "CONTEXT_TOKEN_UPPER_BOUND_UNAVAILABLE"

    def __init__(self, *, unmeasured_items: int) -> None:
        self.unmeasured_items = unmeasured_items
        super().__init__(self.code)


class FinalRequestMeasurement(_StrictContract):
    """Content-free measurement returned by a final shaped-request Adapter."""

    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_revision: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    contributions: tuple[ContextContribution, ...] = Field(max_length=16_384)

    @model_validator(mode="after")
    def _validate_unique_attribution(self) -> Self:
        contribution_ids = [item.contribution_id for item in self.contributions]
        if len(set(contribution_ids)) != len(contribution_ids):
            raise ValueError("duplicate contribution identity")
        source_identities = [item.source_identity_digest for item in self.contributions]
        if len(set(source_identities)) != len(source_identities):
            raise ValueError("each source identity must have exactly one lane attribution")
        return self

    def to_safe_mapping(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @property
    def projected_tokens(self) -> int:
        return sum(item.token_estimate.projected_tokens for item in self.contributions)

    @property
    def lower_bound_tokens(self) -> int:
        return sum(item.token_estimate.lower_bound_tokens for item in self.contributions)

    @property
    def safety_upper_bound_tokens(self) -> int | None:
        upper_bounds = [item.token_estimate.safety_upper_bound_tokens for item in self.contributions]
        if any(value is None for value in upper_bounds):
            return None
        return sum(value or 0 for value in upper_bounds)

    def require_safety_upper_bound(self) -> int:
        upper_bound = self.safety_upper_bound_tokens
        if upper_bound is not None:
            return upper_bound
        unmeasured = tuple(item for item in self.contributions if item.token_estimate.kind is TokenEstimateKind.UNMEASURED)
        unmeasured_items = sum(item.token_estimate.unmeasured_items for item in unmeasured)
        if unmeasured and all(item.lane is ContextLane.VISUAL_MEDIA for item in unmeasured):
            raise VisualTokenCostContractError(
                unmeasured_items=unmeasured_items,
            )
        raise ContextTokenCostContractError(
            unmeasured_items=unmeasured_items,
        )

    @classmethod
    def from_safe_mapping(cls, value: Any) -> Self:
        if not isinstance(value, dict):
            raise TypeError("final request measurement must be a mapping")
        return cls.model_validate_json(json.dumps(value, separators=(",", ":")))
