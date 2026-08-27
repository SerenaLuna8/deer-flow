"""Strict, secret-free contracts for database-backed runtime policy."""

from __future__ import annotations

import re
import unicodedata
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

from deerflow.config.vision_bridge_config import (
    DEFAULT_VISION_BRIDGE_TIMEOUT_SECONDS,
)

_JSON_SAFE_INTEGER = 2**53 - 1
_MAX_CHARS = 10_000_000
MAX_MEMORY_DOCUMENT_SECTION_TITLE_CHARS = 80
DEFAULT_MEMORY_DOCUMENT_SECTIONS = (
    "用户偏好与协作方式",
    "项目背景",
    "长期约束与架构决策",
    "当前仍有效的目标",
)
_FORBIDDEN_MEMORY_DOCUMENT_SECTION_MARKER = re.compile(
    r"\[H:\d+\]|\[(?:skip|correction|permanent|durable|ephemeral)\]",
    re.IGNORECASE,
)
ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$",
    ),
]
ModelName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    ),
]


class RuntimePolicySection(StrEnum):
    AGENT_RUNTIME = "agent_runtime"
    AUTH = "auth"
    AUTOMATIONS = "automations"
    MEMORY_DOCUMENT = "memory_document"
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
    model_name: ModelName | None = None
    dream_interval_minutes: int = Field(default=120, ge=15, le=1_440)
    max_injection_tokens: int = Field(default=2_000, ge=100, le=8_000)
    idle_seal_minutes: int = Field(default=1_440, ge=0, le=10_080)
    episode_retention_days: int = Field(default=365, ge=0, le=3_650)

    @field_validator("idle_seal_minutes")
    @classmethod
    def validate_idle_seal_minutes(cls, value: int) -> int:
        if value != 0 and value < 30:
            raise ValueError("idle_seal_minutes must be 0 or between 30 and 10080")
        return value

    @field_validator("episode_retention_days")
    @classmethod
    def validate_episode_retention_days(cls, value: int) -> int:
        if value != 0 and value < 30:
            raise ValueError("episode_retention_days must be 0 or between 30 and 3650")
        return value


class MemoryDocumentPolicy(_PolicyModel):
    sections: list[str] = Field(
        default_factory=lambda: list(DEFAULT_MEMORY_DOCUMENT_SECTIONS),
        min_length=2,
        max_length=8,
    )

    @field_validator("sections")
    @classmethod
    def validate_sections(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw_title in value:
            if any((category := unicodedata.category(character)).startswith("C") or category in {"Zl", "Zp"} for character in raw_title):
                raise ValueError("memory document section titles must not contain control characters or line separators")
            title = raw_title.strip()
            if not title or len(title) > MAX_MEMORY_DOCUMENT_SECTION_TITLE_CHARS:
                raise ValueError("memory document section title length is invalid")
            if title.startswith("#"):
                raise ValueError("memory document section titles must not contain Markdown prefixes")
            if _FORBIDDEN_MEMORY_DOCUMENT_SECTION_MARKER.search(title) is not None:
                raise ValueError("memory document section titles must not contain history markers")
            normalized.append(title)
        if len(normalized) != len(set(normalized)):
            raise ValueError("memory document section titles must be unique")
        return normalized


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


class IdenticalCallsPolicy(_PolicyModel):
    warn_threshold: int = Field(default=3, ge=1, le=100_000)
    hard_limit: int = Field(default=20, ge=1, le=100_000)
    window_size: int = Field(default=20, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> IdenticalCallsPolicy:
        if self.hard_limit < self.warn_threshold:
            raise ValueError("hard_limit must be >= warn_threshold")
        return self


class LoopDetectionPolicy(_PolicyModel):
    enabled: bool = True
    identical_calls: IdenticalCallsPolicy = Field(
        default_factory=IdenticalCallsPolicy,
    )


class InternalToolCallLimitsPolicy(_PolicyModel):
    lead_per_run: int = Field(default=200, ge=1, le=100_000)
    subagent_per_task: int = Field(default=50, ge=1, le=100_000)


class SubagentTotalsByWorkloadPolicy(_PolicyModel):
    interactive: int = Field(default=6, ge=1, le=50)
    research: int = Field(default=9, ge=1, le=50)


class SubagentPolicy(_PolicyModel):
    max_concurrent: int = Field(default=3, ge=1, le=4)
    max_total_per_run_by_workload: SubagentTotalsByWorkloadPolicy = Field(
        default_factory=SubagentTotalsByWorkloadPolicy,
    )


class VisionBridgePolicy(_PolicyModel):
    """Frozen selection for the text-model Vision Bridge.

    A non-null ``model_name`` is the enablement signal.  Provider, endpoint,
    secret and prompt details remain outside this policy and are resolved
    through the exact System Model snapshot admitted for the Run.
    """

    model_name: ModelName | None = None
    timeout_seconds: int = Field(
        default=DEFAULT_VISION_BRIDGE_TIMEOUT_SECONDS,
        ge=5,
        le=120,
    )
    contract_version: Literal["vision.bridge.v1"] = "vision.bridge.v1"


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
    internal_tool_call_limits: InternalToolCallLimitsPolicy = Field(
        default_factory=InternalToolCallLimitsPolicy,
    )
    read_before_write: EnabledPolicy = Field(default_factory=EnabledPolicy)
    safety_finish_reason: EnabledPolicy = Field(default_factory=EnabledPolicy)
    subagents: SubagentPolicy = Field(default_factory=SubagentPolicy)
    vision_bridge: VisionBridgePolicy = Field(default_factory=VisionBridgePolicy)


class AuthPolicyValue(_PolicyModel):
    allow_registration: bool = True


class AutomationsPolicyValue(_PolicyModel):
    enabled: bool = True
    poll_interval_seconds: int = Field(default=5, ge=1, le=300)
    max_concurrent_runs: int = Field(default=3, ge=1, le=32)
    min_once_delay_seconds: int = Field(default=60, ge=0, le=86_400)


class QuotaPolicyValue(_PolicyModel):
    default_member_limit: int = Field(default=20, ge=1, le=_JSON_SAFE_INTEGER)
    default_storage_bytes_limit: int = Field(default=5_368_709_120, ge=0, le=_JSON_SAFE_INTEGER)
    default_concurrent_run_limit: int = Field(default=3, ge=1, le=_JSON_SAFE_INTEGER)
    default_mcp_calls_daily_limit: int = Field(default=10_000, ge=0, le=_JSON_SAFE_INTEGER)
    warning_threshold: float = Field(default=0.8, gt=0.0, lt=1.0)


RuntimePolicyValue = AgentRuntimePolicyValue | AuthPolicyValue | AutomationsPolicyValue | MemoryDocumentPolicy | QuotaPolicyValue
RuntimePolicyEffectScope = Literal[
    "new_requests_and_runs",
    "new_requests",
    "new_memory_documents",
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
    revision: int
    schema_version: int
    payload_checksum: str
    value: AgentRuntimePolicyValue


@dataclass(frozen=True, slots=True)
class MaterializedAgentRuntimePolicy:
    schema_version: int
    value: AgentRuntimePolicyValue


@dataclass(frozen=True, slots=True)
class LockedMemoryDocumentPolicy:
    policy_version_id: uuid.UUID
    revision: int
    schema_version: int
    payload_checksum: str
    value: MemoryDocumentPolicy


CATALOG_DEFAULT_MODEL_REF = "default"
DEFAULT_VISION_BRIDGE_MODEL_NAME = str(
    uuid.uuid5(
        uuid.UUID("e9ef2794-807b-5d89-967c-c67be15b42e7"),
        "deepseek-v4-flash-vision-exp:model",
    )
)


def auxiliary_model_snapshot_ref(
    purpose: str,
    model_name: str | None,
    *,
    title_enabled: bool,
) -> str | None:
    """Return the catalog ref to freeze for one auxiliary Run purpose.

    ``title.model_name is None`` means the current system default model, not
    "skip the LLM". Other auxiliary purposes still omit a snapshot when unset.
    """

    if model_name is not None:
        return model_name
    if purpose == "title" and title_enabled:
        return CATALOG_DEFAULT_MODEL_REF
    return None


def default_policy_value(section: RuntimePolicySection) -> RuntimePolicyValue:
    if section is RuntimePolicySection.AGENT_RUNTIME:
        return AgentRuntimePolicyValue(
            vision_bridge=VisionBridgePolicy(
                model_name=DEFAULT_VISION_BRIDGE_MODEL_NAME,
            ),
        )
    if section is RuntimePolicySection.AUTH:
        return AuthPolicyValue()
    if section is RuntimePolicySection.AUTOMATIONS:
        return AutomationsPolicyValue()
    if section is RuntimePolicySection.MEMORY_DOCUMENT:
        return MemoryDocumentPolicy()
    if section is RuntimePolicySection.QUOTAS:
        return QuotaPolicyValue()
    raise AssertionError("unreachable runtime policy section")


__all__ = [
    "AgentRuntimePolicyValue",
    "AuthPolicyValue",
    "AutomationsPolicyValue",
    "CATALOG_DEFAULT_MODEL_REF",
    "DEFAULT_VISION_BRIDGE_MODEL_NAME",
    "DEFAULT_MEMORY_DOCUMENT_SECTIONS",
    "IdenticalCallsPolicy",
    "InternalToolCallLimitsPolicy",
    "LoopDetectionPolicy",
    "LockedMemoryDocumentPolicy",
    "MaterializedAgentRuntimePolicy",
    "MAX_MEMORY_DOCUMENT_SECTION_TITLE_CHARS",
    "MemoryDocumentPolicy",
    "QuotaPolicyValue",
    "RuntimePolicySection",
    "RuntimePolicyCatalogView",
    "RuntimePolicyEffectScope",
    "RuntimePolicyUpdateResult",
    "RuntimePolicyValue",
    "RuntimePolicyView",
    "SubagentPolicy",
    "SubagentTotalsByWorkloadPolicy",
    "VisionBridgePolicy",
    "LockedAgentRuntimePolicy",
    "auxiliary_model_snapshot_ref",
    "default_policy_value",
]
