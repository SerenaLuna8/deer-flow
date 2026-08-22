import uuid
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class ModelConfig(BaseModel):
    """Config section for a model"""

    name: str = Field(..., description="Unique name for the model")
    display_name: str | None = Field(..., default_factory=lambda: None, description="Display name for the model")
    description: str | None = Field(..., default_factory=lambda: None, description="Description for the model")
    use: str = Field(
        ...,
        description="Class path of the model provider(e.g. langchain_openai.ChatOpenAI)",
    )
    model: str = Field(..., description="Model name")
    model_config = ConfigDict(extra="allow")
    _system_model_config_id: uuid.UUID | None = PrivateAttr(default=None)
    _system_model_payload_checksum: str | None = PrivateAttr(default=None)
    _system_model_secret_generation_id: uuid.UUID | None = PrivateAttr(
        default=None,
    )
    _system_model_secret_envelope_digest: str | None = PrivateAttr(default=None)
    _system_provider_adapter: str | None = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def reject_removed_pricing_metadata(cls, value: object) -> object:
        """Model definitions carry capabilities and usage, never money metadata."""

        if isinstance(value, Mapping) and "pricing" in value:
            raise ValueError("model pricing metadata is not supported")
        return value

    @property
    def system_provider_adapter(self) -> str | None:
        """Return the exact database adapter name without serializing it."""

        return self._system_provider_adapter

    use_responses_api: bool | None = Field(
        default=None,
        description="Whether to route OpenAI ChatOpenAI calls through the /v1/responses API",
    )
    output_version: str | None = Field(
        default=None,
        description="Structured output version for OpenAI responses content, e.g. responses/v1",
    )
    supports_thinking: bool = Field(default_factory=lambda: False, description="Whether the model supports thinking")
    supports_reasoning_effort: bool = Field(default_factory=lambda: False, description="Whether the model supports reasoning effort")
    when_thinking_enabled: dict | None = Field(
        default_factory=lambda: None,
        description="Extra settings to be passed to the model when thinking is enabled",
    )
    when_thinking_disabled: dict | None = Field(
        default_factory=lambda: None,
        description="Extra settings to be passed to the model when thinking is disabled",
    )
    supports_vision: bool = Field(default_factory=lambda: False, description="Whether the model supports vision/image inputs")
    stream_chunk_timeout: float | None = Field(
        default=None,
        description=(
            "Maximum seconds to wait between successive streaming chunks before "
            "langchain-openai raises StreamChunkTimeoutError. None means use the "
            "factory default (240s for OpenAI-compatible clients). Tune higher for "
            "reasoning models with long thinking pauses; lower for latency-sensitive "
            "interactive endpoints. Has no effect on non-OpenAI-compatible providers."
        ),
    )
    thinking: dict | None = Field(
        default_factory=lambda: None,
        description=(
            "Thinking settings for the model. If provided, these settings will be passed to the model when thinking is enabled. "
            "This is a shortcut for `when_thinking_enabled` and will be merged with `when_thinking_enabled` if both are provided."
        ),
    )
