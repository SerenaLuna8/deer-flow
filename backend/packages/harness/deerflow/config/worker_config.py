from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkerConfig(BaseModel):
    """Restart-required configuration for independent M6 Workers."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    poll_interval_seconds: float = Field(default=0.5, gt=0, le=30)
    lease_seconds: int = Field(default=90, ge=15, le=3600)
    heartbeat_seconds: int = Field(default=20, ge=1, le=1200)
    max_concurrent_jobs: int = Field(default=4, ge=1, le=128)
    shutdown_grace_seconds: int = Field(default=30, ge=1, le=600)
    default_max_attempts: int = Field(default=3, ge=1, le=20)
    retry_initial_seconds: int = Field(default=2, ge=1, le=3600)
    retry_max_seconds: int = Field(default=300, ge=1, le=86400)

    @model_validator(mode="after")
    def validate_intervals(self) -> Self:
        if self.heartbeat_seconds * 3 >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be less than one third of lease_seconds")
        if self.retry_initial_seconds > self.retry_max_seconds:
            raise ValueError("retry_initial_seconds must not exceed retry_max_seconds")
        return self


__all__ = ["WorkerConfig"]
