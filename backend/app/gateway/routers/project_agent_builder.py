"""Project-scoped Agent Builder HTTP contract."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.gateway.deps import get_config
from app.gateway.routers.project_assets import (
    ASSET_ERRORS,
    AgentAssetItemResponse,
    AssetRoute,
    SkillAssetRefRequest,
    project_asset_context,
    raise_asset_domain,
)
from app.projects.context import ProjectContext
from app.shared_assets.agent_catalog import (
    AgentCatalogValidator,
    StaticToolGroupCatalog,
)
from app.shared_assets.agent_design_activity import AgentDesignActivity
from app.shared_assets.agent_design_service import (
    AGENT_DESIGN_SLUG_MAX_LENGTH,
    AGENT_DESIGN_SLUG_MIN_LENGTH,
    AGENT_DESIGN_SLUG_PATTERN,
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
    SetAgentDesignGenerationPreference,
    SubmitAgentDesignTurn,
)
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetStorageUnavailable, AssetValidationFailed
from app.shared_assets.models import AgentModelSettings, SkillAssetRef
from deerflow.config.app_config import AppConfig
from deerflow.persistence.engine import get_session_factory
from deerflow.trace_context import generate_trace_id, get_current_trace_id

router = APIRouter(
    prefix="/api/projects/{project_id}/agent-builder/sessions",
    tags=["project-agent-builder"],
    route_class=AssetRoute,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


AgentDesignSlug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=AGENT_DESIGN_SLUG_MIN_LENGTH,
        max_length=AGENT_DESIGN_SLUG_MAX_LENGTH,
        pattern=rf"^{AGENT_DESIGN_SLUG_PATTERN}$",
    ),
]


class CreateAgentDesignSessionRequest(_StrictModel):
    slug: AgentDesignSlug
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
    model_ref: str = Field(
        min_length=7,
        max_length=36,
        pattern=(
            r"^(?:default|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})$"
        ),
    )
    tool_groups: list[str]
    skill_refs: list[SkillAssetRefRequest]
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
    generation_model_ref: str | None = Field(
        default=None,
        min_length=7,
        max_length=36,
        pattern=(
            r"^(?:default|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})$"
        ),
    )
    generation_mode: Literal["flash", "thinking", "pro", "ultra"] | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


class AgentDesignGenerationPreferenceRequest(_StrictModel):
    generation_model_ref: str = Field(
        min_length=7,
        max_length=36,
        pattern=(
            r"^(?:default|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})$"
        ),
    )
    generation_mode: Literal["flash", "thinking", "pro", "ultra"]
    thinking_enabled: bool
    reasoning_effort: Literal["none", "low", "medium", "high"] | None


class AgentDesignCommitRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    expected_blueprint_checksum: str
    idempotency_key: str
    slug: AgentDesignSlug | None = None


class AgentDesignCancelRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str


class AgentDesignProgressItemResponse(_StrictModel):
    id: str
    label: str
    status: Literal["pending", "running", "completed", "failed"]


class AgentDesignActivityPayloadResponse(_StrictModel):
    text: str | None = None
    status: Literal["completed", "failed", "stopped", "cancelled"] | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None


class AgentDesignActivityResponse(_StrictModel):
    seq: str
    operation_id: uuid.UUID
    kind: Literal[
        "turn_accepted",
        "attempt_started",
        "reasoning",
        "candidate_generated",
        "validation_started",
        "validation_passed",
        "validation_failed",
        "repair_started",
        "turn_terminal",
        "commit_accepted",
        "commit_validation_started",
        "commit_validation_passed",
        "commit_persistence_started",
        "commit_persistence_completed",
        "commit_terminal",
    ]
    attempt: Literal[1, 2] | None
    payload: AgentDesignActivityPayloadResponse
    created_at: datetime


class AgentDesignActivityListResponse(_StrictModel):
    data: tuple[AgentDesignActivityResponse, ...]
    request_id: str


class AgentDesignMessageResponse(_StrictModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class AgentDesignMessageV3Response(AgentDesignMessageResponse):
    operation_id: uuid.UUID | None


class AgentDesignConflictResponse(_StrictModel):
    code: str
    fields: tuple[
        Literal[
            "agents_instructions",
            "soul",
            "identity",
            "user_context",
        ],
        ...,
    ]
    message: str
    severity: Literal["warning", "error"]


class AgentDesignBlueprintResponse(_StrictModel):
    description: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skill_refs: tuple[SkillAssetRefRequest, ...]
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


class AgentDesignSessionItemV1Response(_StrictModel):
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
    active_clarifications: tuple[AgentDesignClarificationRequestResponse, ...]
    progress: tuple[AgentDesignProgressItemResponse, ...]
    error_code: str | None
    error_message: str | None
    created_agent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AgentDesignSessionItemV2Response(AgentDesignSessionItemV1Response):
    assumptions: tuple[str, ...]
    conflicts: tuple[AgentDesignConflictResponse, ...]


class AgentDesignGenerationPreferenceResponse(_StrictModel):
    model_ref: str
    mode: Literal["flash", "thinking", "pro", "ultra"]


class AgentDesignSessionItemV3Response(AgentDesignSessionItemV2Response):
    generation_preference: AgentDesignGenerationPreferenceResponse | None
    messages: tuple[AgentDesignMessageV3Response, ...]


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
    revision: int = Field(ge=1)
    updated_at: datetime


class AgentDesignSessionV1Response(_StrictModel):
    data: AgentDesignSessionItemV1Response
    request_id: str


class AgentDesignSessionV2Response(_StrictModel):
    data: AgentDesignSessionItemV2Response
    request_id: str


class AgentDesignSessionV3Response(_StrictModel):
    data: AgentDesignSessionItemV3Response
    request_id: str


class AgentDesignSessionListV1Response(_StrictModel):
    data: list[AgentDesignSessionSummaryResponse]
    request_id: str


class AgentDesignSessionListV2Response(_StrictModel):
    data: list[AgentDesignSessionSummaryResponse]
    next_cursor: str | None
    request_id: str


class AgentDesignCommitDataV1Response(_StrictModel):
    session: AgentDesignSessionItemV1Response
    agent: AgentAssetItemResponse


class AgentDesignCommitDataV2Response(_StrictModel):
    session: AgentDesignSessionItemV2Response
    agent: AgentAssetItemResponse


class AgentDesignCommitDataV3Response(_StrictModel):
    session: AgentDesignSessionItemV3Response
    agent: AgentAssetItemResponse


class AgentDesignCommitV1Response(_StrictModel):
    data: AgentDesignCommitDataV1Response
    request_id: str


class AgentDesignCommitV2Response(_StrictModel):
    data: AgentDesignCommitDataV2Response
    request_id: str


class AgentDesignCommitV3Response(_StrictModel):
    data: AgentDesignCommitDataV3Response
    request_id: str


AgentDesignSessionItemResponse = AgentDesignSessionItemV3Response
AgentDesignSessionResponse = AgentDesignSessionV3Response
AgentDesignSessionListResponse = AgentDesignSessionListV2Response
AgentDesignCommitResponse = AgentDesignCommitV3Response
AgentDesignSessionNegotiatedResponse = AgentDesignSessionV1Response | AgentDesignSessionV2Response | AgentDesignSessionV3Response
AgentDesignSessionListNegotiatedResponse = AgentDesignSessionListV1Response | AgentDesignSessionListV2Response
AgentDesignCommitNegotiatedResponse = AgentDesignCommitV1Response | AgentDesignCommitV2Response | AgentDesignCommitV3Response
AgentBuilderContractVersion = Annotated[Literal["1", "2", "3"], Query()]


def _request_id() -> str:
    return get_current_trace_id() or generate_trace_id()


_ACTIVITY_CURSOR = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")


def _activity_cursor(value: str | None, request_id: str) -> int:
    if value in {None, ""}:
        return 0
    if not isinstance(value, str) or _ACTIVITY_CURSOR.fullmatch(value) is None:
        raise_asset_domain(AssetValidationFailed(request_id))
    parsed = int(value)
    if parsed > 9_223_372_036_854_775_807:
        raise_asset_domain(AssetValidationFailed(request_id))
    return parsed


def _activity_response(
    activity: AgentDesignActivity,
) -> AgentDesignActivityResponse:
    return AgentDesignActivityResponse(
        seq=str(activity.seq),
        operation_id=activity.operation_id,
        kind=activity.kind.value,
        attempt=activity.attempt,
        payload=AgentDesignActivityPayloadResponse.model_validate(
            activity.payload,
        ),
        created_at=activity.created_at,
    )


def _all_internal_tool_groups(config: AppConfig) -> tuple[str, ...]:
    """Return every configured internal tool group plus subagent delegation."""

    return tuple(
        dict.fromkeys(
            (
                *(group.name for group in config.tool_groups),
                *(tool.group for tool in config.tools),
                "task",
            )
        )
    )


def _agent_catalog_validator(config: AppConfig) -> AgentCatalogValidator:
    return AgentCatalogValidator(
        StaticToolGroupCatalog(_all_internal_tool_groups(config)),
    )


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
    config = get_config()
    service = AgentDesignService(
        session_factory,
        generator=generator,
        agent_service=AgentService(
            session_factory,
            governance_sink=governance_sink,
            catalog_validator=_agent_catalog_validator(config),
        ),
        default_tool_groups_provider=lambda: _all_internal_tool_groups(get_config()),
        generation_control=getattr(
            request.app.state,
            "agent_design_generation_control",
            None,
        ),
    )
    return service


def _blueprint(value: AgentDesignBlueprintRequest) -> AgentDesignBlueprint:
    return AgentDesignBlueprint(
        description=value.description,
        model_ref=value.model_ref,
        tool_groups=tuple(value.tool_groups),
        skill_refs=tuple(SkillAssetRef(scope=item.scope, asset_id=item.asset_id) for item in value.skill_refs),
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
        generation_model_ref=body.generation_model_ref,
        generation_mode=body.generation_mode,
        thinking_enabled=body.thinking_enabled,
        reasoning_effort=body.reasoning_effort,
    )


def _generation_preference(
    body: AgentDesignGenerationPreferenceRequest,
) -> SetAgentDesignGenerationPreference:
    return SetAgentDesignGenerationPreference(
        generation_model_ref=body.generation_model_ref,
        generation_mode=body.generation_mode,
        thinking_enabled=body.thinking_enabled,
        reasoning_effort=body.reasoning_effort,
    )


def _session_item(view: AgentDesignSessionView) -> AgentDesignSessionItemResponse:
    return AgentDesignSessionItemResponse.model_validate(
        view,
        from_attributes=True,
    )


def _session_item_v2(
    view: AgentDesignSessionView,
) -> AgentDesignSessionItemV2Response:
    return AgentDesignSessionItemV2Response.model_validate(
        view,
        from_attributes=True,
    )


def _session_item_v1(
    view: AgentDesignSessionView,
) -> AgentDesignSessionItemV1Response:
    return AgentDesignSessionItemV1Response.model_validate(
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
    *,
    contract_version: Literal["1", "2", "3"],
) -> AgentDesignSessionNegotiatedResponse:
    if contract_version == "1":
        return AgentDesignSessionV1Response(
            data=_session_item_v1(view),
            request_id=context.request_id,
        )
    if contract_version == "2":
        return AgentDesignSessionV2Response(
            data=_session_item_v2(view),
            request_id=context.request_id,
        )
    if contract_version == "3":
        return AgentDesignSessionV3Response(
            data=_session_item(view),
            request_id=context.request_id,
        )
    raise ValueError("unsupported Agent Builder contract version")


def _commit_response(
    result: AgentDesignCommitResult,
    context: ProjectContext,
    *,
    contract_version: Literal["1", "2", "3"],
) -> AgentDesignCommitNegotiatedResponse:
    agent = AgentAssetItemResponse.model_validate(
        result.agent,
        from_attributes=True,
    )
    if contract_version == "1":
        return AgentDesignCommitV1Response(
            data=AgentDesignCommitDataV1Response(
                session=_session_item_v1(result.session),
                agent=agent,
            ),
            request_id=context.request_id,
        )
    if contract_version == "2":
        return AgentDesignCommitV2Response(
            data=AgentDesignCommitDataV2Response(
                session=_session_item_v2(result.session),
                agent=agent,
            ),
            request_id=context.request_id,
        )
    if contract_version == "3":
        return AgentDesignCommitV3Response(
            data=AgentDesignCommitDataV3Response(
                session=_session_item(result.session),
                agent=agent,
            ),
            request_id=context.request_id,
        )
    raise ValueError("unsupported Agent Builder contract version")


@router.post(
    "",
    response_model=AgentDesignSessionNegotiatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_design_session(
    body: CreateAgentDesignSessionRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignSessionNegotiatedResponse:
    try:
        view = await service.create(
            context,
            CreateAgentDesignSession(
                slug=body.slug,
                display_name=body.display_name,
                idempotency_key=body.idempotency_key,
            ),
        )
        return _session_response(
            view,
            context,
            contract_version=contract_version,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get("", response_model=AgentDesignSessionListNegotiatedResponse)
async def list_agent_design_sessions(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignSessionListNegotiatedResponse:
    try:
        page = await service.list_incomplete(
            context,
            limit=limit,
            cursor=cursor,
        )
        data = [_summary_item(item) for item in page.items]
        if contract_version == "1":
            return AgentDesignSessionListV1Response(
                data=data,
                request_id=context.request_id,
            )
        if contract_version in {"2", "3"}:
            return AgentDesignSessionListV2Response(
                data=data,
                next_cursor=page.next_cursor,
                request_id=context.request_id,
            )
        raise ValueError("unsupported Agent Builder contract version")
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get(
    "/by-agent/{agent_id}",
    response_model=AgentDesignSessionNegotiatedResponse,
)
async def get_agent_design_session_by_agent(
    agent_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignSessionNegotiatedResponse:
    try:
        return _session_response(
            await service.get_by_created_agent(context, agent_id),
            context,
            contract_version=contract_version,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get(
    "/{session_id}",
    response_model=AgentDesignSessionNegotiatedResponse,
)
async def get_agent_design_session(
    session_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignSessionNegotiatedResponse:
    try:
        return _session_response(
            await service.get(context, session_id),
            context,
            contract_version=contract_version,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/turns",
    response_model=AgentDesignSessionNegotiatedResponse,
)
async def submit_agent_design_turn(
    session_id: uuid.UUID,
    body: AgentDesignTurnRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignSessionNegotiatedResponse:
    try:
        return _session_response(
            await service.submit_turn(context, session_id, _turn(body)),
            context,
            contract_version=contract_version,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.put(
    "/{session_id}/generation-preference",
    response_model=AgentDesignSessionNegotiatedResponse,
)
async def set_agent_design_generation_preference(
    session_id: uuid.UUID,
    body: AgentDesignGenerationPreferenceRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignSessionNegotiatedResponse:
    try:
        return _session_response(
            await service.set_generation_preference(
                context,
                session_id,
                _generation_preference(body),
            ),
            context,
            contract_version=contract_version,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get(
    "/{session_id}/activities",
    response_model=AgentDesignActivityListResponse,
)
async def list_agent_design_activities(
    session_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    after_seq: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2_000)] = 500,
) -> AgentDesignActivityListResponse:
    try:
        activities = await service.list_activities(
            context,
            session_id,
            after_seq=_activity_cursor(after_seq, context.request_id),
            limit=limit,
        )
        return AgentDesignActivityListResponse(
            data=tuple(_activity_response(activity) for activity in activities),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get("/{session_id}/activities/stream")
async def stream_agent_design_activities(
    session_id: uuid.UUID,
    request: Request,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    after_seq: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    cursor = _activity_cursor(
        request.headers.get("Last-Event-ID") or after_seq,
        context.request_id,
    )
    try:
        await service.list_activities(
            context,
            session_id,
            after_seq=cursor,
            limit=1,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)

    async def events():
        nonlocal cursor
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                return
            try:
                activities = await service.list_activities(
                    context,
                    session_id,
                    after_seq=cursor,
                    limit=500,
                )
            except ASSET_ERRORS:
                return
            if activities:
                idle_ticks = 0
                for activity in activities:
                    response = _activity_response(activity)
                    cursor = activity.seq
                    payload = json.dumps(
                        response.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {response.seq}\nevent: activity\ndata: {payload}\n\n"
                continue
            idle_ticks += 1
            if idle_ticks >= 60:
                idle_ticks = 0
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{session_id}/turns/stop",
    response_model=AgentDesignSessionNegotiatedResponse,
)
async def stop_agent_design_turn(
    session_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignSessionNegotiatedResponse:
    try:
        return _session_response(
            await service.stop_turn(context, session_id),
            context,
            contract_version=contract_version,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/commit",
    response_model=AgentDesignCommitNegotiatedResponse,
)
async def commit_agent_design_session(
    session_id: uuid.UUID,
    body: AgentDesignCommitRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignCommitNegotiatedResponse:
    try:
        result = await service.commit(
            context,
            session_id,
            CommitAgentDesignSession(
                expected_revision=body.expected_revision,
                expected_blueprint_checksum=body.expected_blueprint_checksum,
                idempotency_key=body.idempotency_key,
                slug=body.slug,
            ),
        )
        return _commit_response(
            result,
            context,
            contract_version=contract_version,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/cancel",
    response_model=AgentDesignSessionNegotiatedResponse,
)
async def cancel_agent_design_session(
    session_id: uuid.UUID,
    body: AgentDesignCancelRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentDesignService, Depends(get_agent_design_service)],
    contract_version: AgentBuilderContractVersion = "1",
) -> AgentDesignSessionNegotiatedResponse:
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
            contract_version=contract_version,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


__all__ = [
    "AgentDesignCancelRequest",
    "AgentDesignCommitRequest",
    "AgentDesignActivityListResponse",
    "AgentDesignActivityResponse",
    "AgentDesignCommitResponse",
    "AgentDesignCommitV1Response",
    "AgentDesignCommitV2Response",
    "AgentDesignCommitV3Response",
    "AgentDesignSessionListResponse",
    "AgentDesignSessionListV1Response",
    "AgentDesignSessionListV2Response",
    "AgentDesignSessionResponse",
    "AgentDesignSessionV1Response",
    "AgentDesignSessionV2Response",
    "AgentDesignSessionV3Response",
    "AgentDesignTurnRequest",
    "CreateAgentDesignSessionRequest",
    "get_agent_design_service",
    "router",
]
