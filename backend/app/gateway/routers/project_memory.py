"""Owner-private project Memory document API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Self

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AwareDatetime, Field, model_validator

from app.gateway.deps import (
    get_memory_dream_prepare_service,
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
from app.private_work.errors import PrivateWorkError, PrivateWorkInvalid
from app.private_work.memory_dream_prepare_service import MemoryDreamPrepareService
from app.private_work.memory_service import PrivateMemoryDocumentService
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.private_work.memory_document_repository import (
    MAX_MEMORY_UNIFIED_DIFF_CHARS,
    MemoryDocumentVersionRecord,
    memory_document_diff_preview,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/memory",
    tags=["project-memory"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)


class ProjectMemoryInjectionAdvisoryResponse(StrictPrivateWorkResponse):
    basis: Literal["current_non_continuation"]
    status: Literal["eligible", "skipped_over_budget", "inactive"]
    reason: Literal[
        "within_budget",
        "over_budget",
        "platform_disabled",
        "account_disabled",
        "no_document",
    ]

    @model_validator(mode="after")
    def validate_advisory_contract(self) -> Self:
        valid = {
            ("eligible", "within_budget"),
            ("skipped_over_budget", "over_budget"),
            ("inactive", "platform_disabled"),
            ("inactive", "account_disabled"),
            ("inactive", "no_document"),
        }
        if (self.status, self.reason) not in valid:
            raise ValueError("Memory injection advisory fields do not match")
        return self


class ProjectMemoryDocumentResponse(StrictPrivateWorkResponse):
    content: str = Field(max_length=16_000)
    version: int = Field(ge=0)
    updated_at: datetime | None = Field(alias="updatedAt")
    pending_count: int = Field(alias="pendingCount", ge=0)
    dream_running: bool = Field(alias="dreamRunning")
    injection_status: Literal["ok", "skipped_over_budget"] = Field(alias="injectionStatus")
    injection_advisory: ProjectMemoryInjectionAdvisoryResponse | None = Field(
        default=None,
        alias="injectionAdvisory",
        exclude_if=lambda value: value is None,
    )


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
    unified_diff: str = Field(
        alias="unifiedDiff",
        max_length=MAX_MEMORY_UNIFIED_DIFF_CHARS,
    )
    diff_truncated: bool | None = Field(
        default=None,
        alias="diffTruncated",
        exclude_if=lambda value: value is None,
    )


class ProjectMemoryEpisodeItem(StrictPrivateWorkResponse):
    id: uuid.UUID
    thread_id: str = Field(alias="threadId", max_length=64)
    origin: Literal["snip", "tool"]
    tagged_text: str = Field(alias="taggedText", max_length=1_000)
    occurred_at: datetime = Field(alias="occurredAt")
    created_at: datetime = Field(alias="createdAt")


class ProjectMemoryEpisodesResponse(StrictPrivateWorkResponse):
    items: list[ProjectMemoryEpisodeItem] = Field(max_length=50)


class ProjectMemoryEpisodesCursorResponse(ProjectMemoryEpisodesResponse):
    next_cursor: str | None = Field(alias="nextCursor")


class ProjectMemoryPendingItem(StrictPrivateWorkResponse):
    sequence: int = Field(ge=1)
    origin: Literal["snip", "tool"]
    tagged_text: str = Field(alias="taggedText", max_length=1_000)
    created_at: datetime = Field(alias="createdAt")


class ProjectMemoryPendingResponse(StrictPrivateWorkResponse):
    items: list[ProjectMemoryPendingItem] = Field(max_length=100)


class ProjectMemoryDreamRequest(StrictPrivateWorkRequest):
    """Bodyless immediate Dream retained only for the project Memory page."""


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


class ProjectMemoryDreamPrepareRequest(StrictPrivateWorkRequest):
    thread_id: str = Field(alias="threadId", min_length=1, max_length=64)
    operation_id: uuid.UUID = Field(alias="operationId")


class ProjectMemoryDreamPrepareAdmissionResponse(StrictPrivateWorkResponse):
    disposition: Literal["queued", "already_running"]
    job_id: uuid.UUID = Field(alias="jobId")
    status: Literal["queued", "running", "succeeded", "cancelled", "failed"]


class ProjectMemoryDreamPrepareStatusResponse(StrictPrivateWorkResponse):
    job_id: uuid.UUID = Field(alias="jobId")
    status: Literal["queued", "running", "succeeded", "cancelled", "failed"]
    phase: Literal[
        "queued",
        "draining",
        "verifying",
        "dream_admitted",
        "succeeded",
        "cancelled",
        "failed",
    ]
    compacted_passes: int = Field(alias="compactedPasses", ge=0)
    dream_job_id: uuid.UUID | None = Field(alias="dreamJobId")
    history_count: int | None = Field(default=None, alias="historyCount", ge=0, le=20)
    admission_kind: Literal["history", "budget_rewrite"] | None = Field(
        default=None,
        alias="admissionKind",
    )
    result_disposition: Literal[
        "queued",
        "already_running",
        "nothing_pending",
        "cancelled",
        "failed",
    ] = Field(alias="resultDisposition")
    cancel_requested: bool = Field(alias="cancelRequested")
    public_error_code: str | None = Field(
        default=None,
        alias="publicErrorCode",
        pattern=r"^[A-Z][A-Z0-9_]{0,63}$",
    )
    updated_at: datetime = Field(alias="updatedAt")


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
        audit=getattr(request.app.state, "operational_audit_sink", None),
    )
    request.app.state.project_memory_service = service
    return service


def _prepare_status(value) -> Literal["queued", "running", "succeeded", "cancelled", "failed"]:
    if value.job_status in {"leased", "running", "retry_wait"}:
        return "running"
    if value.job_status == "dead":
        return "failed"
    return value.job_status


def _prepare_response(value) -> ProjectMemoryDreamPrepareStatusResponse:
    return ProjectMemoryDreamPrepareStatusResponse(
        jobId=value.job_id,
        status=_prepare_status(value),
        phase=value.phase,
        compactedPasses=value.compacted_passes,
        dreamJobId=value.dream_job_id,
        historyCount=value.history_count,
        admissionKind=value.admission_kind,
        resultDisposition=value.result_disposition,
        cancelRequested=value.cancel_requested,
        publicErrorCode=value.public_error_code,
        updatedAt=value.updated_at,
    )


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


def _version_detail(
    row: MemoryDocumentVersionRecord,
    *,
    include_truncation: bool,
) -> ProjectMemoryVersionDetailResponse:
    summary = _version_summary(row)
    preview, truncated = memory_document_diff_preview(
        row.unified_diff,
        legacy_utf16=not include_truncation,
    )
    return ProjectMemoryVersionDetailResponse(
        **summary.model_dump(by_alias=True),
        content=row.content,
        unifiedDiff=preview,
        diffTruncated=truncated if include_truncation else None,
    )


@router.get("", response_model=ProjectMemoryDocumentResponse)
async def get_project_memory(
    request: Request,
    response: Response,
    injection_contract: Literal["advisory_v1"] | None = Query(
        default=None,
        alias="injectionContract",
    ),
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryDocumentResponse:
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    if injection_contract == "advisory_v1":
        state, injection_advisory = await _call(service.get_with_injection_advisory(context))
        injection_status = injection_advisory.legacy_status
    else:
        state, injection_status = await _call(service.get(context))
        injection_advisory = None
    return ProjectMemoryDocumentResponse(
        content=state.document.content,
        version=state.document.version,
        updatedAt=state.document.updated_at,
        pendingCount=state.pending_count,
        dreamRunning=state.document.active_dream_job_id is not None,
        injectionStatus=injection_status,
        injectionAdvisory=(
            ProjectMemoryInjectionAdvisoryResponse(
                basis="current_non_continuation",
                status=injection_advisory.status,
                reason=injection_advisory.reason,
            )
            if injection_contract == "advisory_v1"
            else None
        ),
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


@router.get(
    "/episodes",
    response_model=ProjectMemoryEpisodesResponse | ProjectMemoryEpisodesCursorResponse,
)
async def list_project_memory_episodes(
    request: Request,
    response: Response,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    tags: list[Literal["permanent", "durable", "ephemeral", "correction"]] | None = Query(default=None),
    before: AwareDatetime | None = Query(default=None),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    pagination: Literal["keyset_v1"] | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryEpisodesResponse | ProjectMemoryEpisodesCursorResponse:
    response.headers["Cache-Control"] = "no-store"
    normalized_q = q.strip() if isinstance(q, str) else None
    if cursor is not None and (pagination is None or before is not None or normalized_q):
        raise private_work_http_exception(PrivateWorkInvalid(context.request_id))
    page = await _call(
        _service(request).list_episodes(
            context,
            q=normalized_q or None,
            tags=tuple(dict.fromkeys(tags or ())),
            cursor=cursor,
            limit=limit,
            before=before,
        )
    )
    items = [
        ProjectMemoryEpisodeItem(
            id=row.id,
            threadId=row.thread_id,
            origin=row.origin,
            taggedText=row.tagged_text,
            occurredAt=row.occurred_at,
            createdAt=row.created_at,
        )
        for row in page.items
    ]
    if pagination is None:
        return ProjectMemoryEpisodesResponse(items=items)
    return ProjectMemoryEpisodesCursorResponse(
        items=items,
        nextCursor=page.next_cursor,
    )


@router.get(
    "/versions/{version}",
    response_model=ProjectMemoryVersionDetailResponse,
)
async def get_project_memory_version(
    version: int,
    request: Request,
    response: Response,
    response_contract: Literal["preview_v1"] | None = Query(
        default=None,
        alias="responseContract",
    ),
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryVersionDetailResponse:
    response.headers["Cache-Control"] = "no-store"
    row = await _call(_service(request).get_version(context, version))
    return _version_detail(
        row,
        include_truncation=response_contract == "preview_v1",
    )


@router.post("/dream", response_model=ProjectMemoryDreamResponse)
async def dream_project_memory(
    request: Request,
    body: ProjectMemoryDreamRequest | None = None,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryDreamResponse:
    # Rolling compatibility is deliberately fail-closed: the strict empty body
    # rejects legacy ``threadId`` callers with 422. Thread-scoped `/Dream` must
    # use the durable ``/dream-preparations`` contract; this endpoint never
    # executes compaction/model work in Gateway.
    del body
    result = await _call(_service(request).dream(context))
    return ProjectMemoryDreamResponse(
        disposition=result.disposition,
        jobId=result.job_id,
        historyCount=result.history_count,
        admissionKind=("budget_rewrite" if result.admission_kind == "budget_rewrite" else None),
    )


@router.post(
    "/dream-preparations",
    response_model=ProjectMemoryDreamPrepareAdmissionResponse,
    status_code=202,
)
async def admit_project_memory_dream_preparation(
    body: ProjectMemoryDreamPrepareRequest,
    request: Request,
    response: Response,
    context: PrivateWorkContext = Depends(private_work_context),
    service: MemoryDreamPrepareService = Depends(get_memory_dream_prepare_service),
) -> ProjectMemoryDreamPrepareAdmissionResponse:
    response.headers["Cache-Control"] = "no-store"
    result = await _call(
        service.admit(
            context,
            thread_id=body.thread_id,
            operation_id=body.operation_id,
        )
    )
    return ProjectMemoryDreamPrepareAdmissionResponse(
        disposition=result.disposition,
        jobId=result.record.job_id,
        status=_prepare_status(result.record),
    )


@router.get(
    "/dream-preparations/latest",
    response_model=ProjectMemoryDreamPrepareStatusResponse,
)
async def get_latest_project_memory_dream_preparation(
    request: Request,
    response: Response,
    thread_id: str = Query(alias="threadId", min_length=1, max_length=64),
    context: PrivateWorkContext = Depends(private_work_context),
    service: MemoryDreamPrepareService = Depends(get_memory_dream_prepare_service),
) -> ProjectMemoryDreamPrepareStatusResponse:
    response.headers["Cache-Control"] = "no-store"
    return _prepare_response(
        await _call(
            service.read_latest(
                context,
                thread_id=thread_id,
            )
        )
    )


@router.get(
    "/dream-preparations/{job_id}",
    response_model=ProjectMemoryDreamPrepareStatusResponse,
)
async def get_project_memory_dream_preparation(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    context: PrivateWorkContext = Depends(private_work_context),
    service: MemoryDreamPrepareService = Depends(get_memory_dream_prepare_service),
) -> ProjectMemoryDreamPrepareStatusResponse:
    response.headers["Cache-Control"] = "no-store"
    return _prepare_response(await _call(service.read(context, job_id)))


@router.post(
    "/dream-preparations/{job_id}/cancel",
    response_model=ProjectMemoryDreamPrepareStatusResponse,
)
async def cancel_project_memory_dream_preparation(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    context: PrivateWorkContext = Depends(private_work_context),
    service: MemoryDreamPrepareService = Depends(get_memory_dream_prepare_service),
) -> ProjectMemoryDreamPrepareStatusResponse:
    response.headers["Cache-Control"] = "no-store"
    return _prepare_response(await _call(service.cancel(context, job_id)))


@router.post(
    "/versions/{version}/restore",
    response_model=ProjectMemoryVersionDetailResponse,
)
async def restore_project_memory_version(
    version: int,
    body: ProjectMemoryRestoreRequest,
    request: Request,
    response: Response,
    response_contract: Literal["preview_v1"] | None = Query(
        default=None,
        alias="responseContract",
    ),
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
    return _version_detail(
        row,
        include_truncation=response_contract == "preview_v1",
    )


__all__ = ["router"]
