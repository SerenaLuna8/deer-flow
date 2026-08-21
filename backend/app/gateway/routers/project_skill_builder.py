"""Project-scoped conversational Skill Builder HTTP contract."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

from app.gateway.deps import get_system_model_catalog
from app.gateway.routers.project_assets import (
    ASSET_ERRORS,
    AssetRoute,
    CurrentVersionAssetItemResponse,
    SkillVersionItemResponse,
    project_asset_context,
    raise_asset_domain,
)
from app.private_work.skill_builder_run_admission import (
    SkillBuilderRunAdmissionService,
)
from app.projects.context import ProjectContext
from app.shared_assets.agent_design_profile import agent_design_mode_matches_profile
from app.shared_assets.errors import AssetStorageUnavailable, AssetValidationFailed
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
    SetSkillDesignExecutionPreference,
    SkillDesignActivity,
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


def _parse_json_uuid(value: object) -> object:
    """Parse the JSON UUID representation without relaxing other fields."""

    if isinstance(value, uuid.UUID):
        return value
    if type(value) is str:
        try:
            return uuid.UUID(value)
        except ValueError:
            return value
    return value


_JsonUuid = Annotated[uuid.UUID, BeforeValidator(_parse_json_uuid)]


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
    skill_id: _JsonUuid | None = None
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
    mode: Literal["flash", "thinking", "pro", "ultra"] | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: SkillDesignReasoningEffort | None = None
    attachments: list[SkillDesignAttachmentRequest] = Field(
        default_factory=list,
        max_length=MAX_SKILL_DESIGN_ATTACHMENTS,
    )

    @model_validator(mode="after")
    def validate_profile(self) -> SkillDesignMessageTurnRequest:
        _validate_turn_profile(self)
        return self


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
    mode: Literal["flash", "thinking", "pro", "ultra"] | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: SkillDesignReasoningEffort | None = None

    @model_validator(mode="after")
    def validate_profile(self) -> SkillDesignClarificationTurnRequest:
        _validate_turn_profile(self)
        return self


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


class SkillDesignExecutionPreferenceRequest(_StrictModel):
    model_name: str = Field(
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )
    mode: Literal["flash", "thinking", "pro", "ultra"]
    thinking_enabled: bool
    reasoning_effort: SkillDesignReasoningEffort | None

    @model_validator(mode="after")
    def validate_profile(self) -> SkillDesignExecutionPreferenceRequest:
        if not agent_design_mode_matches_profile(
            self.mode,
            thinking_enabled=self.thinking_enabled,
            reasoning_effort=self.reasoning_effort,
        ):
            raise ValueError("mode and reasoning profile are inconsistent")
        return self


def _validate_turn_profile(
    value: SkillDesignMessageTurnRequest | SkillDesignClarificationTurnRequest,
) -> None:
    if value.mode is None and value.thinking_enabled is None:
        return
    if value.model_name is None or value.mode is None or value.thinking_enabled is None:
        raise ValueError("a turn execution profile must be complete")
    if not agent_design_mode_matches_profile(
        value.mode,
        thinking_enabled=value.thinking_enabled,
        reasoning_effort=value.reasoning_effort,
    ):
        raise ValueError("turn mode and reasoning profile are inconsistent")


class SkillDesignProgressItemResponse(_StrictModel):
    id: str
    label: str
    status: Literal["pending", "running", "completed", "failed"]


class SkillDesignExecutionPreferenceResponse(_StrictModel):
    model_name: str
    mode: Literal["flash", "thinking", "pro", "ultra"]
    thinking_enabled: bool
    reasoning_effort: SkillDesignReasoningEffort | None


class SkillDesignMessageResponse(_StrictModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    operation_id: uuid.UUID | None = None


class _SkillDesignActivityBaseResponse(_StrictModel):
    seq: str
    operation_id: uuid.UUID
    run_id: str | None
    attempt: int | None
    created_at: datetime


class SkillDesignEmptyActivityPayloadResponse(_StrictModel):
    pass


class SkillDesignReasoningActivityPayloadResponse(_StrictModel):
    text: str = Field(min_length=1)


class SkillDesignToolActivityPayloadResponse(_StrictModel):
    tool_call_id: str = Field(min_length=1, max_length=512)
    tool_name: Literal[
        "search_available_skills",
        "read_skill_version",
        "search_available_mcp_tools",
        "inspect_mcp_tool",
        "list_candidate_files",
        "read_candidate_file",
        "upsert_candidate_file",
        "delete_candidate_file",
        "request_skill_clarification",
        "finalize_skill_candidate",
    ]
    result_count: int | None = Field(default=None, ge=0, le=128)
    resource_name: str | None = Field(default=None, min_length=1, max_length=512)
    path: str | None = Field(default=None, min_length=1, max_length=1_024)
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        le=2 * 1024 * 1024,
    )


class SkillDesignTerminalActivityPayloadResponse(_StrictModel):
    status: Literal["completed", "failed", "stopped"]
    code: str | None = Field(default=None, min_length=1, max_length=64)


class SkillDesignValidationStageActivityPayloadResponse(_StrictModel):
    stage: Literal["package_files", "safety_scan"]


class SkillDesignEmptyActivityResponse(_SkillDesignActivityBaseResponse):
    kind: Literal[
        "request_accepted",
        "attempt_started",
        "candidate_generated",
        "validation_passed",
        "validation_failed",
        "repair_started",
        "commit_accepted",
        "commit_validation_started",
        "commit_validation_passed",
        "commit_persistence_started",
        "commit_persistence_completed",
    ]
    payload: SkillDesignEmptyActivityPayloadResponse


class SkillDesignValidationStartedActivityResponse(
    _SkillDesignActivityBaseResponse,
):
    kind: Literal["validation_started"]
    payload: SkillDesignEmptyActivityPayloadResponse | SkillDesignValidationStageActivityPayloadResponse


class SkillDesignReasoningActivityResponse(_SkillDesignActivityBaseResponse):
    kind: Literal["reasoning"]
    payload: SkillDesignReasoningActivityPayloadResponse


class SkillDesignToolActivityResponse(_SkillDesignActivityBaseResponse):
    kind: Literal["tool_started", "tool_completed", "tool_failed"]
    payload: SkillDesignToolActivityPayloadResponse


class SkillDesignTerminalActivityResponse(_SkillDesignActivityBaseResponse):
    kind: Literal["run_terminal", "commit_terminal"]
    payload: SkillDesignTerminalActivityPayloadResponse


SkillDesignActivityResponse = Annotated[
    SkillDesignEmptyActivityResponse | SkillDesignValidationStartedActivityResponse | SkillDesignReasoningActivityResponse | SkillDesignToolActivityResponse | SkillDesignTerminalActivityResponse,
    Field(discriminator="kind"),
]

_SKILL_DESIGN_ACTIVITY_RESPONSE_ADAPTER = TypeAdapter(
    SkillDesignActivityResponse,
)


class SkillDesignActivityListResponse(_StrictModel):
    data: tuple[SkillDesignActivityResponse, ...]
    request_id: str


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
    execution_preference: SkillDesignExecutionPreferenceResponse | None
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
    skill: CurrentVersionAssetItemResponse
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
            mode=turn.mode,
            thinking_enabled=turn.thinking_enabled,
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
            mode=turn.mode,
            thinking_enabled=turn.thinking_enabled,
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


def _execution_preference(
    body: SkillDesignExecutionPreferenceRequest,
) -> SetSkillDesignExecutionPreference:
    return SetSkillDesignExecutionPreference(
        model_name=body.model_name,
        mode=body.mode,
        thinking_enabled=body.thinking_enabled,
        reasoning_effort=body.reasoning_effort,
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
            skill=CurrentVersionAssetItemResponse.model_validate(
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


def _activity_cursor(value: str | None, request_id: str) -> int:
    if value is None:
        return 0
    if not value.isascii() or not value.isdigit():
        raise_asset_domain(AssetValidationFailed(request_id))
    cursor = int(value)
    if cursor < 0 or cursor > 9_223_372_036_854_775_807:
        raise_asset_domain(AssetValidationFailed(request_id))
    return cursor


def _activity_response(
    activity: SkillDesignActivity,
) -> SkillDesignActivityResponse:
    return _SKILL_DESIGN_ACTIVITY_RESPONSE_ADAPTER.validate_python(
        {
            "seq": str(activity.seq),
            "operation_id": activity.operation_id,
            "run_id": activity.run_id,
            "kind": activity.kind.value,
            "attempt": activity.attempt,
            "payload": activity.payload,
            "created_at": activity.created_at,
        }
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
    "/by-version/{version_id}",
    response_model=SkillDesignSessionResponse,
)
async def get_skill_design_session_by_version(
    version_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignSessionResponse:
    try:
        return _session_response(
            await service.get_by_created_version(context, version_id),
            context,
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


@router.get(
    "/{session_id}/activities",
    response_model=SkillDesignActivityListResponse,
)
async def list_skill_design_activities(
    session_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
    after_seq: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2_000)] = 500,
) -> SkillDesignActivityListResponse:
    try:
        activities = await service.list_activities(
            context,
            session_id,
            after_seq=_activity_cursor(after_seq, context.request_id),
            limit=limit,
        )
        return SkillDesignActivityListResponse(
            data=tuple(_activity_response(item) for item in activities),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get("/{session_id}/activities/stream")
async def stream_skill_design_activities(
    session_id: uuid.UUID,
    request: Request,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
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
                    yield (f"id: {response.seq}\nevent: activity\ndata: {payload}\n\n")
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


def require_admissible_execution_preference(
    models: list[PublicSystemModelView],
    body: SkillDesignExecutionPreferenceRequest,
    *,
    request_id: str,
) -> None:
    selected = next(
        (model for model in models if model.model_ref == body.model_name),
        None,
    )
    if selected is None:
        raise _execution_options_error(
            "SKILL_BUILDER_MODEL_UNAVAILABLE",
            "所选模型当前不可用，请重新选择模型。",
            request_id,
        )
    if body.mode != "flash" and not selected.supports_thinking:
        raise _execution_options_error(
            "SKILL_BUILDER_EFFORT_UNSUPPORTED",
            "所选模型不支持扩展思考，请调整思考强度。",
            request_id,
        )
    if body.mode in {"pro", "ultra"} and not selected.supports_reasoning_effort:
        raise _execution_options_error(
            "SKILL_BUILDER_EFFORT_UNSUPPORTED",
            "所选模型不支持该思考强度，请调整思考强度。",
            request_id,
        )
    expected_effort = (
        {
            "flash": "none",
            "thinking": "low",
            "pro": "medium",
            "ultra": "high",
        }[body.mode]
        if selected.supports_reasoning_effort
        else None
    )
    if body.reasoning_effort != expected_effort:
        raise _execution_options_error(
            "SKILL_BUILDER_EFFORT_UNSUPPORTED",
            "所选模型无法使用该思考强度，请刷新模型目录。",
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
    ) and (turn.model_name is not None or turn.mode is not None or turn.thinking_enabled is not None or turn.reasoning_effort is not None):
        try:
            models = await model_catalog.list_available_models()
        except SystemModelStorageUnavailable:
            raise_asset_domain(AssetStorageUnavailable(context.request_id))
        if turn.model_name is not None and turn.mode is not None and turn.thinking_enabled is not None:
            require_admissible_execution_preference(
                list(models),
                SkillDesignExecutionPreferenceRequest(
                    model_name=turn.model_name,
                    mode=turn.mode,
                    thinking_enabled=turn.thinking_enabled,
                    reasoning_effort=turn.reasoning_effort,
                ),
                request_id=context.request_id,
            )
        else:
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


@router.put(
    "/{session_id}/execution-preference",
    response_model=SkillDesignSessionResponse,
)
async def set_skill_design_execution_preference(
    session_id: uuid.UUID,
    body: SkillDesignExecutionPreferenceRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
    model_catalog: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
) -> SkillDesignSessionResponse:
    try:
        models = list(await model_catalog.list_available_models())
    except SystemModelStorageUnavailable:
        raise_asset_domain(AssetStorageUnavailable(context.request_id))
    require_admissible_execution_preference(
        models,
        body,
        request_id=context.request_id,
    )
    try:
        view = await service.set_execution_preference(
            context,
            session_id,
            _execution_preference(body),
        )
        return _session_response(view, context)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.post(
    "/{session_id}/turns/stop",
    response_model=SkillDesignSessionResponse,
)
async def stop_skill_design_turn(
    session_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillDesignService, Depends(get_skill_design_service)],
) -> SkillDesignSessionResponse:
    try:
        return _session_response(
            await service.stop_current_run(context, session_id),
            context,
        )
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
