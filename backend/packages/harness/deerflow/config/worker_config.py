from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_TEXT_DELTA_FLUSH_MS = 75


class WorkerStreamConfig(BaseModel):
    """Durable stream publication and wakeup policy."""

    model_config = ConfigDict(extra="forbid")

    text_delta_flush_ms: int = Field(
        default=DEFAULT_TEXT_DELTA_FLUSH_MS,
        ge=0,
        le=5000,
        description=("Root assistant text deltas are merged for at most this many milliseconds (or 4 KiB) before one durable frame is published. 0 disables coalescing and restores per-token frames."),
    )
    run_event_notify_enabled: bool = Field(
        default=True,
        description=("Queue PostgreSQL run_events NOTIFY wakeups after durable stream writes and start the Gateway LISTEN connection. False restores polling-only SSE wakeups."),
    )


class WorkerConfig(BaseModel):
    """Restart-required configuration for independent M6 Workers."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    poll_interval_seconds: float = Field(default=0.5, gt=0, le=30)
    lease_seconds: int = Field(default=90, ge=15, le=3600)
    heartbeat_seconds: int = Field(default=20, ge=1, le=1200)
    max_concurrent_jobs: int = Field(default=4, ge=1, le=128)
    shutdown_grace_seconds: int = Field(default=30, ge=1, le=600)
    retry_initial_seconds: int = Field(default=2, ge=1, le=3600)
    retry_max_seconds: int = Field(default=300, ge=1, le=86400)
    stream: WorkerStreamConfig = Field(
        default_factory=WorkerStreamConfig,
        description="Durable stream publication policy for this Worker.",
    )

    @model_validator(mode="after")
    def validate_intervals(self) -> Self:
        if self.heartbeat_seconds * 3 >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be less than one third of lease_seconds")
        if self.retry_initial_seconds > self.retry_max_seconds:
            raise ValueError("retry_initial_seconds must not exceed retry_max_seconds")
        return self


__all__ = ["DEFAULT_TEXT_DELTA_FLUSH_MS", "WorkerConfig", "WorkerStreamConfig"]
