"""Owner-private project Memory document API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Self

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AwareDatetime, Field, model_validator

from app.gateway.deps import (
    get_current_agent_runtime_config,
    private_work_context,
    require_project_private_open,
)
from app.gateway.private_work_schemas import (
    PrivateWorkRoute,
    StrictPrivateWorkRequest,
    StrictPrivateWorkResponse,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import PrivateWorkError
from app.private_work.memory_service import PrivateMemoryDocumentService
from deerflow.config.app_config import AppConfig
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentVersionRecord,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/memory",
    tags=["project-memory"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)


class ProjectMemoryDocumentResponse(StrictPrivateWorkResponse):
    content: str = Field(max_length=16_000)
    version: int = Field(ge=0)
    updated_at: datetime | None = Field(alias="updatedAt")
    pending_count: int = Field(alias="pendingCount", ge=0)
    dream_running: bool = Field(alias="dreamRunning")
    injection_status: Literal["ok", "skipped_over_budget"] = Field(alias="injectionStatus")


class ProjectMemoryVersionSummary(StrictPrivateWorkResponse):
    version: int = Field(ge=1)
    trigger: Literal["auto_dream", "manual_dream", "restore", "budget_rewrite"]
    history_count: int | None = Field(default=None, alias="historyCount", ge=0, le=20)
    changed: bool
    needs_review: bool = Field(alias="needsReview")
    created_at: datetime = Field(alias="createdAt")

    @model_validator(mode="after")
    def validate_history_contract(self) -> Self:
        if self.trigger == "restore":
            valid = self.history_count is None
        elif self.trigger == "budget_rewrite":
            valid = self.history_count == 0
        else:
            valid = self.history_count is not None and self.history_count >= 1
        if not valid:
            raise ValueError("Memory version history count does not match its trigger")
        return self


class ProjectMemoryVersionsResponse(StrictPrivateWorkResponse):
    items: list[ProjectMemoryVersionSummary] = Field(max_length=100)


class ProjectMemoryVersionDetailResponse(ProjectMemoryVersionSummary):
    content: str = Field(max_length=16_000)
    unified_diff: str = Field(alias="unifiedDiff", max_length=64_000)


class ProjectMemoryEpisodeItem(StrictPrivateWorkResponse):
    id: uuid.UUID
    thread_id: str = Field(alias="threadId", max_length=64)
    origin: Literal["snip", "tool"]
    tagged_text: str = Field(alias="taggedText", max_length=1_000)
    occurred_at: datetime = Field(alias="occurredAt")
    created_at: datetime = Field(alias="createdAt")


class ProjectMemoryEpisodesResponse(StrictPrivateWorkResponse):
    items: list[ProjectMemoryEpisodeItem] = Field(max_length=50)


class ProjectMemoryPendingItem(StrictPrivateWorkResponse):
    sequence: int = Field(ge=1)
    origin: Literal["snip", "tool"]
    tagged_text: str = Field(alias="taggedText", max_length=1_000)
    created_at: datetime = Field(alias="createdAt")


class ProjectMemoryPendingResponse(StrictPrivateWorkResponse):
    items: list[ProjectMemoryPendingItem] = Field(max_length=100)


class ProjectMemoryDreamRequest(StrictPrivateWorkRequest):
    thread_id: str | None = Field(
        default=None,
        alias="threadId",
        min_length=1,
        max_length=64,
    )


class ProjectMemoryDreamResponse(StrictPrivateWorkResponse):
    disposition: Literal["queued", "already_running", "nothing_pending"]
    job_id: uuid.UUID | None = Field(alias="jobId")
    history_count: int = Field(alias="historyCount", ge=0, le=20)
    admission_kind: Literal["budget_rewrite"] | None = Field(
        default=None,
        alias="admissionKind",
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_admission_contract(self) -> Self:
        if self.disposition == "nothing_pending":
            valid = self.job_id is None and self.history_count == 0 and self.admission_kind is None
        elif self.admission_kind == "budget_rewrite":
            valid = self.job_id is not None and self.history_count == 0
        else:
            valid = self.job_id is not None and self.history_count >= 1
        if not valid:
            raise ValueError("Dream admission fields do not match their disposition")
        return self


class ProjectMemoryRestoreRequest(StrictPrivateWorkRequest):
    expected_current_version: int = Field(
        alias="expectedCurrentVersion",
        ge=0,
    )


def _service(request: Request) -> PrivateMemoryDocumentService:
    service = getattr(request.app.state, "project_memory_service", None)
    if isinstance(service, PrivateMemoryDocumentService):
        return service
    service = PrivateMemoryDocumentService(
        get_session_factory(),
        dream_archive_barrier=getattr(
            request.app.state,
            "project_chat_control_service",
            None,
        ),
    )
    request.app.state.project_memory_service = service
    return service


async def _call(operation):
    try:
        return await operation
    except PrivateWorkError as error:
        raise private_work_http_exception(error) from None


def _version_summary(
    row: MemoryDocumentVersionRecord,
) -> ProjectMemoryVersionSummary:
    return ProjectMemoryVersionSummary(
        version=row.version,
        trigger=row.trigger,
        historyCount=row.history_count,
        changed=bool(row.unified_diff),
        needsReview=row.needs_review,
        createdAt=row.created_at,
    )


@router.get("", response_model=ProjectMemoryDocumentResponse)
async def get_project_memory(
    request: Request,
    response: Response,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryDocumentResponse:
    response.headers["Cache-Control"] = "no-store"
    state, injection_status = await _call(_service(request).get(context))
    return ProjectMemoryDocumentResponse(
        content=state.document.content,
        version=state.document.version,
        updatedAt=state.document.updated_at,
        pendingCount=state.pending_count,
        dreamRunning=state.document.active_dream_job_id is not None,
        injectionStatus=injection_status,
    )


@router.get("/versions", response_model=ProjectMemoryVersionsResponse)
async def list_project_memory_versions(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryVersionsResponse:
    response.headers["Cache-Control"] = "no-store"
    rows = await _call(
        _service(request).list_versions(
            context,
            limit=limit,
            offset=offset,
        )
    )
    return ProjectMemoryVersionsResponse(items=[_version_summary(row) for row in rows])


@router.get("/pending", response_model=ProjectMemoryPendingResponse)
async def list_project_memory_pending(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryPendingResponse:
    response.headers["Cache-Control"] = "no-store"
    rows = await _call(
        _service(request).list_pending(
            context,
            limit=limit,
            offset=offset,
        )
    )
    return ProjectMemoryPendingResponse(
        items=[
            ProjectMemoryPendingItem(
                sequence=row.sequence,
                origin=row.origin,
                taggedText=row.tagged_text,
                createdAt=row.created_at,
            )
            for row in rows
        ]
    )


@router.get("/episodes", response_model=ProjectMemoryEpisodesResponse)
async def list_project_memory_episodes(
    request: Request,
    response: Response,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    tags: list[Literal["permanent", "durable", "ephemeral", "correction"]] | None = Query(default=None),
    before: AwareDatetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryEpisodesResponse:
    response.headers["Cache-Control"] = "no-store"
    normalized_q = q.strip() if isinstance(q, str) else None
    rows = await _call(
        _service(request).list_episodes(
            context,
            q=normalized_q or None,
            tags=tuple(dict.fromkeys(tags or ())),
            before=before,
            limit=limit,
        )
    )
    return ProjectMemoryEpisodesResponse(
        items=[
            ProjectMemoryEpisodeItem(
                id=row.id,
                threadId=row.thread_id,
                origin=row.origin,
                taggedText=row.tagged_text,
                occurredAt=row.occurred_at,
                createdAt=row.created_at,
            )
            for row in rows
        ]
    )


@router.get(
    "/versions/{version}",
    response_model=ProjectMemoryVersionDetailResponse,
)
async def get_project_memory_version(
    version: int,
    request: Request,
    response: Response,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryVersionDetailResponse:
    response.headers["Cache-Control"] = "no-store"
    row = await _call(_service(request).get_version(context, version))
    summary = _version_summary(row)
    return ProjectMemoryVersionDetailResponse(
        **summary.model_dump(by_alias=True),
        content=row.content,
        unifiedDiff=row.unified_diff,
    )


@router.post("/dream", response_model=ProjectMemoryDreamResponse)
async def dream_project_memory(
    request: Request,
    body: ProjectMemoryDreamRequest | None = None,
    context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_current_agent_runtime_config),
) -> ProjectMemoryDreamResponse:
    result = await _call(
        _service(request).dream(
            context,
            thread_id=None if body is None else body.thread_id,
            app_config=config,
        )
    )
    return ProjectMemoryDreamResponse(
        disposition=result.disposition,
        jobId=result.job_id,
        historyCount=result.history_count,
        admissionKind=("budget_rewrite" if result.admission_kind == "budget_rewrite" else None),
    )


@router.post(
    "/versions/{version}/restore",
    response_model=ProjectMemoryVersionDetailResponse,
)
async def restore_project_memory_version(
    version: int,
    body: ProjectMemoryRestoreRequest,
    request: Request,
    response: Response,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryVersionDetailResponse:
    response.headers["Cache-Control"] = "no-store"
    row = await _call(
        _service(request).restore(
            context,
            target_version=version,
            expected_current_version=body.expected_current_version,
        )
    )
    summary = _version_summary(row)
    return ProjectMemoryVersionDetailResponse(
        **summary.model_dump(by_alias=True),
        content=row.content,
        unifiedDiff=row.unified_diff,
    )


__all__ = ["router"]
