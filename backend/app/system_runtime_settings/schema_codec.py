"""Version-owned codecs for immutable Runtime Policy payloads."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.system_runtime_settings.models import (
    AuthPolicyValue,
    AutomationsPolicyValue,
    EnabledPolicy,
    InputPolishPolicy,
    MemoryDocumentPolicy,
    MemoryPolicy,
    QuotaPolicyValue,
    RuntimePolicySection,
    SummarizationPolicy,
    TitlePolicy,
    TokenBudgetPolicy,
    ToolFrequencyOverridePolicy,
    ToolName,
    ToolOutputPolicy,
    ToolSearchPolicy,
    VisionBridgePolicy,
)
from deerflow.vision.dispatch import (
    VISION_TOOL_FREQUENCY_HARD_STOP,
    VISION_TOOL_FREQUENCY_WARN,
)


class _SchemaV3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _LoopDetectionPolicyV3(_SchemaV3Model):
    enabled: bool = True
    warn_threshold: int = Field(default=3, ge=1, le=100_000)
    hard_limit: int = Field(default=5, ge=1, le=100_000)
    window_size: int = Field(default=20, ge=1, le=100_000)
    max_tracked_threads: int = Field(default=100, ge=1, le=100_000)
    tool_freq_warn: int = Field(default=30, ge=1, le=100_000)
    tool_freq_hard_limit: int = Field(default=50, ge=1, le=100_000)
    tool_freq_overrides: dict[ToolName, ToolFrequencyOverridePolicy] = Field(
        default_factory=lambda: {
            "web_fetch": ToolFrequencyOverridePolicy(warn=6, hard_limit=10),
            "web_search": ToolFrequencyOverridePolicy(warn=6, hard_limit=10),
            "recall_memory": ToolFrequencyOverridePolicy(warn=6, hard_limit=10),
            "inspect_image": ToolFrequencyOverridePolicy(
                warn=VISION_TOOL_FREQUENCY_WARN,
                hard_limit=VISION_TOOL_FREQUENCY_HARD_STOP,
            ),
        },
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_threshold_order(self) -> _LoopDetectionPolicyV3:
        if self.hard_limit < self.warn_threshold:
            raise ValueError("hard_limit must be >= warn_threshold")
        if self.tool_freq_hard_limit < self.tool_freq_warn:
            raise ValueError("tool_freq_hard_limit must be >= tool_freq_warn")
        return self


class _SubagentPolicyV3(_SchemaV3Model):
    max_total_per_run: int = Field(default=6, ge=1, le=50)


class _AgentRuntimePolicyValueV2(_SchemaV3Model):
    token_usage: EnabledPolicy = Field(default_factory=EnabledPolicy)
    token_budget: TokenBudgetPolicy = Field(default_factory=TokenBudgetPolicy)
    max_recursion_limit: int = Field(default=1_000, ge=1, le=100_000)
    title: TitlePolicy = Field(default_factory=TitlePolicy)
    suggestions: EnabledPolicy = Field(default_factory=EnabledPolicy)
    input_polish: InputPolishPolicy = Field(default_factory=InputPolishPolicy)
    summarization: SummarizationPolicy = Field(default_factory=SummarizationPolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    tool_search: ToolSearchPolicy = Field(default_factory=ToolSearchPolicy)
    tool_output: ToolOutputPolicy = Field(default_factory=ToolOutputPolicy)
    loop_detection: _LoopDetectionPolicyV3 = Field(
        default_factory=_LoopDetectionPolicyV3,
    )
    read_before_write: EnabledPolicy = Field(default_factory=EnabledPolicy)
    safety_finish_reason: EnabledPolicy = Field(default_factory=EnabledPolicy)
    subagents: _SubagentPolicyV3 = Field(default_factory=_SubagentPolicyV3)


class _AgentRuntimePolicyValueV3(_SchemaV3Model):
    token_usage: EnabledPolicy = Field(default_factory=EnabledPolicy)
    token_budget: TokenBudgetPolicy = Field(default_factory=TokenBudgetPolicy)
    max_recursion_limit: int = Field(default=1_000, ge=1, le=100_000)
    title: TitlePolicy = Field(default_factory=TitlePolicy)
    suggestions: EnabledPolicy = Field(default_factory=EnabledPolicy)
    input_polish: InputPolishPolicy = Field(default_factory=InputPolishPolicy)
    summarization: SummarizationPolicy = Field(default_factory=SummarizationPolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    tool_search: ToolSearchPolicy = Field(default_factory=ToolSearchPolicy)
    tool_output: ToolOutputPolicy = Field(default_factory=ToolOutputPolicy)
    loop_detection: _LoopDetectionPolicyV3 = Field(
        default_factory=_LoopDetectionPolicyV3,
    )
    read_before_write: EnabledPolicy = Field(default_factory=EnabledPolicy)
    safety_finish_reason: EnabledPolicy = Field(default_factory=EnabledPolicy)
    subagents: _SubagentPolicyV3 = Field(default_factory=_SubagentPolicyV3)
    vision_bridge: VisionBridgePolicy = Field(default_factory=VisionBridgePolicy)


_SCHEMA_V3_MODELS: Mapping[RuntimePolicySection, type[BaseModel]] = {
    RuntimePolicySection.AGENT_RUNTIME: _AgentRuntimePolicyValueV3,
    RuntimePolicySection.AUTH: AuthPolicyValue,
    RuntimePolicySection.AUTOMATIONS: AutomationsPolicyValue,
    RuntimePolicySection.MEMORY_DOCUMENT: MemoryDocumentPolicy,
    RuntimePolicySection.QUOTAS: QuotaPolicyValue,
}
_SCHEMA_V2_MODELS: Mapping[RuntimePolicySection, type[BaseModel]] = {
    RuntimePolicySection.AGENT_RUNTIME: _AgentRuntimePolicyValueV2,
    RuntimePolicySection.AUTH: AuthPolicyValue,
    RuntimePolicySection.AUTOMATIONS: AutomationsPolicyValue,
    RuntimePolicySection.MEMORY_DOCUMENT: MemoryDocumentPolicy,
    RuntimePolicySection.QUOTAS: QuotaPolicyValue,
}


def canonical_policy_value_v2(
    section: RuntimePolicySection,
    value: BaseModel | Mapping[str, object],
) -> dict[str, object]:
    """Return the exact schema-v2 value without later schema defaults."""

    raw = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
    return _SCHEMA_V2_MODELS[section].model_validate(raw).model_dump(mode="json")


def canonical_policy_value_v3(
    section: RuntimePolicySection,
    value: BaseModel | Mapping[str, object],
) -> dict[str, object]:
    """Return the exact schema-v3 value without current-schema defaults."""

    raw = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
    return _SCHEMA_V3_MODELS[section].model_validate(raw).model_dump(mode="json")


__all__ = ["canonical_policy_value_v2", "canonical_policy_value_v3"]
