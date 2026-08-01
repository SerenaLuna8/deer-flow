"""Strict, secret-free contracts for database-backed runtime policy."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

_JSON_SAFE_INTEGER = 2**53 - 1
_MAX_CHARS = 10_000_000
ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$",
    ),
]
CategoryName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$",
    ),
]
ModelName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    ),
]


class RuntimePolicySection(StrEnum):
    AGENT_RUNTIME = "agent_runtime"
    AUTH = "auth"
    QUOTAS = "quotas"


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EnabledPolicy(_PolicyModel):
    enabled: bool = True


class TokenBudgetPolicy(_PolicyModel):
    enabled: bool = False
    max_tokens: int = Field(default=200_000, ge=1_000, le=2_000_000)
    max_input_tokens: int | None = Field(default=None, ge=1, le=2_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=2_000_000)
    warn_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    hard_stop_threshold: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> TokenBudgetPolicy:
        if self.hard_stop_threshold < self.warn_threshold:
            raise ValueError("hard_stop_threshold must be >= warn_threshold")
        return self


class TitlePolicy(_PolicyModel):
    enabled: bool = True
    max_words: int = Field(default=6, ge=1, le=20)
    max_chars: int = Field(default=60, ge=10, le=200)
    model_name: ModelName | None = None


class InputPolishPolicy(_PolicyModel):
    enabled: bool = True
    max_chars: int = Field(default=4_000, ge=1, le=100_000)
    model_name: ModelName | None = None


class ContextSizePolicy(_PolicyModel):
    type: Literal["fraction", "tokens", "messages"]
    value: int | float

    @model_validator(mode="before")
    @classmethod
    def normalize_json_fraction_one(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("type") == "fraction" and type(value.get("value")) is int and value.get("value") == 1:
            return {**value, "value": 1.0}
        return value

    @model_validator(mode="after")
    def validate_value_for_type(self) -> ContextSizePolicy:
        if self.type == "fraction":
            if type(self.value) is not float or not 0 < self.value <= 1:
                raise ValueError("fraction value must be a float in (0, 1]")
        elif type(self.value) is not int or not 1 <= self.value <= 2_000_000:
            raise ValueError("tokens/messages value must be an integer in range")
        return self


class SummarizationPolicy(_PolicyModel):
    enabled: bool = True
    model_name: ModelName | None = None
    trigger: list[ContextSizePolicy] | None = Field(
        default_factory=lambda: [ContextSizePolicy(type="tokens", value=32_000)],
        max_length=8,
    )
    keep: ContextSizePolicy = Field(
        default_factory=lambda: ContextSizePolicy(type="messages", value=10),
    )
    trim_tokens_to_summarize: int | None = Field(default=15_564, ge=1, le=2_000_000)
    skill_file_read_tool_names: list[ToolName] = Field(
        default_factory=lambda: ["read_file", "read", "view", "cat"],
        min_length=1,
        max_length=32,
    )

    @field_validator("skill_file_read_tool_names")
    @classmethod
    def unique_tool_names(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("tool names must be unique")
        return value


class MemoryPolicy(_PolicyModel):
    enabled: bool = True
    search_enabled: bool = True
    debounce_seconds: int = Field(default=30, ge=1, le=300)
    model_name: ModelName | None = None
    max_facts: int = Field(default=100, ge=10, le=500)
    fact_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    injection_enabled: bool = True
    max_injection_tokens: int = Field(default=2_000, ge=100, le=8_000)
    token_counting: Literal["tiktoken", "char"] = "tiktoken"
    guaranteed_categories: list[CategoryName] = Field(
        default_factory=lambda: ["correction"],
        max_length=32,
    )
    guaranteed_token_budget: int = Field(default=500, ge=50, le=2_000)
    staleness_review_enabled: bool = True
    staleness_age_days: int = Field(default=90, ge=30, le=365)
    staleness_min_candidates: int = Field(default=3, ge=1, le=50)
    staleness_max_removals_per_cycle: int = Field(default=10, ge=1, le=50)
    staleness_protected_categories: list[CategoryName] = Field(
        default_factory=lambda: ["correction"],
        max_length=32,
    )

    @field_validator("guaranteed_categories", "staleness_protected_categories")
    @classmethod
    def unique_categories(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("categories must be unique")
        return value


class ToolSearchPolicy(_PolicyModel):
    enabled: bool = False
    auto_promote_top_k: int = Field(default=3, ge=1, le=5)


class ToolOutputPolicy(_PolicyModel):
    enabled: bool = True
    externalize_min_chars: int = Field(default=12_000, ge=0, le=_MAX_CHARS)
    preview_head_chars: int = Field(default=2_000, ge=0, le=_MAX_CHARS)
    preview_tail_chars: int = Field(default=1_000, ge=0, le=_MAX_CHARS)
    fallback_max_chars: int = Field(default=30_000, ge=0, le=_MAX_CHARS)
    fallback_head_chars: int = Field(default=8_000, ge=0, le=_MAX_CHARS)
    fallback_tail_chars: int = Field(default=3_000, ge=0, le=_MAX_CHARS)
    exempt_tools: list[ToolName] = Field(
        default_factory=lambda: ["read_file", "read_file_tool"],
        max_length=64,
    )
    tool_overrides: dict[ToolName, int] = Field(default_factory=dict, max_length=64)

    @field_validator("exempt_tools")
    @classmethod
    def unique_exempt_tools(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("tool names must be unique")
        return value

    @field_validator("tool_overrides")
    @classmethod
    def validate_override_values(cls, value: dict[str, int]) -> dict[str, int]:
        if any(type(item) is not int or not 0 <= item <= _MAX_CHARS for item in value.values()):
            raise ValueError("tool override out of range")
        return value


class ToolFrequencyOverridePolicy(_PolicyModel):
    warn: int = Field(ge=1, le=100_000)
    hard_limit: int = Field(ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> ToolFrequencyOverridePolicy:
        if self.hard_limit < self.warn:
            raise ValueError("hard_limit must be >= warn")
        return self


class LoopDetectionPolicy(_PolicyModel):
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
        },
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_threshold_order(self) -> LoopDetectionPolicy:
        if self.hard_limit < self.warn_threshold:
            raise ValueError("hard_limit must be >= warn_threshold")
        if self.tool_freq_hard_limit < self.tool_freq_warn:
            raise ValueError("tool_freq_hard_limit must be >= tool_freq_warn")
        return self


class SubagentPolicy(_PolicyModel):
    max_total_per_run: int = Field(default=6, ge=1, le=50)


class AgentRuntimePolicyValue(_PolicyModel):
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
    loop_detection: LoopDetectionPolicy = Field(default_factory=LoopDetectionPolicy)
    read_before_write: EnabledPolicy = Field(default_factory=EnabledPolicy)
    safety_finish_reason: EnabledPolicy = Field(default_factory=EnabledPolicy)
    subagents: SubagentPolicy = Field(default_factory=SubagentPolicy)


class AuthPolicyValue(_PolicyModel):
    allow_registration: bool = True


class QuotaPolicyValue(_PolicyModel):
    default_member_limit: int = Field(default=20, ge=1, le=_JSON_SAFE_INTEGER)
    default_storage_bytes_limit: int = Field(default=5_368_709_120, ge=0, le=_JSON_SAFE_INTEGER)
    default_concurrent_run_limit: int = Field(default=3, ge=1, le=_JSON_SAFE_INTEGER)
    default_mcp_calls_daily_limit: int = Field(default=10_000, ge=0, le=_JSON_SAFE_INTEGER)
    warning_threshold: float = Field(default=0.8, gt=0.0, lt=1.0)


RuntimePolicyValue = AgentRuntimePolicyValue | AuthPolicyValue | QuotaPolicyValue
RuntimePolicyEffectScope = Literal[
    "new_requests_and_runs",
    "new_requests",
    "next_authoritative_check",
]


@dataclass(frozen=True, slots=True)
class RuntimePolicyView:
    section: RuntimePolicySection
    revision: int
    schema_version: int
    value: RuntimePolicyValue
    effect_scope: RuntimePolicyEffectScope
    effective_revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimePolicyCatalogView:
    catalog_revision: int
    sections: Mapping[RuntimePolicySection, RuntimePolicyView]

    @classmethod
    def create(
        cls,
        catalog_revision: int,
        sections: Mapping[RuntimePolicySection, RuntimePolicyView],
    ) -> RuntimePolicyCatalogView:
        return cls(
            catalog_revision=catalog_revision,
            sections=MappingProxyType(dict(sections)),
        )


@dataclass(frozen=True, slots=True)
class RuntimePolicyUpdateResult:
    catalog_revision: int
    policy: RuntimePolicyView
    effective_at: datetime
    pending_roles: tuple[Literal["gateway", "worker", "scheduler"], ...] = ()


@dataclass(frozen=True, slots=True)
class LockedAgentRuntimePolicy:
    policy_version_id: uuid.UUID
    schema_version: int
    payload_checksum: str
    value: AgentRuntimePolicyValue


def default_policy_value(section: RuntimePolicySection) -> RuntimePolicyValue:
    if section is RuntimePolicySection.AGENT_RUNTIME:
        return AgentRuntimePolicyValue()
    if section is RuntimePolicySection.AUTH:
        return AuthPolicyValue()
    if section is RuntimePolicySection.QUOTAS:
        return QuotaPolicyValue()
    raise AssertionError("unreachable runtime policy section")


__all__ = [
    "AgentRuntimePolicyValue",
    "AuthPolicyValue",
    "QuotaPolicyValue",
    "RuntimePolicySection",
    "RuntimePolicyCatalogView",
    "RuntimePolicyEffectScope",
    "RuntimePolicyUpdateResult",
    "RuntimePolicyValue",
    "RuntimePolicyView",
    "LockedAgentRuntimePolicy",
    "default_policy_value",
]
