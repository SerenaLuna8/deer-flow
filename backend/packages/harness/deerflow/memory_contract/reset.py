"""Pure account Memory reset result contracts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryResetSettledDream:
    project_id: uuid.UUID
    job_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class MemoryResetCounts:
    scopes_reset: int
    history_entries: int
    documents: int
    versions: int
    dream_runs: int
    prepare_runs: int
    snapshots: int
    episodes: int
    jobs_cancelled: int
    affected_project_ids: tuple[uuid.UUID, ...]
    settled_dreams: tuple[MemoryResetSettledDream, ...]


__all__ = ["MemoryResetCounts", "MemoryResetSettledDream"]
