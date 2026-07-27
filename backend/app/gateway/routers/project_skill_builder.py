"""Project-scoped conversational Skill Builder HTTP contract."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.gateway.routers.project_assets import (
    ASSET_ERRORS,
    AssetItemResponse,
    AssetRoute,
    project_asset_context,
    raise_asset_domain,
)
from app.projects.context import ProjectContext
from app.shared_assets.errors import AssetStorageUnavailable
from app.shared_assets.skill_design_service import (
    CancelSkillDesignSession,
    CommitSkillDesignSession,
    CreateSkillDesignSession,
    SkillDesignClarificationResponse,
    SkillDesignClarificationTurn,
    SkillDesignCommitResult,
    SkillDesignDraftUpdateTurn,
    SkillDesignMessageTurn,
    SkillDesignService,
    SkillDesignSessionSummary,
    SkillDesignSessionView,
    SubmitSkillDesignTurn,
    ValidateSkillDesignSession,
)
from app.shared_assets.skill_service import SkillFileChange, SkillService
from deerflow.persistence.engine import get_session_factory
from deerflow.trace_context import generate_trace_id, get_current_trace_id

router = APIRouter(
    prefix="/api/projects/{project_id}/skill-builder/sessions",
    tags=["project-skill-builder"],
    route_class=AssetRoute,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateSkillDesignSessionRequest(_StrictModel):
    slug: str
    display_name: str
    idempotency_key: str


class SkillDesignMessageTurnRequest(_StrictModel):
    kind: Literal["message"]
    message: str


class SkillDesignClarificationResponseRequest(_StrictModel):
    version: Literal[1]
    kind: Literal["human_input_response"]
    source: str
    request_id: str
    response_kind: Literal["option", "text"]
    value: str
    option_id: str | None = None


class SkillDesignClarificationTurnRequest(_StrictModel):
    kind: Literal["clarification"]
    response: SkillDesignClarificationResponseRequest


class SkillDesignFileChangeRequest(_StrictModel):
    op: Literal["create", "replace", "delete"]
    path: str = Field(min_length=1, max_length=1024)
    content: str | None = None
    media_type: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_payload(self) -> SkillDesignFileChangeRequest:
        if self.op == "delete":
            if self.content is not None or self.media_type is not None:
                raise ValueError("delete accepts only op and path")
        elif self.content is None or self.media_type is None:
            raise ValueError("create and replace require content and media_type")
        return self


class SkillDesignDraftUpdateTurnRequest(_StrictModel):
    kind: Literal["draft_update"]
    expected_draft_checksum: str
    changes: list[SkillDesignFileChangeRequest] = Field(
        min_length=1,
        max_length=128,
    )


class SkillDesignTurnRequest(_StrictModel):
    input: Annotated[
        SkillDesignMessageTurnRequest | SkillDesignClarificationTurnRequest | SkillDesignDraftUpdateTurnRequest,
        Field(discriminator="kind"),
    ]
    expected_revision: int = Field(ge=1)
    idempotency_key: str


class SkillDesignValidateRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    expected_draft_checksum: str
    idempotency_key: str


class SkillDesignCommitRequest(SkillDesignValidateRequest):
    acknowledge_warnings: bool


class SkillDesignCancelRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str


class SkillDesignProgressItemResponse(_StrictModel):
    id: str
    label: str
    status: Literal["pending", "running", "completed", "failed"]


class SkillDesignMessageResponse(_StrictModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class SkillDesignClarificationOptionResponse(_StrictModel):
    id: str
    label: str
    value: str


class SkillDesignClarificationRequestResponse(_StrictModel):
    version: Literal[1]
    kind: Literal["human_input_request"]
    source: str
    request_id: str
    clarification_type: str
    title: str
    question: str
    context: str
    input_mode: Literal["free_text", "single_choice", "choice_with_other"]
    options: tuple[SkillDesignClarificationOptionResponse, ...]


class SkillDesignFileResponse(_StrictModel):
    path: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str
    encoding: Literal["utf-8"]
    content: str


class SkillDesignSecretRequirementResponse(_StrictModel):
    name: str
    optional: bool


class SkillDesignValidationResponse(_StrictModel):
    draft_checksum: str
    validated_at: datetime
    description: str
    frontmatter: dict[str, object]
    compatibility: str | None
    secret_requirements: tuple[SkillDesignSecretRequirementResponse, ...]
    scan_decision: Literal["allow", "warn"]
    scan_rule_ids: tuple[str, ...]
    scan_summary: dict[str, object]


class SkillDesignSessionItemResponse(_StrictModel):
    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: uuid.UUID
    slug: str
    display_name: str
    status: Literal[
        "interviewing",
        "generating",
        "awaiting_clarification",
        "draft_ready",
        "validated",
        "committing",
        "completed",
        "failed",
        "cancelled",
    ]
    revision: int = Field(ge=1)
    messages: tuple[SkillDesignMessageResponse, ...]
    active_clarification: SkillDesignClarificationRequestResponse | None
    progress: tuple[SkillDesignProgressItemResponse, ...]
    files: tuple[SkillDesignFileResponse, ...]
    draft_checksum: str | None
    validation: SkillDesignValidationResponse | None
    error_code: str | None
    error_message: str | None
    created_skill_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SkillDesignSessionSummaryResponse(_StrictModel):
    id: uuid.UUID
    slug: str
    display_name: str
    status: Literal[
        "interviewing",
        "generating",
        "awaiting_clarification",
        "draft_ready",
        "validated",
        "committing",
        "completed",
        "failed",
        "cancelled",
    ]
    updated_at: datetime


class SkillDesignSessionResponse(_StrictModel):
    data: SkillDesignSessionItemResponse
    request_id: str


class SkillDesignSessionListResponse(_StrictModel):
    data: list[SkillDesignSessionSummaryResponse]
    request_id: str


class SkillDesignCommitDataResponse(_StrictModel):
    session: SkillDesignSessionItemResponse
    skill: AssetItemResponse


class SkillDesignCommitResponse(_StrictModel):
    data: SkillDesignCommitDataResponse
    request_id: str


def _request_id() -> str:
    return get_current_trace_id() or generate_trace_id()


def get_skill_design_service(request: Request) -> SkillDesignService:
    existing = getattr(request.app.state, "skill_design_service", None)
    if isinstance(existing, SkillDesignService):
        return existing
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        raise_asset_domain(AssetStorageUnavailable(_request_id()))
    governance_sink = getattr(request.app.state, "shared_asset_audit_sink", None)
    if governance_sink is None:
        raise_asset_domain(AssetStorageUnavailable(_request_id()))
    generator = getattr(
        request.app.state,
        "skill_design_generation_service",
        None,
    )
    skill_service = SkillService(
        session_factory,
        governance_sink=governance_sink,
        quota=getattr(request.app.state, "project_quota_enforcer", None),
    )
    service = SkillDesignService(
        session_factory,
        generator=generator,
        skill_service=skill_service,
    )
    request.app.state.skill_design_service = service
    return service


def _turn(body: SkillDesignTurnRequest) -> SubmitSkillDesignTurn:
    turn = body.input
    if isinstance(turn, SkillDesignMessageTurnRequest):
        value = SkillDesignMessageTurn(kind="message", message=turn.message)
    elif isinstance(turn, SkillDesignClarificationTurnRequest):
        response = turn.response
        value = SkillDesignClarificationTurn(
            kind="clarification",
            response=SkillDesignClarificationResponse(
                version=1,
                kind="human_input_response",
                source=response.source,
                request_id=response.request_id,
                response_kind=response.response_kind,
                value=response.value,
                option_id=response.option_id,
            ),
        )
    else:
        value = SkillDesignDraftUpdateTurn(
            kind="draft_update",
            expected_draft_checksum=turn.expected_draft_checksum,
            changes=tuple(
                SkillFileChange(
                    op=change.op,
                    path=change.path,
                    content=change.content,
                    media_type=change.media_type,
                )
                for change in turn.changes
            ),
        )
    return SubmitSkillDesignTurn(
        input=value,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
    )


def _session_item(
    view: SkillDesignSessionView,
) -> SkillDesignSessionItemResponse:
    return SkillDesignSessionItemResponse.model_validate(
        view,
        from_attributes=True,
    )


def _session_response(
    view: SkillDesignSessionView,
    context: ProjectContext,
) -> SkillDesignSessionResponse:
    return SkillDesignSessionResponse(
        data=_session_item(view),
        request_id=context.request_id,
    )


def _summary_item(
    view: SkillDesignSessionSummary,
) -> SkillDesignSessionSummaryResponse:
    return SkillDesignSessionSummaryResponse.model_validate(
        view,
        from_attributes=True,
    )


def _commit_response(
    result: SkillDesignCommitResult,
    context: ProjectContext,
) -> SkillDesignCommitResponse:
    return SkillDesignCommitResponse(
        data=SkillDesignCommitDataResponse(
            session=_session_item(result.session),
            skill=AssetItemResponse.model_validate(
                result.skill,
                from_attributes=True,
            ),
        ),
        request_id=context.request_id,
    )


@router.post(
    "",
    response_model=SkillDesignSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_skill_design_session(
    body: CreateSkillDesignSessionRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignSessionResponse:
    try:
        view = await service.create(
            context,
            CreateSkillDesignSession(
                slug=body.slug,
                display_name=body.display_name,
                idempotency_key=body.idempotency_key,
            ),
        )
        return _session_response(view, context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get("", response_model=SkillDesignSessionListResponse)
async def list_skill_design_sessions(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignSessionListResponse:
    try:
        items = await service.list_incomplete(context)
        return SkillDesignSessionListResponse(
            data=[_summary_item(item) for item in items],
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get(
    "/{session_id}",
    response_model=SkillDesignSessionResponse,
)
async def get_skill_design_session(
    session_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignSessionResponse:
    try:
        return _session_response(await service.get(context, session_id), context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/turns",
    response_model=SkillDesignSessionResponse,
)
async def submit_skill_design_turn(
    session_id: uuid.UUID,
    body: SkillDesignTurnRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignSessionResponse:
    try:
        view = await service.submit_turn(
            context,
            session_id,
            _turn(body),
        )
        return _session_response(view, context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/validate",
    response_model=SkillDesignSessionResponse,
)
async def validate_skill_design_session(
    session_id: uuid.UUID,
    body: SkillDesignValidateRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignSessionResponse:
    try:
        view = await service.validate(
            context,
            session_id,
            ValidateSkillDesignSession(
                expected_revision=body.expected_revision,
                expected_draft_checksum=body.expected_draft_checksum,
                idempotency_key=body.idempotency_key,
            ),
        )
        return _session_response(view, context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/commit",
    response_model=SkillDesignCommitResponse,
)
async def commit_skill_design_session(
    session_id: uuid.UUID,
    body: SkillDesignCommitRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignCommitResponse:
    try:
        result = await service.commit(
            context,
            session_id,
            CommitSkillDesignSession(
                expected_revision=body.expected_revision,
                expected_draft_checksum=body.expected_draft_checksum,
                acknowledge_warnings=body.acknowledge_warnings,
                idempotency_key=body.idempotency_key,
            ),
        )
        return _commit_response(result, context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/cancel",
    response_model=SkillDesignSessionResponse,
)
async def cancel_skill_design_session(
    session_id: uuid.UUID,
    body: SkillDesignCancelRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignSessionResponse:
    try:
        view = await service.cancel(
            context,
            session_id,
            CancelSkillDesignSession(
                expected_revision=body.expected_revision,
                idempotency_key=body.idempotency_key,
            ),
        )
        return _session_response(view, context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


__all__ = [
    "CreateSkillDesignSessionRequest",
    "SkillDesignCancelRequest",
    "SkillDesignCommitRequest",
    "SkillDesignCommitResponse",
    "SkillDesignSessionListResponse",
    "SkillDesignSessionResponse",
    "SkillDesignTurnRequest",
    "SkillDesignValidateRequest",
    "get_skill_design_service",
    "router",
]
