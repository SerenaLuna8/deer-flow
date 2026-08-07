"""Configuration for the final document-based Memory mechanism."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MemoryConfig(BaseModel):
    """Six-field runtime contract for project-scoped Memory."""

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
    idle_seal_minutes: int = Field(
        default=1440,
        ge=0,
        le=10_080,
        description="Idle minutes before a thread is sealed for capture (0 disables)",
    )
    episode_retention_days: int = Field(
        default=365,
        ge=0,
        le=3_650,
        description="Days archived episodes stay searchable (0 keeps them forever)",
    )

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
