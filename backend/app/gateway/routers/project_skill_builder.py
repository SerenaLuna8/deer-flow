"""Project-scoped conversational Skill Builder HTTP contract."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.gateway.deps import get_system_model_catalog
from app.gateway.routers.project_assets import (
    ASSET_ERRORS,
    AssetItemResponse,
    AssetRoute,
    SkillVersionItemResponse,
    project_asset_context,
    raise_asset_domain,
)
from app.private_work.skill_builder_run_admission import (
    SkillBuilderRunAdmissionService,
)
from app.projects.context import ProjectContext
from app.shared_assets.errors import AssetStorageUnavailable
from app.shared_assets.skill_builder_admission_contract import (
    SkillBuilderRunAdmission,
)
from app.shared_assets.skill_design_generation import (
    MAX_SKILL_DESIGN_ATTACHMENTS,
)
from app.shared_assets.skill_design_service import (
    CancelSkillDesignSession,
    CommitSkillDesignSession,
    CreateSkillDesignRevisionSession,
    CreateSkillDesignSession,
    SkillDesignClarificationResponse,
    SkillDesignClarificationTurn,
    SkillDesignCommitResult,
    SkillDesignDraftUpdateTurn,
    SkillDesignMessageTurn,
    SkillDesignService,
    SkillDesignSessionSummary,
    SkillDesignSessionView,
    SkillDesignTurnAttachment,
    SubmitSkillDesignTurn,
    ValidateSkillDesignSession,
)
from app.shared_assets.skill_service import SkillFileChange, SkillService
from app.system_settings import (
    PublicSystemModelView,
    SystemModelCatalogService,
)
from app.system_settings.errors import SystemModelStorageUnavailable
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
    """Single strict model with kind cross-validation.

    A discriminated union would 422 existing clients that omit the tag, so
    ``kind`` defaults to ``create`` and field pairing is enforced after
    parsing. Revise mode rejects slug/display_name: the server copies both
    from the target asset so they cannot drift.
    """

    kind: Literal["create", "revise"] = "create"
    slug: str | None = None
    display_name: str | None = None
    skill_id: uuid.UUID | None = None
    idempotency_key: str

    @model_validator(mode="after")
    def validate_kind_fields(self) -> CreateSkillDesignSessionRequest:
        if self.kind == "create":
            if self.slug is None or self.display_name is None or self.skill_id is not None:
                raise ValueError("create requires slug and display_name")
        elif self.slug is not None or self.display_name is not None or self.skill_id is None:
            raise ValueError("revise requires only skill_id")
        return self


SkillDesignReasoningEffort = Literal["none", "low", "medium", "high"]


class SkillDesignAttachmentRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=120)
    content: str


class SkillDesignMessageTurnRequest(_StrictModel):
    kind: Literal["message"]
    message: str
    model_name: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )
    reasoning_effort: SkillDesignReasoningEffort | None = None
    attachments: list[SkillDesignAttachmentRequest] = Field(
        default_factory=list,
        max_length=MAX_SKILL_DESIGN_ATTACHMENTS,
    )


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
    model_name: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )
    reasoning_effort: SkillDesignReasoningEffort | None = None


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
    acknowledge_base_stale: bool = False


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


class SkillDesignBaseFileResponse(_StrictModel):
    path: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str


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


class SkillDesignActiveRunResponse(_StrictModel):
    runId: uuid.UUID = Field(validation_alias="run_id")
    status: Literal["pending", "running"]
    streamUrl: str = ""


class SkillDesignSkillDependencyResponse(_StrictModel):
    kind: Literal["skill"]
    reference: str
    scope: Literal["project", "system"]
    skill_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int = Field(ge=1)
    slug: str
    display_name: str
    payload_checksum: str
    authoring_only: Literal[True]
    runtime_authorized: Literal[False]


class SkillDesignMcpToolDependencyResponse(_StrictModel):
    kind: Literal["mcp_tool"]
    reference: str
    scope: Literal["project", "system"]
    mcp_server_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int = Field(ge=1)
    server_slug: str
    server_name: str
    tool_name: str
    payload_checksum: str
    inventory_status: Literal["ready", "degraded"]
    inventory_error_code: (
        Literal[
            "mcp_discovery_unavailable",
            "mcp_catalog_invalid",
        ]
        | None
    )
    last_success_at: datetime
    authoring_only: Literal[True]
    runtime_authorized: Literal[False]


class SkillDesignDependencySnapshotResponse(_StrictModel):
    version: Literal[1]
    draft_checksum: str
    requirements: tuple[
        Annotated[
            SkillDesignSkillDependencyResponse | SkillDesignMcpToolDependencyResponse,
            Field(discriminator="kind"),
        ],
        ...,
    ]


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
    created_skill_version_id: uuid.UUID | None
    authoring_dependencies: SkillDesignDependencySnapshotResponse | None
    session_kind: Literal["create", "revise"]
    target_skill_id: uuid.UUID | None
    base_version_id: uuid.UUID | None
    base_version_number: int | None = Field(default=None, ge=1)
    base_payload_checksum: str | None
    target_skill_deleted: bool
    base_files: tuple[SkillDesignBaseFileResponse, ...]
    activeRun: SkillDesignActiveRunResponse | None = Field(
        default=None,
        validation_alias="active_run",
    )
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
    revision: int = Field(ge=1)
    updated_at: datetime
    session_kind: Literal["create", "revise"]


class SkillDesignSessionResponse(_StrictModel):
    data: SkillDesignSessionItemResponse
    request_id: str


class SkillDesignRunAdmissionResponse(_StrictModel):
    runId: uuid.UUID
    status: Literal["pending", "running"]
    streamUrl: str


class SkillDesignSessionListResponse(_StrictModel):
    data: list[SkillDesignSessionSummaryResponse]
    request_id: str


class SkillDesignCommitDataResponse(_StrictModel):
    session: SkillDesignSessionItemResponse
    skill: AssetItemResponse
    version: SkillVersionItemResponse | None = None


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
    skill_service = SkillService(
        session_factory,
        governance_sink=governance_sink,
        quota=getattr(request.app.state, "project_quota_enforcer", None),
    )
    model_catalog = getattr(request.app.state, "system_model_catalog", None)
    runtime_policy = getattr(
        request.app.state,
        "system_runtime_policy_service",
        None,
    )
    if model_catalog is None or runtime_policy is None:
        raise_asset_domain(AssetStorageUnavailable(_request_id()))
    run_admission = SkillBuilderRunAdmissionService(
        session_factory,
        model_catalog=model_catalog,
        runtime_policy=runtime_policy,
        endpoint_policy=getattr(
            request.app.state,
            "mcp_endpoint_policy",
            None,
        ),
        quota=getattr(
            request.app.state,
            "project_quota_enforcer",
            None,
        ),
        audit=getattr(
            request.app.state,
            "operational_audit_sink",
            None,
        ),
    )
    service = SkillDesignService(
        session_factory,
        skill_service=skill_service,
        run_admission=run_admission,
        quota=getattr(
            request.app.state,
            "project_quota_enforcer",
            None,
        ),
        audit=getattr(
            request.app.state,
            "operational_audit_sink",
            None,
        ),
    )
    request.app.state.skill_design_service = service
    return service


def _turn(body: SkillDesignTurnRequest) -> SubmitSkillDesignTurn:
    turn = body.input
    if isinstance(turn, SkillDesignMessageTurnRequest):
        value = SkillDesignMessageTurn(
            kind="message",
            message=turn.message,
            model_name=turn.model_name,
            reasoning_effort=turn.reasoning_effort,
            attachments=tuple(
                SkillDesignTurnAttachment(
                    name=attachment.name,
                    content=attachment.content,
                )
                for attachment in turn.attachments
            ),
        )
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
            model_name=turn.model_name,
            reasoning_effort=turn.reasoning_effort,
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
    item = SkillDesignSessionItemResponse.model_validate(
        replace(view, active_run=None),
        from_attributes=True,
    )
    if view.active_run is None:
        return item
    return item.model_copy(
        update={
            "activeRun": SkillDesignActiveRunResponse(
                run_id=uuid.UUID(view.active_run.run_id),
                status=view.active_run.status,
                streamUrl=(f"/api/projects/{view.project_id}/private-work/threads/{view.thread_id}/runs/{view.active_run.run_id}/stream"),
            )
        }
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
            version=(
                SkillVersionItemResponse.model_validate(
                    result.version,
                    from_attributes=True,
                )
                if result.version is not None
                else None
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
        if body.kind == "revise":
            view = await service.create_revision(
                context,
                CreateSkillDesignRevisionSession(
                    skill_id=body.skill_id,
                    idempotency_key=body.idempotency_key,
                ),
            )
        else:
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


def _execution_options_error(
    code: str,
    message: str,
    request_id: str,
) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": code,
            "message": message,
            "request_id": request_id,
        },
    )


def require_admissible_execution_options(
    models: list[PublicSystemModelView],
    *,
    model_name: str | None,
    reasoning_effort: str | None,
    request_id: str,
) -> None:
    """Fail closed when a turn requests a model or effort the catalog denies."""

    wants_thinking = reasoning_effort is not None and reasoning_effort != "none"
    if model_name is None and not wants_thinking:
        return
    selected: PublicSystemModelView | None = None
    if model_name is not None:
        selected = next(
            (model for model in models if model.model_ref == model_name),
            None,
        )
        if selected is None:
            raise _execution_options_error(
                "SKILL_BUILDER_MODEL_UNAVAILABLE",
                "所选模型当前不可用，请重新选择模型。",
                request_id,
            )
    if wants_thinking:
        thinking_model = selected or next(
            (model for model in models if model.is_default),
            models[0] if models else None,
        )
        if thinking_model is None or not thinking_model.supports_thinking:
            raise _execution_options_error(
                "SKILL_BUILDER_EFFORT_UNSUPPORTED",
                "所选模型不支持扩展思考，请调整思考强度。",
                request_id,
            )


@router.post(
    "/{session_id}/turns",
    response_model=SkillDesignSessionResponse | SkillDesignRunAdmissionResponse,
)
async def submit_skill_design_turn(
    session_id: uuid.UUID,
    body: SkillDesignTurnRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
    model_catalog: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
    response: Response,
) -> SkillDesignSessionResponse | SkillDesignRunAdmissionResponse:
    turn = body.input
    if isinstance(
        turn,
        SkillDesignMessageTurnRequest | SkillDesignClarificationTurnRequest,
    ) and (turn.model_name is not None or turn.reasoning_effort is not None):
        try:
            models = await model_catalog.list_available_models()
        except SystemModelStorageUnavailable:
            raise_asset_domain(AssetStorageUnavailable(context.request_id))
        require_admissible_execution_options(
            list(models),
            model_name=turn.model_name,
            reasoning_effort=turn.reasoning_effort,
            request_id=context.request_id,
        )
    try:
        result = await service.submit_turn(
            context,
            session_id,
            _turn(body),
        )
        if isinstance(result, SkillBuilderRunAdmission):
            response.status_code = status.HTTP_202_ACCEPTED
            return SkillDesignRunAdmissionResponse(
                runId=uuid.UUID(result.run_id),
                status=result.status,
                streamUrl=(f"/api/projects/{context.project_id}/private-work/threads/{result.thread_id}/runs/{result.run_id}/stream"),
            )
        return _session_response(result, context)
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
                acknowledge_base_stale=body.acknowledge_base_stale,
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
    "SkillDesignAttachmentRequest",
    "SkillDesignCancelRequest",
    "SkillDesignCommitRequest",
    "SkillDesignCommitResponse",
    "SkillDesignSessionListResponse",
    "SkillDesignSessionResponse",
    "SkillDesignTurnRequest",
    "SkillDesignValidateRequest",
    "get_skill_design_service",
    "require_admissible_execution_options",
    "router",
]
