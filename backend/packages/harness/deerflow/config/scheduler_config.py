from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SchedulerConfig(BaseModel):
    """Restart-required configuration for the embedded M5 poller."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable automatic polling; manual project Automation remains available when disabled.")
    poll_interval_seconds: int = Field(default=5, ge=1, le=300, description="Seconds between due-occurrence polls.")
    lease_seconds: int = Field(default=120, ge=1, le=3600, description="Occurrence admission lease; this is not an Agent execution lease.")
    max_concurrent_runs: int = Field(default=3, ge=1, le=32, description="Global active Automation occurrence limit shared by scheduled and manual triggers.")
    min_once_delay_seconds: int = Field(default=60, ge=0, le=86400, description="Minimum future offset accepted for one-time schedules.")

    @model_validator(mode="after")
    def validate_scheduler_timing(self) -> Self:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if self.lease_seconds <= self.poll_interval_seconds:
            raise ValueError("lease_seconds must exceed poll_interval_seconds")
        if self.max_concurrent_runs <= 0:
            raise ValueError("max_concurrent_runs must be positive")
        if self.min_once_delay_seconds < 0:
            raise ValueError("min_once_delay_seconds must be non-negative")
        return self
