"""Configuration for conversation summarization."""

from string import Formatter
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ContextSizeType = Literal["fraction", "tokens", "messages"]
DEFAULT_SKILL_FILE_READ_TOOL_NAMES: tuple[str, ...] = ("read_file", "read", "view", "cat")
_SUMMARY_PROMPT_CONTRACT_ERROR = "summary_prompt must be a valid format template whose only replacement field is {messages}"


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
    """Context size specification for trigger or keep parameters."""

    type: ContextSizeType = Field(description="Type of context size specification")
    value: int | float = Field(description="Value for the context size specification")

    def to_tuple(self) -> tuple[ContextSizeType, int | float]:
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
    trigger: ContextSize | list[ContextSize] | None = Field(
        default=None,
        description="One or more thresholds that trigger summarization. When any threshold is met, summarization runs. "
        "Examples: {'type': 'messages', 'value': 50} triggers at 50 messages, "
        "{'type': 'tokens', 'value': 4000} triggers at 4000 tokens, "
        "{'type': 'fraction', 'value': 0.8} triggers at 80% of model's max input tokens",
    )
    keep: ContextSize = Field(
        default_factory=lambda: ContextSize(type="messages", value=20),
        description="Context retention policy after summarization. Specifies how much history to preserve. "
        "Examples: {'type': 'messages', 'value': 20} keeps 20 messages, "
        "{'type': 'tokens', 'value': 3000} keeps 3000 tokens, "
        "{'type': 'fraction', 'value': 0.3} keeps 30% of model's max input tokens",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=4000,
        description="Maximum tokens to keep when preparing messages for summarization. Pass null to skip trimming.",
    )
    summary_prompt: str | None = Field(
        default=None,
        description="Custom prompt template for generating summaries. If not provided, uses the default LangChain prompt.",
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
