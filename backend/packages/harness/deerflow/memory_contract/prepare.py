"""Pure contracts for durable thread-scoped Dream preparation."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from deerflow.memory_contract.common import MemoryDocumentScope

MemoryDreamPrepareAdmissionDisposition = Literal["queued", "already_running"]
MemoryDreamPreparePhase = Literal[
    "queued",
    "draining",
    "verifying",
    "dream_admitted",
    "succeeded",
    "cancelled",
    "failed",
]
MemoryDreamPrepareResultDisposition = Literal[
    "queued",
    "already_running",
    "nothing_pending",
    "cancelled",
    "failed",
]


class MemoryDreamPrepareNotFound(LookupError):
    """The exact owner-private preparation is unavailable."""


class MemoryDreamPrepareConflict(RuntimeError):
    """The preparation state or lease fence no longer matches."""


@dataclass(frozen=True, slots=True)
class MemoryDreamPrepareRecord:
    job_id: uuid.UUID
    thread_id: str
    phase: MemoryDreamPreparePhase
    compacted_passes: int
    dream_job_id: uuid.UUID | None
    history_count: int | None
    admission_kind: Literal["history", "budget_rewrite"] | None
    result_disposition: MemoryDreamPrepareResultDisposition
    job_status: str
    public_error_code: str | None
    cancel_requested: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryDreamPrepareAdmission:
    disposition: MemoryDreamPrepareAdmissionDisposition
    record: MemoryDreamPrepareRecord


def memory_dream_prepare_idempotency_key(
    scope: MemoryDocumentScope,
    operation_id: uuid.UUID,
) -> str:
    if type(scope) is not MemoryDocumentScope or not isinstance(operation_id, uuid.UUID):
        raise TypeError("Dream preparation idempotency authority is invalid")
    return hashlib.sha256(
        "\x1f".join(
            (
                "memory_dream_prepare_v1",
                str(scope.project_id),
                scope.owner_user_id,
                scope.namespace,
                str(operation_id),
            )
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "MemoryDreamPrepareAdmission",
    "MemoryDreamPrepareAdmissionDisposition",
    "MemoryDreamPrepareConflict",
    "MemoryDreamPrepareNotFound",
    "MemoryDreamPreparePhase",
    "MemoryDreamPrepareRecord",
    "MemoryDreamPrepareResultDisposition",
    "memory_dream_prepare_idempotency_key",
]
