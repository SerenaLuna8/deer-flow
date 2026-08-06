"""Project-scoped Agent Builder HTTP contract."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.gateway.routers.project_assets import (
    ASSET_ERRORS,
    AssetItemResponse,
    AssetRoute,
    project_asset_context,
    raise_asset_domain,
)
from app.projects.context import ProjectContext
from app.shared_assets.agent_design_service import (
    AgentDesignBlueprint,
    AgentDesignBlueprintTurn,
    AgentDesignClarificationResponse,
    AgentDesignClarificationTurn,
    AgentDesignCommitResult,
    AgentDesignMessageTurn,
    AgentDesignService,
    AgentDesignSessionSummary,
    AgentDesignSessionView,
    CancelAgentDesignSession,
    CommitAgentDesignSession,
    CreateAgentDesignSession,
    SubmitAgentDesignTurn,
)
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetStorageUnavailable
from app.shared_assets.models import AgentModelSettings
from deerflow.persistence.engine import get_session_factory
from deerflow.trace_context import generate_trace_id, get_current_trace_id

router = APIRouter(
    prefix="/api/projects/{project_id}/agent-builder/sessions",
    tags=["project-agent-builder"],
    route_class=AssetRoute,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateAgentDesignSessionRequest(_StrictModel):
    slug: str
    display_name: str
    idempotency_key: str


class AgentDesignMessageTurnRequest(_StrictModel):
    kind: Literal["message"]
    message: str


class AgentDesignClarificationResponseRequest(_StrictModel):
    version: Literal[1]
    kind: Literal["human_input_response"]
    source: str
    request_id: str
    response_kind: Literal["option", "text"]
    option_id: str | None = None
    value: str


class AgentDesignClarificationTurnRequest(_StrictModel):
    kind: Literal["clarification"]
    response: AgentDesignClarificationResponseRequest


class AgentDesignBlueprintRequest(_StrictModel):
    description: str
    model_ref: str
    tool_groups: list[str]
    skill_version_ids: list[uuid.UUID]
    mcp_version_ids: list[uuid.UUID]
    agents_instructions: str
    soul: str
    identity: str
    user_context: str
    model_settings: AgentModelSettings = Field(default_factory=AgentModelSettings)


class AgentDesignBlueprintTurnRequest(_StrictModel):
    kind: Literal["blueprint_update"]
    blueprint: AgentDesignBlueprintRequest


class AgentDesignTurnRequest(_StrictModel):
    input: Annotated[
        AgentDesignMessageTurnRequest | AgentDesignClarificationTurnRequest | AgentDesignBlueprintTurnRequest,
        Field(discriminator="kind"),
    ]
    expected_revision: int = Field(ge=1)
    idempotency_key: str


class AgentDesignCommitRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    expected_blueprint_checksum: str
    idempotency_key: str


class AgentDesignCancelRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str


class AgentDesignProgressItemResponse(_StrictModel):
    id: str
    label: str
    status: Literal["pending", "running", "completed", "failed"]


class AgentDesignMessageResponse(_StrictModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class AgentDesignBlueprintResponse(_StrictModel):
    description: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skill_version_ids: tuple[uuid.UUID, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]
    agents_instructions: str
    soul: str
    identity: str
    user_context: str
    model_settings: AgentModelSettings


class AgentDesignClarificationOptionResponse(_StrictModel):
    id: str
    label: str
    value: str


class AgentDesignClarificationRequestResponse(_StrictModel):
    version: Literal[1]
    kind: Literal["human_input_request"]
    source: str
    request_id: str
    clarification_type: str | None = None
    title: str | None = None
    question: str
    context: str | None = None
    input_mode: Literal["free_text", "single_choice", "choice_with_other"]
    options: tuple[AgentDesignClarificationOptionResponse, ...] | None = None


class AgentDesignSessionItemResponse(_StrictModel):
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
        "proposal_ready",
        "committing",
        "completed",
        "failed",
        "cancelled",
    ]
    revision: int = Field(ge=1)
    blueprint: AgentDesignBlueprintResponse | None
    blueprint_checksum: str | None
    messages: tuple[AgentDesignMessageResponse, ...]
    active_clarification: AgentDesignClarificationRequestResponse | None
    progress: tuple[AgentDesignProgressItemResponse, ...]
    error_code: str | None
    error_message: str | None
    created_agent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AgentDesignSessionSummaryResponse(_StrictModel):
    id: uuid.UUID
    slug: str
    display_name: str
    status: Literal[
        "interviewing",
        "generating",
        "awaiting_clarification",
        "proposal_ready",
        "committing",
        "completed",
        "failed",
        "cancelled",
    ]
    updated_at: datetime


class AgentDesignSessionResponse(_StrictModel):
    data: AgentDesignSessionItemResponse
    request_id: str


class AgentDesignSessionListResponse(_StrictModel):
    data: list[AgentDesignSessionSummaryResponse]
    request_id: str


class AgentDesignCommitDataResponse(_StrictModel):
    session: AgentDesignSessionItemResponse
    agent: AssetItemResponse


class AgentDesignCommitResponse(_StrictModel):
    data: AgentDesignCommitDataResponse
    request_id: str


def _request_id() -> str:
    return get_current_trace_id() or generate_trace_id()


def get_agent_design_service(request: Request) -> AgentDesignService:
    """Resolve the app-owned persistence, audit, and generation dependencies."""

    existing = getattr(request.app.state, "agent_design_service", None)
    if isinstance(existing, AgentDesignService):
        return existing
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        raise_asset_domain(AssetStorageUnavailable(_request_id()))
    governance_sink = getattr(request.app.state, "shared_asset_audit_sink", None)
    if governance_sink is None:
        raise_asset_domain(AssetStorageUnavailable(_request_id()))
    generator = getattr(request.app.state, "agent_design_generation_service", None)
    service = AgentDesignService(
        session_factory,
        generator=generator,
        agent_service=AgentService(
            session_factory,
            governance_sink=governance_sink,
        ),
    )
    request.app.state.agent_design_service = service
    return service


def _blueprint(value: AgentDesignBlueprintRequest) -> AgentDesignBlueprint:
    return AgentDesignBlueprint(
        description=value.description,
        model_ref=value.model_ref,
        tool_groups=tuple(value.tool_groups),
        skill_version_ids=tuple(value.skill_version_ids),
        mcp_version_ids=tuple(value.mcp_version_ids),
        agents_instructions=value.agents_instructions,
        soul=value.soul,
        identity=value.identity,
        user_context=value.user_context,
        model_settings=value.model_settings,
    )


def _turn(body: AgentDesignTurnRequest):
    turn = body.input
    if isinstance(turn, AgentDesignMessageTurnRequest):
        value = AgentDesignMessageTurn(
            kind="message",
            message=turn.message,
        )
    elif isinstance(turn, AgentDesignClarificationTurnRequest):
        response = turn.response
        value = AgentDesignClarificationTurn(
            kind="clarification",
            response=AgentDesignClarificationResponse(
                version=1,
                kind="human_input_response",
                source=response.source,
                request_id=response.request_id,
                response_kind=response.response_kind,
                option_id=response.option_id,
                value=response.value,
            ),
        )
    else:
        value = AgentDesignBlueprintTurn(
            kind="blueprint_update",
            blueprint=_blueprint(turn.blueprint),
        )
    return SubmitAgentDesignTurn(
        input=value,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
    )


def _session_item(view: AgentDesignSessionView) -> AgentDesignSessionItemResponse:
    return AgentDesignSessionItemResponse.model_validate(
        view,
        from_attributes=True,
    )


def _summary_item(
    view: AgentDesignSessionSummary,
) -> AgentDesignSessionSummaryResponse:
    return AgentDesignSessionSummaryResponse.model_validate(
        view,
        from_attributes=True,
    )


def _session_response(
    view: AgentDesignSessionView,
    context: ProjectContext,
) -> AgentDesignSessionResponse:
    return AgentDesignSessionResponse(
        data=_session_item(view),
        request_id=context.request_id,
    )


def _commit_response(
    result: AgentDesignCommitResult,
    context: ProjectContext,
) -> AgentDesignCommitResponse:
    return AgentDesignCommitResponse(
        data=AgentDesignCommitDataResponse(
            session=_session_item(result.session),
            agent=AssetItemResponse.model_validate(
                result.agent,
                from_attributes=True,
            ),
        ),
        request_id=context.request_id,
    )


@router.post(
    "",
    response_model=AgentDesignSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_design_session(
    body: CreateAgentDesignSessionRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
) -> AgentDesignSessionResponse:
    try:
        view = await service.create(
            context,
            CreateAgentDesignSession(
                slug=body.slug,
                display_name=body.display_name,
                idempotency_key=body.idempotency_key,
            ),
        )
        return _session_response(view, context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get("", response_model=AgentDesignSessionListResponse)
async def list_agent_design_sessions(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
) -> AgentDesignSessionListResponse:
    try:
        items = await service.list_incomplete(context)
        return AgentDesignSessionListResponse(
            data=[_summary_item(item) for item in items],
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get(
    "/{session_id}",
    response_model=AgentDesignSessionResponse,
)
async def get_agent_design_session(
    session_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
) -> AgentDesignSessionResponse:
    try:
        return _session_response(
            await service.get(context, session_id),
            context,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/turns",
    response_model=AgentDesignSessionResponse,
)
async def submit_agent_design_turn(
    session_id: uuid.UUID,
    body: AgentDesignTurnRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
) -> AgentDesignSessionResponse:
    try:
        return _session_response(
            await service.submit_turn(context, session_id, _turn(body)),
            context,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/commit",
    response_model=AgentDesignCommitResponse,
)
async def commit_agent_design_session(
    session_id: uuid.UUID,
    body: AgentDesignCommitRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
) -> AgentDesignCommitResponse:
    try:
        result = await service.commit(
            context,
            session_id,
            CommitAgentDesignSession(
                expected_revision=body.expected_revision,
                expected_blueprint_checksum=body.expected_blueprint_checksum,
                idempotency_key=body.idempotency_key,
            ),
        )
        return _commit_response(result, context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/cancel",
    response_model=AgentDesignSessionResponse,
)
async def cancel_agent_design_session(
    session_id: uuid.UUID,
    body: AgentDesignCancelRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
) -> AgentDesignSessionResponse:
    try:
        return _session_response(
            await service.cancel(
                context,
                session_id,
                CancelAgentDesignSession(
                    expected_revision=body.expected_revision,
                    idempotency_key=body.idempotency_key,
                ),
            ),
            context,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


__all__ = [
    "AgentDesignCancelRequest",
    "AgentDesignCommitRequest",
    "AgentDesignCommitResponse",
    "AgentDesignSessionListResponse",
    "AgentDesignSessionResponse",
    "AgentDesignTurnRequest",
    "CreateAgentDesignSessionRequest",
    "get_agent_design_service",
    "router",
]
