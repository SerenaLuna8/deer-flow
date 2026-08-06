"""Configuration for the final document-based Memory mechanism."""

from pydantic import BaseModel, ConfigDict, Field


class MemoryConfig(BaseModel):
    """Four-field runtime contract for project-scoped Memory."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="Whether to enable memory mechanism",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name used by Dream (None selects the default model)",
    )
    dream_interval_minutes: int = Field(
        default=120,
        ge=15,
        le=1440,
        description="Interval for admitting automatic Dream jobs",
    )
    max_injection_tokens: int = Field(
        default=2000,
        ge=100,
        le=8000,
        description="Maximum tokens to use for memory injection",
    )


# Global configuration instance
_memory_config: MemoryConfig = MemoryConfig()


def get_memory_config() -> MemoryConfig:
    """Get the current memory configuration."""
    return _memory_config


def set_memory_config(config: MemoryConfig) -> None:
    """Set the memory configuration."""
    global _memory_config
    _memory_config = config


def load_memory_config_from_dict(config_dict: dict) -> None:
    """Load memory configuration from a dictionary."""
    global _memory_config
    _memory_config = MemoryConfig(**config_dict)
