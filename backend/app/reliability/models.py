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


__all__ = ["ReliabilityReadiness", "ReliabilityReadinessStatus"]
