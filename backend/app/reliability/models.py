"""Public, content-free M6 reliability models."""

from dataclasses import dataclass
from typing import Literal

ReliabilityReadinessStatus = Literal["ready", "degraded", "closed"]
ReliabilitySchemaState = Literal["ready", "unavailable"]


@dataclass(frozen=True, slots=True)
class ReliabilityReadiness:
    status: ReliabilityReadinessStatus
    database: str
    schema: str
    worker_fleet: str
    scheduler: str
    stream: str
    quota: str
    audit: str
    request_id: str
    role: str = "gateway"
    worker_count: int = 0
    worker_capacity: int = 0
    worker_oldest_heartbeat_age_seconds: int | None = None
    private_run_worker_fleet: str = "unavailable"
    private_run_worker_count: int = 0
    private_run_worker_capacity: int = 0
    scheduler_ownership: str = "unavailable"
    schema_state: ReliabilitySchemaState = "unavailable"
    run_skill_writer_mode: Literal["v4_reference", "legacy_v3"] = "v4_reference"
    run_skill_writer_artifact_version: str = ""
    run_skill_legacy_policy_digest: str = ""
    run_skill_writer_ready: bool = False


__all__ = ["ReliabilityReadiness", "ReliabilityReadinessStatus", "ReliabilitySchemaState"]
