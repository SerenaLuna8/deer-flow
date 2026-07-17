"""Public, content-free M6 reliability models."""

from dataclasses import dataclass
from typing import Literal

ReliabilityReadinessStatus = Literal["ready", "degraded", "closed"]


@dataclass(frozen=True, slots=True)
class ReliabilityReadiness:
    status: ReliabilityReadinessStatus
    database: str
    schema: str
    worker_fleet: str
    scheduler: str
    stream: str
    recovery: str
    quota: str
    audit: str
    request_id: str
    role: str = "gateway"
    worker_count: int = 0
    worker_capacity: int = 0
    worker_oldest_heartbeat_age_seconds: int | None = None
    scheduler_ownership: str = "unavailable"
    cutover: str = "unknown"


__all__ = ["ReliabilityReadiness", "ReliabilityReadinessStatus"]
