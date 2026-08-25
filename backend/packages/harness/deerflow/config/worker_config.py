from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_TEXT_DELTA_FLUSH_MS = 75
RELEASE_WORKER_PROCESS_COUNT = 1
RELEASE_WORKER_MAX_CONCURRENT_JOBS = 8
MIN_V4_MATERIALIZATION_INFLIGHT_BYTES = 100 * 1024 * 1024
# Release-calibrated process RSS envelopes for the compatibility decoders.
# The full 12,922-file ppt-master mixed profile measured a 1,216,462,848-byte
# Worker delta after every source query moved behind its reservation. Round both
# legacy codecs up to 1.5 GiB so either read is exclusive with fixed headroom;
# the independent v4 aggregate below remains unchanged.
LEGACY_V2_MATERIALIZATION_ENVELOPE_BYTES = 1536 * 1024 * 1024
LEGACY_V3_MATERIALIZATION_ENVELOPE_BYTES = 1536 * 1024 * 1024
LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES = LEGACY_V3_MATERIALIZATION_ENVELOPE_BYTES
DEFAULT_MATERIALIZATION_MAX_INFLIGHT_BYTES = LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES
# Keep the previously accepted v4 aggregate unchanged when the total gate is
# enlarged to admit one legacy decoder envelope.
DEFAULT_MATERIALIZATION_V4_MAX_INFLIGHT_BYTES = 256 * 1024 * 1024
DEFAULT_MATERIALIZATION_BATCH_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MATERIALIZATION_BATCH_MAX_FILES = 50
DEFAULT_MATERIALIZATION_ORPHAN_GRACE_SECONDS = 300


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
    max_concurrent_jobs: int = Field(
        default=RELEASE_WORKER_MAX_CONCURRENT_JOBS,
        ge=1,
        le=128,
    )
    shutdown_grace_seconds: int = Field(default=30, ge=1, le=600)
    retry_initial_seconds: int = Field(default=2, ge=1, le=3600)
    retry_max_seconds: int = Field(default=300, ge=1, le=86400)
    materialization_max_inflight_bytes: int = Field(
        default=DEFAULT_MATERIALIZATION_MAX_INFLIGHT_BYTES,
        ge=MIN_V4_MATERIALIZATION_INFLIGHT_BYTES,
        description=("Per-Worker-process total weighted Skill materialization byte budget. This is not a fleet-wide memory limit."),
    )
    materialization_v4_max_inflight_bytes: int = Field(
        default=DEFAULT_MATERIALIZATION_V4_MAX_INFLIGHT_BYTES,
        ge=MIN_V4_MATERIALIZATION_INFLIGHT_BYTES,
        description=("Per-Worker-process v4-only aggregate nested inside the total materialization byte budget."),
    )
    materialization_batch_max_bytes: int = Field(
        default=DEFAULT_MATERIALIZATION_BATCH_MAX_BYTES,
        gt=0,
        description="Maximum ordinary v4 content-query bytes per batch.",
    )
    materialization_batch_max_files: int = Field(
        default=DEFAULT_MATERIALIZATION_BATCH_MAX_FILES,
        gt=0,
        description="Maximum v4 content-query rows per ordinary batch.",
    )
    materialization_orphan_grace_seconds: int = Field(
        default=DEFAULT_MATERIALIZATION_ORPHAN_GRACE_SECONDS,
        ge=30,
        le=86400,
        description=("Minimum durable owner age before a startup reaper may reconcile an inactive Run Skill materialization tree."),
    )
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
        if self.materialization_v4_max_inflight_bytes > self.materialization_max_inflight_bytes:
            raise ValueError("materialization_v4_max_inflight_bytes must not exceed materialization_max_inflight_bytes")
        if self.materialization_batch_max_bytes > self.materialization_v4_max_inflight_bytes:
            raise ValueError("materialization_batch_max_bytes must not exceed materialization_v4_max_inflight_bytes")
        return self


def require_supported_worker_release_topology(config: WorkerConfig) -> None:
    """Fail startup outside the single-process, capacity-8 release envelope."""

    if type(config) is not WorkerConfig:
        raise TypeError("worker release topology requires WorkerConfig")
    if config.max_concurrent_jobs != RELEASE_WORKER_MAX_CONCURRENT_JOBS:
        raise ValueError(f"worker release topology requires max_concurrent_jobs={RELEASE_WORKER_MAX_CONCURRENT_JOBS}")
    if config.materialization_v4_max_inflight_bytes < MIN_V4_MATERIALIZATION_INFLIGHT_BYTES:
        raise ValueError("worker materialization budget does not cover the legal v4 maximum")
    if config.materialization_max_inflight_bytes < LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES:
        raise ValueError("worker materialization budget does not cover the legacy envelope")
    if config.materialization_max_inflight_bytes != DEFAULT_MATERIALIZATION_MAX_INFLIGHT_BYTES:
        raise ValueError("worker release topology requires the accepted total materialization byte cap")
    if config.materialization_v4_max_inflight_bytes != DEFAULT_MATERIALIZATION_V4_MAX_INFLIGHT_BYTES:
        raise ValueError("worker release topology requires the accepted v4 aggregate byte cap")
    if config.materialization_batch_max_bytes != DEFAULT_MATERIALIZATION_BATCH_MAX_BYTES:
        raise ValueError("worker release topology requires the accepted v4 batch byte cap")
    if config.materialization_batch_max_files != DEFAULT_MATERIALIZATION_BATCH_MAX_FILES:
        raise ValueError("worker release topology requires the accepted v4 batch file cap")
    if config.materialization_v4_max_inflight_bytes > config.materialization_max_inflight_bytes:
        raise ValueError("worker v4 materialization budget exceeds the total budget")


__all__ = [
    "DEFAULT_MATERIALIZATION_BATCH_MAX_BYTES",
    "DEFAULT_MATERIALIZATION_BATCH_MAX_FILES",
    "DEFAULT_MATERIALIZATION_MAX_INFLIGHT_BYTES",
    "DEFAULT_MATERIALIZATION_ORPHAN_GRACE_SECONDS",
    "DEFAULT_MATERIALIZATION_V4_MAX_INFLIGHT_BYTES",
    "DEFAULT_TEXT_DELTA_FLUSH_MS",
    "LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES",
    "LEGACY_V2_MATERIALIZATION_ENVELOPE_BYTES",
    "LEGACY_V3_MATERIALIZATION_ENVELOPE_BYTES",
    "MIN_V4_MATERIALIZATION_INFLIGHT_BYTES",
    "RELEASE_WORKER_MAX_CONCURRENT_JOBS",
    "RELEASE_WORKER_PROCESS_COUNT",
    "WorkerConfig",
    "WorkerStreamConfig",
    "require_supported_worker_release_topology",
]
