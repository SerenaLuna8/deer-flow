from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

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
from app.private_work.memory_service import (
    PrivateMemoryService,
    PrivateMemoryStatus,
    PrivateMemoryV2Service,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.agents.memory.storage import ProjectMemorySnapshot
from deerflow.config.app_config import AppConfig
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.private_work.memory_v2_management import (
    MemoryV2CandidateView,
    MemoryV2EvidenceView,
    MemoryV2FactDetail,
    MemoryV2FactView,
    MemoryV2HardForgetResult,
    MemoryV2RevisionView,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/memory",
    tags=["project-memory"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)

Namespace = Annotated[str, Query(min_length=1, max_length=128)]


def _strip_filter(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


FactQuery = Annotated[
    str,
    BeforeValidator(_strip_filter),
    StringConstraints(min_length=1, max_length=200),
]
FactCategory = Annotated[
    str,
    BeforeValidator(_strip_filter),
    StringConstraints(min_length=1, max_length=32),
]


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


class ProjectMemoryV2Revision(StrictPrivateWorkResponse):
    id: uuid.UUID
    fact_id: uuid.UUID = Field(alias="factId")
    revision_number: int = Field(alias="revisionNumber", ge=1)
    revision_sequence: int = Field(alias="revisionSequence", ge=1)
    content: str | None = Field(default=None, max_length=16_000)
    content_digest: str = Field(alias="contentDigest", min_length=64, max_length=64)
    category: str = Field(min_length=1, max_length=32)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    valid_from: datetime | None = Field(alias="validFrom")
    valid_to: datetime | None = Field(alias="validTo")
    last_confirmed_at: datetime | None = Field(alias="lastConfirmedAt")
    changed_by: Literal["user", "system", "consolidator"] = Field(alias="changedBy")
    source_candidate_id: uuid.UUID | None = Field(alias="sourceCandidateId")
    supersedes_revision_id: uuid.UUID | None = Field(alias="supersedesRevisionId")
    change_reason: str | None = Field(alias="changeReason", max_length=64)
    content_erased_at: datetime | None = Field(alias="contentErasedAt")
    created_at: datetime = Field(alias="createdAt")


class ProjectMemoryV2Fact(StrictPrivateWorkResponse):
    id: uuid.UUID
    fact_kind: str = Field(alias="factKind", min_length=1, max_length=32)
    status: Literal["active", "disabled", "superseded", "deleted"]
    version: int = Field(ge=1)
    disabled_at: datetime | None = Field(alias="disabledAt")
    superseded_at: datetime | None = Field(alias="supersededAt")
    deleted_at: datetime | None = Field(alias="deletedAt")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    current_revision: ProjectMemoryV2Revision = Field(alias="currentRevision")


class ProjectMemoryV2Candidate(StrictPrivateWorkResponse):
    id: uuid.UUID
    candidate_type: str = Field(alias="candidateType", min_length=1, max_length=32)
    content: str | None = Field(default=None, max_length=16_000)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    retention_class: Literal["permanent", "durable", "ephemeral"] = Field(alias="retentionClass")
    sensitivity: Literal["normal", "sensitive", "restricted"]
    status: Literal["pending", "accepted", "rejected", "superseded"]
    decision_reason: str | None = Field(alias="decisionReason", max_length=64)
    decided_at: datetime | None = Field(alias="decidedAt")
    content_erased_at: datetime | None = Field(alias="contentErasedAt")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class ProjectMemoryV2Evidence(StrictPrivateWorkResponse):
    id: uuid.UUID
    fact_id: uuid.UUID = Field(alias="factId")
    revision_id: uuid.UUID = Field(alias="revisionId")
    source_candidate_id: uuid.UUID | None = Field(alias="sourceCandidateId")
    source_item_id: uuid.UUID | None = Field(alias="sourceItemId")
    thread_id: str | None = Field(alias="threadId", max_length=64)
    run_id: str | None = Field(alias="runId", max_length=64)
    run_event_sequence: int | None = Field(alias="runEventSequence", ge=0)
    evidence_excerpt: str | None = Field(alias="evidenceExcerpt", max_length=4_000)
    trust_class: Literal["direct", "derived", "untrusted"] = Field(alias="trustClass")
    source_erased_at: datetime | None = Field(alias="sourceErasedAt")
    created_at: datetime = Field(alias="createdAt")


class ProjectMemoryV2FactsResponse(StrictPrivateWorkResponse):
    namespace: str
    items: list[ProjectMemoryV2Fact] = Field(max_length=100)


class ProjectMemoryV2CandidatesResponse(StrictPrivateWorkResponse):
    namespace: str
    items: list[ProjectMemoryV2Candidate] = Field(max_length=100)


class ProjectMemoryV2StatusResponse(StrictPrivateWorkResponse):
    enabled: bool
    pipeline_mode: Literal["off", "shadow", "consolidate", "v2"] = Field(
        alias="pipelineMode",
    )
    search_enabled: bool = Field(alias="searchEnabled")
    injection_enabled: bool = Field(alias="injectionEnabled")
    consolidation_interval_minutes: int = Field(
        alias="consolidationIntervalMinutes",
        ge=15,
        le=1_440,
    )
    candidate_retention_days: int = Field(
        alias="candidateRetentionDays",
        ge=1,
        le=365,
    )


class ProjectMemoryV2FactDetailResponse(StrictPrivateWorkResponse):
    namespace: str
    fact: ProjectMemoryV2Fact
    revisions: list[ProjectMemoryV2Revision] = Field(max_length=1_000)
    evidence: list[ProjectMemoryV2Evidence] = Field(max_length=5_000)


class ProjectMemoryV2CandidateDecisionRequest(StrictPrivateWorkRequest):
    expected_updated_at: str = Field(
        alias="expectedUpdatedAt",
        min_length=1,
        max_length=64,
    )

    @field_validator("expected_updated_at")
    @classmethod
    def validate_expected_updated_at(cls, value: str) -> str:
        return _require_timezone(value, allow_empty=False)

    def parsed_expected_updated_at(self) -> datetime:
        return datetime.fromisoformat(self.expected_updated_at.replace("Z", "+00:00"))


class ProjectMemoryV2FactUpdateRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(alias="expectedVersion", ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=16_000)
    category: str | None = Field(default=None, min_length=1, max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    reason: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_change(self):
        if self.content is None and self.category is None and self.confidence is None:
            raise ValueError("at least one Fact field must change")
        return self


class ProjectMemoryV2FactStateRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(alias="expectedVersion", ge=1)


class ProjectMemoryV2HardForgetResponse(StrictPrivateWorkResponse):
    fact_id: uuid.UUID = Field(alias="factId")
    version: int = Field(ge=2)
    status: Literal["deleted"]
    erased_candidates: int = Field(alias="erasedCandidates", ge=0)
    erased_revisions: int = Field(alias="erasedRevisions", ge=1)
    erased_evidence: int = Field(alias="erasedEvidence", ge=0)
    erased_source_items: int = Field(alias="erasedSourceItems", ge=0)


def _service(request: Request) -> PrivateMemoryService:
    service = getattr(request.app.state, "project_memory_service", None)
    if isinstance(service, PrivateMemoryService):
        return service
    service = PrivateMemoryService(get_session_factory())
    request.app.state.project_memory_service = service
    return service


def _v2_service(request: Request) -> PrivateMemoryV2Service:
    service = getattr(request.app.state, "project_memory_v2_service", None)
    if isinstance(service, PrivateMemoryV2Service):
        return service
    service = PrivateMemoryV2Service(
        get_session_factory(),
        source_hmac=AuditHmacKeyring.from_environment().memory_source_ref,
    )
    request.app.state.project_memory_v2_service = service
    return service


def _revision_response(row: MemoryV2RevisionView) -> ProjectMemoryV2Revision:
    return ProjectMemoryV2Revision.model_validate(
        {
            "id": row.id,
            "factId": row.fact_id,
            "revisionNumber": row.revision_number,
            "revisionSequence": row.revision_sequence,
            "content": row.content,
            "contentDigest": row.content_digest,
            "category": row.category,
            "confidence": row.confidence,
            "validFrom": row.valid_from,
            "validTo": row.valid_to,
            "lastConfirmedAt": row.last_confirmed_at,
            "changedBy": row.changed_by,
            "sourceCandidateId": row.source_candidate_id,
            "supersedesRevisionId": row.supersedes_revision_id,
            "changeReason": row.change_reason,
            "contentErasedAt": row.content_erased_at,
            "createdAt": row.created_at,
        }
    )


def _fact_response(row: MemoryV2FactView) -> ProjectMemoryV2Fact:
    return ProjectMemoryV2Fact.model_validate(
        {
            "id": row.id,
            "factKind": row.fact_kind,
            "status": row.status,
            "version": row.version,
            "disabledAt": row.disabled_at,
            "supersededAt": row.superseded_at,
            "deletedAt": row.deleted_at,
            "createdAt": row.created_at,
            "updatedAt": row.updated_at,
            "currentRevision": _revision_response(row.current_revision),
        }
    )


def _candidate_response(
    row: MemoryV2CandidateView,
) -> ProjectMemoryV2Candidate:
    return ProjectMemoryV2Candidate.model_validate(
        {
            "id": row.id,
            "candidateType": row.candidate_type,
            "content": row.content,
            "confidence": row.confidence,
            "retentionClass": row.retention_class,
            "sensitivity": row.sensitivity,
            "status": row.status,
            "decisionReason": row.decision_reason,
            "decidedAt": row.decided_at,
            "contentErasedAt": row.content_erased_at,
            "createdAt": row.created_at,
            "updatedAt": row.updated_at,
        }
    )


def _evidence_response(row: MemoryV2EvidenceView) -> ProjectMemoryV2Evidence:
    return ProjectMemoryV2Evidence.model_validate(
        {
            "id": row.id,
            "factId": row.fact_id,
            "revisionId": row.revision_id,
            "sourceCandidateId": row.source_candidate_id,
            "sourceItemId": row.source_item_id,
            "threadId": row.thread_id,
            "runId": row.run_id,
            "runEventSequence": row.run_event_sequence,
            "evidenceExcerpt": row.evidence_excerpt,
            "trustClass": row.trust_class,
            "sourceErasedAt": row.source_erased_at,
            "createdAt": row.created_at,
        }
    )


def _fact_detail_response(
    row: MemoryV2FactDetail,
    *,
    namespace: str,
) -> ProjectMemoryV2FactDetailResponse:
    return ProjectMemoryV2FactDetailResponse(
        namespace=namespace,
        fact=_fact_response(row.fact),
        revisions=[_revision_response(item) for item in row.revisions],
        evidence=[_evidence_response(item) for item in row.evidence],
    )


def _hard_forget_response(
    row: MemoryV2HardForgetResult,
) -> ProjectMemoryV2HardForgetResponse:
    return ProjectMemoryV2HardForgetResponse.model_validate(
        {
            "factId": row.fact_id,
            "version": row.version,
            "status": row.status,
            "erasedCandidates": row.erased_candidates,
            "erasedRevisions": row.erased_revisions,
            "erasedEvidence": row.erased_evidence,
            "erasedSourceItems": row.erased_source_items,
        }
    )


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


@router.get("/v2/facts", response_model=ProjectMemoryV2FactsResponse)
async def list_project_memory_v2_facts(
    request: Request,
    namespace: Namespace = "default",
    status: Literal["active", "disabled", "all"] = "active",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    query: Annotated[FactQuery | None, Query()] = None,
    category: Annotated[FactCategory | None, Query()] = None,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2FactsResponse:
    statuses: tuple[Literal["active", "disabled"], ...] = ("active", "disabled") if status == "all" else (status,)
    filters = {key: value for key, value in (("query", query), ("category", category)) if value is not None}
    rows = await _call(
        _v2_service(request).list_facts(
            context,
            namespace=namespace,
            statuses=statuses,
            limit=limit,
            offset=offset,
            **filters,
        )
    )
    return ProjectMemoryV2FactsResponse(
        namespace=namespace,
        items=[_fact_response(row) for row in rows],
    )


@router.get("/v2/status", response_model=ProjectMemoryV2StatusResponse)
async def get_project_memory_v2_status(
    _context: PrivateWorkContext = Depends(private_work_context),
    config: AppConfig = Depends(get_current_agent_runtime_config),
) -> ProjectMemoryV2StatusResponse:
    memory = config.memory
    return ProjectMemoryV2StatusResponse.model_validate(
        {
            "enabled": memory.enabled,
            "pipelineMode": memory.pipeline_mode,
            "searchEnabled": memory.search_enabled,
            "injectionEnabled": memory.injection_enabled,
            "consolidationIntervalMinutes": memory.consolidation_interval_minutes,
            "candidateRetentionDays": memory.candidate_retention_days,
        }
    )


@router.get("/v2/export")
async def export_project_memory_v2(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
):
    stream = await _call(
        _v2_service(request).open_export(
            context,
            namespace=namespace,
        )
    )
    return StreamingResponse(
        stream,
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": ('attachment; filename="deer-flow-memory-v2.ndjson"'),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/v2/candidates", response_model=ProjectMemoryV2CandidatesResponse)
async def list_project_memory_v2_candidates(
    request: Request,
    namespace: Namespace = "default",
    status: Literal[
        "pending",
        "accepted",
        "rejected",
        "superseded",
        "all",
    ] = "pending",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2CandidatesResponse:
    statuses = ("pending", "accepted", "rejected", "superseded") if status == "all" else (status,)
    rows = await _call(
        _v2_service(request).list_candidates(
            context,
            namespace=namespace,
            statuses=statuses,
            limit=limit,
            offset=offset,
        )
    )
    return ProjectMemoryV2CandidatesResponse(
        namespace=namespace,
        items=[_candidate_response(row) for row in rows],
    )


@router.get(
    "/v2/facts/{fact_id}",
    response_model=ProjectMemoryV2FactDetailResponse,
)
async def get_project_memory_v2_fact(
    fact_id: uuid.UUID,
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2FactDetailResponse:
    row = await _call(
        _v2_service(request).get_fact(
            context,
            fact_id,
            namespace=namespace,
        )
    )
    return _fact_detail_response(row, namespace=namespace)


@router.post(
    "/v2/candidates/{candidate_id}/accept",
    response_model=ProjectMemoryV2Fact,
)
async def accept_project_memory_v2_candidate(
    candidate_id: uuid.UUID,
    body: ProjectMemoryV2CandidateDecisionRequest,
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2Fact:
    row = await _call(
        _v2_service(request).accept_candidate(
            context,
            candidate_id,
            namespace=namespace,
            expected_updated_at=body.parsed_expected_updated_at(),
        )
    )
    return _fact_response(row)


@router.post(
    "/v2/candidates/{candidate_id}/reject",
    response_model=ProjectMemoryV2Candidate,
)
async def reject_project_memory_v2_candidate(
    candidate_id: uuid.UUID,
    body: ProjectMemoryV2CandidateDecisionRequest,
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2Candidate:
    row = await _call(
        _v2_service(request).reject_candidate(
            context,
            candidate_id,
            namespace=namespace,
            expected_updated_at=body.parsed_expected_updated_at(),
        )
    )
    return _candidate_response(row)


@router.patch("/v2/facts/{fact_id}", response_model=ProjectMemoryV2Fact)
async def update_project_memory_v2_fact(
    fact_id: uuid.UUID,
    body: ProjectMemoryV2FactUpdateRequest,
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2Fact:
    row = await _call(
        _v2_service(request).revise_fact(
            context,
            fact_id,
            namespace=namespace,
            expected_version=body.expected_version,
            content=body.content,
            category=body.category,
            confidence=body.confidence,
            reason=body.reason,
        )
    )
    return _fact_response(row)


@router.post("/v2/facts/{fact_id}/disable", response_model=ProjectMemoryV2Fact)
async def disable_project_memory_v2_fact(
    fact_id: uuid.UUID,
    body: ProjectMemoryV2FactStateRequest,
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2Fact:
    row = await _call(
        _v2_service(request).set_fact_enabled(
            context,
            fact_id,
            namespace=namespace,
            expected_version=body.expected_version,
            enabled=False,
        )
    )
    return _fact_response(row)


@router.post("/v2/facts/{fact_id}/restore", response_model=ProjectMemoryV2Fact)
async def restore_project_memory_v2_fact(
    fact_id: uuid.UUID,
    body: ProjectMemoryV2FactStateRequest,
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2Fact:
    row = await _call(
        _v2_service(request).set_fact_enabled(
            context,
            fact_id,
            namespace=namespace,
            expected_version=body.expected_version,
            enabled=True,
        )
    )
    return _fact_response(row)


@router.post(
    "/v2/facts/{fact_id}/hard-forget",
    response_model=ProjectMemoryV2HardForgetResponse,
)
async def hard_forget_project_memory_v2_fact(
    fact_id: uuid.UUID,
    body: ProjectMemoryV2FactStateRequest,
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryV2HardForgetResponse:
    row = await _call(
        _v2_service(request).hard_forget_fact(
            context,
            fact_id,
            namespace=namespace,
            expected_version=body.expected_version,
        )
    )
    return _hard_forget_response(row)
