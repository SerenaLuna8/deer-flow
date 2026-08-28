"""Configuration for conversation summarization."""

from dataclasses import dataclass
from string import Formatter
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ContextSizeType = Literal["tokens"]
DEFAULT_SKILL_FILE_READ_TOOL_NAMES: tuple[str, ...] = ("read_file", "read", "view", "cat")
# The packaged dual-segment SNIP prompt plus its bounded repair suffix costs
# roughly 830 approximate tokens before any conversation content. Budgets that
# cannot contain one leaf prompt can never plan a compaction, so authoring
# rejects them and the production factory clamps legacy values up to this floor.
MIN_TRIM_TOKENS_TO_SUMMARIZE = 2_000
_SUMMARY_PROMPT_CONTRACT_ERROR = "summary_prompt must be a valid format template whose only replacement field is {messages}"


@dataclass(frozen=True, slots=True)
class EffectiveCompactionPolicy:
    """One model-bound automatic-compaction policy.

    ``keep_tokens`` is the approximate recent-history selection target.
    Capacity adaptation may move the trigger earlier; the frozen Provider
    profile separately qualifies each concrete retained tail. The safety fields
    expose the statically provable contribution instead of hiding it inside a
    percentage clamp; ``retained_context_safety_tokens == 0`` means concrete
    retention is enforced dynamically by that profile.
    """

    trigger_tokens: int | None
    keep_tokens: int
    context_window_tokens: int | None
    fixed_noncompressible_safety_tokens: int
    summary_headroom_tokens: int
    retained_context_safety_tokens: int

    @property
    def noncompressible_safety_tokens(self) -> int:
        return self.fixed_noncompressible_safety_tokens + self.summary_headroom_tokens


class CompactionPolicyIncompatible(ValueError):
    """The authorized retention floor cannot fit below compaction's trigger."""


def resolve_effective_compaction_policy(
    *,
    trigger_tokens: int | None,
    keep_tokens: int,
    context_window_tokens: int | None,
    fixed_noncompressible_safety_tokens: int = 0,
    summary_headroom_tokens: int = 0,
    retained_context_safety_tokens: int | None = None,
) -> EffectiveCompactionPolicy:
    """Resolve the known portion of the joint compaction invariant.

    A known model capacity can advance the trigger. Profile-aware callers
    supply the Provider safety-bound cost of fixed material; they may pass zero
    for retained context when concrete cutoff candidates are checked at runtime.
    """

    effective_trigger = min(trigger_tokens, context_window_tokens) if trigger_tokens is not None and context_window_tokens is not None else trigger_tokens
    effective_retained_tokens = keep_tokens if retained_context_safety_tokens is None else retained_context_safety_tokens
    if effective_trigger is not None and fixed_noncompressible_safety_tokens + summary_headroom_tokens + effective_retained_tokens >= effective_trigger:
        raise CompactionPolicyIncompatible(
            "Compaction retention and noncompressible context must fit strictly below the effective trigger",
        )
    return EffectiveCompactionPolicy(
        trigger_tokens=effective_trigger,
        keep_tokens=keep_tokens,
        context_window_tokens=context_window_tokens,
        fixed_noncompressible_safety_tokens=(fixed_noncompressible_safety_tokens),
        summary_headroom_tokens=summary_headroom_tokens,
        retained_context_safety_tokens=effective_retained_tokens,
    )


def effective_compaction_trigger_tokens(
    trigger_tokens: int | None,
    context_window_tokens: int | None,
) -> int | None:
    """Clamp the absolute compaction trigger to the Provider Model capacity.

    The trigger is one global policy value while ``max_input_tokens`` varies
    per model. A trigger strictly above the capacity leaves a dead zone
    (``capacity < occupancy < trigger``) where the final Provider guard
    rejects the request before automatic compaction ever participates, so
    every site that freezes a trigger next to a known capacity clamps here.
    An unknown or non-positive capacity keeps the configured trigger.
    """

    if trigger_tokens is None:
        return None
    if not isinstance(context_window_tokens, int) or isinstance(context_window_tokens, bool) or context_window_tokens <= 0:
        return trigger_tokens
    return min(trigger_tokens, context_window_tokens)


def validate_summary_prompt_template(template: str) -> str:
    """Validate the trusted summary prompt's narrow formatting contract.

    Literal braces remain available through Python's normal ``{{`` / ``}}``
    escaping, while conversions, format specifications, attribute/index access,
    and every replacement field other than the required ``{messages}`` are
    rejected. Keeping the contract here makes malformed deployment config fail
    during Pydantic validation instead of at the first live compaction.
    """
    try:
        parsed = tuple(Formatter().parse(template))
    except (TypeError, ValueError) as exc:
        raise ValueError(_SUMMARY_PROMPT_CONTRACT_ERROR) from exc

    fields = [(field_name, format_spec, conversion) for _, field_name, format_spec, conversion in parsed if field_name is not None]
    if not fields or any(field_name != "messages" or format_spec or conversion for field_name, format_spec, conversion in fields):
        raise ValueError(_SUMMARY_PROMPT_CONTRACT_ERROR)
    return template


class ContextSize(BaseModel):
    """Token-based retention size for the post-summarization keep policy.

    Message-count and context-fraction measurements were removed; recent
    history is preserved by token count only.
    """

    type: ContextSizeType = Field(
        default="tokens",
        description="Type of context size specification (token count only)",
    )
    value: int = Field(ge=1, description="Number of recent-history tokens to preserve")

    def to_tuple(self) -> tuple[ContextSizeType, int]:
        """Convert to tuple format expected by SummarizationMiddleware."""
        return (self.type, self.value)


class SummarizationConfig(BaseModel):
    """Configuration for automatic conversation summarization."""

    enabled: bool = Field(
        default=False,
        description="Whether to enable automatic conversation summarization",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name to use for summarization (None = use a lightweight model)",
    )
    trigger_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Estimated-token threshold that triggers automatic summarization. None disables the automatic trigger.",
    )
    keep: ContextSize = Field(
        default_factory=lambda: ContextSize(type="tokens", value=64_000),
        description="Context retention policy after summarization. Specifies how many tokens of recent history to preserve.",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=4000,
        description="Maximum tokens to keep when preparing messages for summarization. Pass null to skip trimming.",
    )
    summary_prompt: str | None = Field(
        default=None,
        description="Custom prompt template for generating summaries. If not provided, uses the packaged dual-segment SNIP prompt.",
    )
    skill_file_read_tool_names: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SKILL_FILE_READ_TOOL_NAMES),
        description="Tool names treated as skill-file reads when capturing loaded skills into the durable skill_context channel.",
    )

    @field_validator("summary_prompt")
    @classmethod
    def validate_summary_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_summary_prompt_template(value)

    @model_validator(mode="after")
    def validate_retention_below_trigger(self) -> "SummarizationConfig":
        if self.enabled and self.trigger_tokens is not None:
            resolve_effective_compaction_policy(
                trigger_tokens=self.trigger_tokens,
                keep_tokens=self.keep.value,
                context_window_tokens=None,
            )
        return self


# Global configuration instance
_summarization_config: SummarizationConfig = SummarizationConfig()


def get_summarization_config() -> SummarizationConfig:
    """Get the current summarization configuration."""
    return _summarization_config


def set_summarization_config(config: SummarizationConfig) -> None:
    """Set the summarization configuration."""
    global _summarization_config
    _summarization_config = config


def load_summarization_config_from_dict(config_dict: dict) -> None:
    """Load summarization configuration from a dictionary."""
    global _summarization_config
    _summarization_config = SummarizationConfig(**config_dict)
