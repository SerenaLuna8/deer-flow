from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

AutomationContextMode = Literal["fresh_thread_per_run", "reuse_thread"]
AutomationAgentScope = Literal["project", "system"]
AutomationScheduleType = Literal["once", "cron"]
AutomationStatus = Literal["enabled", "paused", "completed", "failed", "cancelled"]
AutomationTrigger = Literal["scheduled", "manual"]
AutomationRunStatus = Literal[
    "queued",
    "launching",
    "running",
    "success",
    "failed",
    "skipped",
    "interrupted",
    "cancelled",
    "rejected",
]


@dataclass(frozen=True, slots=True)
class AutomationCreate:
    title: str
    prompt: str
    context_mode: AutomationContextMode
    thread_id: str | None
    agent_asset_id: uuid.UUID
    agent_scope: AutomationAgentScope
    schedule_type: AutomationScheduleType
    schedule_spec: Mapping[str, object]
    timezone: str


@dataclass(frozen=True, slots=True)
class AutomationChanges:
    expected_version: int
    title: str | None = None
    prompt: str | None = None
    schedule_spec: Mapping[str, object] | None = None
    timezone: str | None = None


@dataclass(frozen=True, slots=True)
class AutomationView:
    id: str
    thread_id: str | None
    context_mode: AutomationContextMode
    agent_asset_id: uuid.UUID
    agent_scope: AutomationAgentScope
    title: str
    prompt: str
    schedule_type: AutomationScheduleType
    schedule_spec: Mapping[str, object]
    timezone: str
    status: AutomationStatus
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_outcome: str | None
    last_error_code: str | None
    run_count: int
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AutomationRunView:
    id: str
    automation_id: str
    automation_version: int
    scheduled_for: datetime
    trigger: AutomationTrigger
    status: AutomationRunStatus
    thread_id: str | None
    run_id: str | None
    error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "AutomationAgentScope",
    "AutomationChanges",
    "AutomationContextMode",
    "AutomationCreate",
    "AutomationRunStatus",
    "AutomationRunView",
    "AutomationScheduleType",
    "AutomationStatus",
    "AutomationTrigger",
    "AutomationView",
]
