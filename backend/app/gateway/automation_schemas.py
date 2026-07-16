from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Self

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.automations.error_mapping import automation_http_exception
from app.automations.errors import AutomationInvalid
from app.automations.models import (
    AutomationChanges,
    AutomationCreate,
    AutomationRunView,
    AutomationView,
)
from deerflow.trace_context import generate_trace_id, get_current_trace_id


class StrictAutomationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrictAutomationRequest(StrictAutomationModel):
    pass


class StrictAutomationResponse(StrictAutomationModel):
    pass


class AutomationRoute(APIRoute):
    """Normalize every request/path/query/header validation failure."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                raise automation_http_exception(AutomationInvalid(request_id)) from None

        return handler


class AutomationCreateRequest(StrictAutomationRequest):
    title: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1)
    context_mode: Literal["fresh_thread_per_run", "reuse_thread"]
    thread_id: uuid.UUID | None = None
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"]
    schedule_type: Literal["once", "cron"]
    schedule_spec: dict[str, object]
    timezone: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_thread_mode(self) -> Self:
        if self.context_mode == "fresh_thread_per_run" and self.thread_id is not None:
            raise ValueError("fresh_thread_per_run does not accept thread_id")
        if self.context_mode == "reuse_thread" and self.thread_id is None:
            raise ValueError("reuse_thread requires thread_id")
        return self

    def to_command(self) -> AutomationCreate:
        return AutomationCreate(
            title=self.title,
            prompt=self.prompt,
            context_mode=self.context_mode,
            thread_id=None if self.thread_id is None else str(self.thread_id),
            agent_asset_id=self.agent_asset_id,
            agent_scope=self.agent_scope,
            schedule_type=self.schedule_type,
            schedule_spec=dict(self.schedule_spec),
            timezone=self.timezone,
        )


class AutomationPatchRequest(StrictAutomationRequest):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    prompt: str | None = Field(default=None, min_length=1)
    schedule_spec: dict[str, object] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if all(
            value is None
            for value in (
                self.title,
                self.prompt,
                self.schedule_spec,
                self.timezone,
            )
        ):
            raise ValueError("at least one change is required")
        return self

    def to_changes(self) -> AutomationChanges:
        return AutomationChanges(
            expected_version=self.expected_version,
            title=self.title,
            prompt=self.prompt,
            schedule_spec=self.schedule_spec,
            timezone=self.timezone,
        )


class AutomationVersionRequest(StrictAutomationRequest):
    expected_version: int = Field(ge=1)


class AutomationListQuery(StrictAutomationRequest):
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class AutomationResponse(StrictAutomationResponse):
    id: str
    thread_id: str | None
    context_mode: Literal["fresh_thread_per_run", "reuse_thread"]
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"]
    title: str
    prompt: str
    schedule_type: Literal["once", "cron"]
    schedule_spec: dict[str, object]
    timezone: str
    status: Literal["enabled", "paused", "completed", "failed", "cancelled"]
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_outcome: str | None
    last_error_code: str | None
    run_count: int = Field(ge=0)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "next_run_at",
        "last_run_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def require_offset(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime offset is required")
        return value

    @classmethod
    def from_view(cls, value: AutomationView) -> AutomationResponse:
        return cls(
            id=value.id,
            thread_id=value.thread_id,
            context_mode=value.context_mode,
            agent_asset_id=value.agent_asset_id,
            agent_scope=value.agent_scope,
            title=value.title,
            prompt=value.prompt,
            schedule_type=value.schedule_type,
            schedule_spec=dict(value.schedule_spec),
            timezone=value.timezone,
            status=value.status,
            next_run_at=value.next_run_at,
            last_run_at=value.last_run_at,
            last_outcome=value.last_outcome,
            last_error_code=value.last_error_code,
            run_count=value.run_count,
            version=value.version,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


class AutomationListResponse(StrictAutomationResponse):
    items: list[AutomationResponse]


class AutomationDeleteResponse(StrictAutomationResponse):
    id: str
    deleted: bool


class AutomationRunResponse(StrictAutomationResponse):
    id: str
    automation_id: str
    automation_version: int = Field(ge=1)
    scheduled_for: datetime
    trigger: Literal["scheduled", "manual"]
    status: Literal[
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
    thread_id: str | None
    run_id: str | None
    error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "scheduled_for",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def require_offset(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime offset is required")
        return value

    @classmethod
    def from_view(cls, value: AutomationRunView) -> AutomationRunResponse:
        return cls(
            id=value.id,
            automation_id=value.automation_id,
            automation_version=value.automation_version,
            scheduled_for=value.scheduled_for,
            trigger=value.trigger,
            status=value.status,
            thread_id=value.thread_id,
            run_id=value.run_id,
            error_code=value.error_code,
            started_at=value.started_at,
            finished_at=value.finished_at,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


class AutomationRunListResponse(StrictAutomationResponse):
    items: list[AutomationRunResponse]


class AutomationReadinessResponse(StrictAutomationResponse):
    status: Literal["ready", "migration_required", "unavailable"]
    code: str
    scheduler_enabled: bool
    scheduler_status: Literal["disabled", "stopped", "running", "ownership_lost"]
    project_private_work_ready: bool
    automation_cutover_ready: bool
    request_id: str


__all__ = [
    "AutomationCreateRequest",
    "AutomationDeleteResponse",
    "AutomationListQuery",
    "AutomationListResponse",
    "AutomationPatchRequest",
    "AutomationReadinessResponse",
    "AutomationResponse",
    "AutomationRoute",
    "AutomationRunListResponse",
    "AutomationRunResponse",
    "AutomationVersionRequest",
    "StrictAutomationModel",
    "StrictAutomationRequest",
    "StrictAutomationResponse",
]
