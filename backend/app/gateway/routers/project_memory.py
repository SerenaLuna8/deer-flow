from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ConfigDict, Field, field_validator, model_validator

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.private_work_schemas import (
    PrivateWorkRoute,
    StrictPrivateWorkRequest,
    StrictPrivateWorkResponse,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import PrivateWorkError
from app.private_work.memory_service import PrivateMemoryService, PrivateMemoryStatus
from deerflow.agents.memory.storage import ProjectMemorySnapshot
from deerflow.persistence.engine import get_session_factory

router = APIRouter(
    prefix="/api/projects/{project_id}/memory",
    tags=["project-memory"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)

Namespace = Annotated[str, Query(min_length=1, max_length=128)]


def _require_timezone(value: str, *, allow_empty: bool) -> str:
    if allow_empty and not value:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


class ProjectMemorySummary(StrictPrivateWorkResponse):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(max_length=20_000)
    updated_at: str = Field(alias="updatedAt", max_length=64)

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: str) -> str:
        return _require_timezone(value, allow_empty=True)


class ProjectMemoryUser(StrictPrivateWorkResponse):
    model_config = ConfigDict(extra="forbid", strict=True)

    work_context: ProjectMemorySummary = Field(alias="workContext")
    personal_context: ProjectMemorySummary = Field(alias="personalContext")
    top_of_mind: ProjectMemorySummary = Field(alias="topOfMind")


class ProjectMemoryHistory(StrictPrivateWorkResponse):
    model_config = ConfigDict(extra="forbid", strict=True)

    recent_months: ProjectMemorySummary = Field(alias="recentMonths")
    earlier_context: ProjectMemorySummary = Field(alias="earlierContext")
    long_term_background: ProjectMemorySummary = Field(alias="longTermBackground")


class ProjectMemoryFact(StrictPrivateWorkResponse):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=36, max_length=36)
    content: str = Field(min_length=1, max_length=10_000)
    category: str = Field(min_length=1, max_length=32)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    created_at: str = Field(alias="createdAt", min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)
    source_thread_id: str | None = Field(
        default=None,
        alias="sourceThreadId",
        min_length=1,
        max_length=64,
    )
    source_run_id: str | None = Field(
        default=None,
        alias="sourceRunId",
        min_length=1,
        max_length=64,
    )

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except ValueError:
            raise ValueError("fact id must be a UUID") from None
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: str) -> str:
        return _require_timezone(value, allow_empty=False)

    @model_validator(mode="after")
    def validate_source_pair(self):
        if self.source_run_id is not None and self.source_thread_id is None:
            raise ValueError("sourceRunId requires sourceThreadId")
        return self


class ProjectMemoryDocument(StrictPrivateWorkResponse):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal["1.0"]
    last_updated: str = Field(alias="lastUpdated", max_length=64)
    user: ProjectMemoryUser
    history: ProjectMemoryHistory
    facts: list[ProjectMemoryFact] = Field(max_length=500)

    @field_validator("last_updated")
    @classmethod
    def validate_last_updated(cls, value: str) -> str:
        return _require_timezone(value, allow_empty=True)

    @model_validator(mode="after")
    def validate_unique_fact_ids(self):
        fact_ids = [fact.id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact ids must be unique")
        return self


class ProjectMemoryResponse(StrictPrivateWorkResponse):
    namespace: str
    version: int
    memory: ProjectMemoryDocument


class ProjectMemoryStatusResponse(StrictPrivateWorkResponse):
    namespace: str
    version: int
    fact_count: int
    last_updated: str


class ProjectMemoryImportRequest(StrictPrivateWorkRequest):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_version: int = Field(ge=0)
    memory: ProjectMemoryDocument


class ProjectMemoryCreateRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=10_000)
    category: str = Field(default="context", max_length=32)
    confidence: float = Field(default=0.8, ge=0, le=1)


class ProjectMemoryUpdateRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(ge=0)
    content: str | None = Field(default=None, min_length=1, max_length=10_000)
    category: str | None = Field(default=None, min_length=1, max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ProjectMemoryDeleteRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(ge=0)


def _service(request: Request) -> PrivateMemoryService:
    service = getattr(request.app.state, "project_memory_service", None)
    if isinstance(service, PrivateMemoryService):
        return service
    service = PrivateMemoryService(get_session_factory())
    request.app.state.project_memory_service = service
    return service


def _snapshot_response(
    snapshot: ProjectMemorySnapshot,
    namespace: str,
) -> ProjectMemoryResponse:
    return ProjectMemoryResponse(
        namespace=namespace,
        version=snapshot.version,
        memory=snapshot.memory,
    )


async def _call(operation):
    try:
        return await operation
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None


@router.get("", response_model=ProjectMemoryResponse)
async def list_project_memory(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(_service(request).list(context, namespace=namespace))
    return _snapshot_response(snapshot, namespace)


@router.get("/status", response_model=ProjectMemoryStatusResponse)
async def get_project_memory_status(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryStatusResponse:
    status: PrivateMemoryStatus = await _call(_service(request).status(context, namespace=namespace))
    return ProjectMemoryStatusResponse(
        namespace=status.namespace,
        version=status.version,
        fact_count=status.fact_count,
        last_updated=status.last_updated,
    )


@router.post("/reload", response_model=ProjectMemoryResponse)
async def reload_project_memory(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    del request, namespace, context
    raise HTTPException(
        status_code=501,
        detail="Project Memory reload is not supported; reads already use PostgreSQL.",
    )


@router.get("/export", response_model=ProjectMemoryDocument)
async def export_project_memory(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryDocument:
    memory = await _call(_service(request).export(context, namespace=namespace))
    return ProjectMemoryDocument.model_validate(memory)


@router.post("/import", response_model=ProjectMemoryResponse)
async def import_project_memory(
    request: Request,
    body: ProjectMemoryImportRequest,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(
        _service(request).import_memory(
            context,
            body.memory.model_dump(mode="json", by_alias=True),
            namespace=namespace,
            expected_version=body.expected_version,
        )
    )
    return _snapshot_response(snapshot, namespace)


@router.post("/facts", response_model=ProjectMemoryResponse)
async def create_project_memory_fact(
    request: Request,
    body: ProjectMemoryCreateRequest,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(
        _service(request).create_fact(
            context,
            namespace=namespace,
            expected_version=body.expected_version,
            content=body.content,
            category=body.category,
            confidence=body.confidence,
        )
    )
    return _snapshot_response(snapshot, namespace)


@router.patch("/facts/{fact_id}", response_model=ProjectMemoryResponse)
async def update_project_memory_fact(
    request: Request,
    fact_id: str,
    body: ProjectMemoryUpdateRequest,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(
        _service(request).update(
            context,
            fact_id,
            namespace=namespace,
            expected_version=body.expected_version,
            content=body.content,
            category=body.category,
            confidence=body.confidence,
        )
    )
    return _snapshot_response(snapshot, namespace)


@router.delete("/facts/{fact_id}", response_model=ProjectMemoryResponse)
async def delete_project_memory_fact(
    request: Request,
    fact_id: str,
    body: ProjectMemoryDeleteRequest,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(
        _service(request).delete(
            context,
            fact_id,
            namespace=namespace,
            expected_version=body.expected_version,
        )
    )
    return _snapshot_response(snapshot, namespace)
