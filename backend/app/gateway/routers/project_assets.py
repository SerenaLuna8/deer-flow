from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import get_current_user_from_request, project_session
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectForbidden, ProjectNotFound
from app.shared_assets import (
    AgentPayload,
    AgentService,
    AssetConflict,
    AssetForbidden,
    AssetKind,
    AssetNotFound,
    AssetScope,
    AssetSelection,
    AssetStorageUnavailable,
    AssetValidationFailed,
    BindingService,
    CreateAgent,
    CreateCredential,
    CreateMcpServer,
    CreateSkill,
    CredentialService,
    McpCredentialSlot,
    McpDefinition,
    McpService,
    SharedAssetError,
    SkillArchiveFile,
    SkillService,
)
from deerflow.persistence.engine import get_session_factory
from deerflow.trace_context import generate_trace_id, get_current_trace_id


class AssetRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                raise_asset_domain(AssetValidationFailed(request_id), request_id)

        return handler


project_router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["project-assets"],
    route_class=AssetRoute,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetItemResponse(_StrictModel):
    id: uuid.UUID
    scope: AssetScope
    project_id: uuid.UUID | None
    slug: str
    display_name: str
    status: str
    current_published_version_id: uuid.UUID | None
    version: int
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime


class CredentialItemResponse(_StrictModel):
    id: uuid.UUID
    scope: AssetScope
    project_id: uuid.UUID | None
    name: str
    display_name: str
    credential_type: str
    status: str
    current_version_id: uuid.UUID | None
    version: int
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime


class ScopedAssetListResponse(_StrictModel):
    system_items: list[AssetItemResponse]
    project_items: list[AssetItemResponse]
    request_id: str


class ScopedCredentialListResponse(_StrictModel):
    system_items: list[CredentialItemResponse]
    project_items: list[CredentialItemResponse]
    request_id: str


class AssetMutationResponse(_StrictModel):
    item: AssetItemResponse
    request_id: str


class CredentialMutationResponse(_StrictModel):
    item: CredentialItemResponse
    request_id: str


class CreateAssetRequest(_StrictModel):
    slug: str
    display_name: str


class ExpectedAssetVersionRequest(_StrictModel):
    expected_asset_version: int = Field(ge=1)


class SystemBindingRequest(_StrictModel):
    asset_id: uuid.UUID
    version_id: uuid.UUID
    expected_binding_version: int | None = Field(default=None, ge=1)


class MoveSystemBindingRequest(_StrictModel):
    version_id: uuid.UUID
    expected_binding_version: int = Field(ge=1)


class DisableSystemBindingRequest(_StrictModel):
    expected_binding_version: int = Field(ge=1)


class BindingResponse(_StrictModel):
    project_id: uuid.UUID
    kind: AssetKind
    asset_id: uuid.UUID
    version_id: uuid.UUID
    enabled: bool
    version: int
    created_by_user_id: str
    updated_by_user_id: str
    created_at: datetime
    updated_at: datetime
    request_id: str


class AgentVersionRequest(_StrictModel):
    description: str = ""
    soul: str = ""
    model_ref: str = ""
    tool_groups: list[str] = Field(default_factory=list)
    skill_version_ids: list[uuid.UUID] = Field(default_factory=list)
    mcp_version_ids: list[uuid.UUID] = Field(default_factory=list)
    expected_asset_version: int = Field(ge=1)


class SkillFileRequest(_StrictModel):
    path: str
    content_base64: str
    media_type: str = "application/octet-stream"


class SkillVersionRequest(_StrictModel):
    files: list[SkillFileRequest]
    expected_asset_version: int = Field(ge=1)


class McpSlotRequest(_StrictModel):
    name: str
    purpose: str = ""
    payload_schema: dict[str, list[str]]
    required: bool = True


class McpVersionRequest(_StrictModel):
    description: str = ""
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    oauth: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, Any] = Field(default_factory=dict)
    tool_overrides: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 30
    credential_slots: list[McpSlotRequest] = Field(default_factory=list)
    expected_asset_version: int = Field(ge=1)


class McpApproveRequest(_StrictModel):
    credential_versions: dict[str, uuid.UUID]
    expected_asset_version: int = Field(ge=1)


class CredentialCreateRequest(_StrictModel):
    name: str
    display_name: str
    credential_type: str
    payload: dict[str, dict[str, str]]


class CredentialReplaceRequest(_StrictModel):
    payload: dict[str, dict[str, str]]
    expected_credential_version: int = Field(ge=1)


class CredentialRevokeRequest(_StrictModel):
    expected_credential_version: int = Field(ge=1)


class VersionResponse(_StrictModel):
    data: dict[str, Any]
    request_id: str


ASSET_ERRORS = (
    AssetNotFound,
    AssetForbidden,
    AssetConflict,
    AssetValidationFailed,
    AssetStorageUnavailable,
)


def raise_asset_domain(exc: SharedAssetError, request_id: str | None = None) -> NoReturn:
    known = {
        AssetNotFound: 404,
        AssetForbidden: 403,
        AssetConflict: 409,
        AssetValidationFailed: 422,
        AssetStorageUnavailable: 503,
    }
    status_code = known.get(type(exc))
    if status_code is None:
        raise exc
    raise HTTPException(
        status_code,
        detail={
            "code": exc.code,
            "message": exc.public_message,
            "request_id": request_id or exc.request_id,
        },
    ) from None


async def authenticated_asset_identity(
    user=Depends(get_current_user_from_request),
) -> tuple[uuid.UUID, str]:
    return uuid.UUID(str(user.id)), get_current_trace_id() or generate_trace_id()


async def project_asset_context(
    project_id: uuid.UUID,
    identity: Annotated[tuple[uuid.UUID, str], Depends(authenticated_asset_identity)],
    session: Annotated[AsyncSession, Depends(project_session)],
) -> ProjectContext:
    user_id, request_id = identity
    try:
        return await resolve_project_context(session, user_id, project_id, request_id)
    except ProjectNotFound:
        raise_asset_domain(AssetNotFound(request_id))
    except ProjectForbidden:
        raise_asset_domain(AssetForbidden(request_id))
    except ProjectDatabaseUnavailable:
        raise_asset_domain(AssetStorageUnavailable(request_id))


def _factory():
    try:
        return get_session_factory()
    except RuntimeError:
        raise HTTPException(
            503,
            detail={
                "code": AssetStorageUnavailable.code,
                "message": AssetStorageUnavailable.public_message,
                "request_id": get_current_trace_id() or generate_trace_id(),
            },
        ) from None


def get_agent_service() -> AgentService:
    return AgentService(_factory())


def get_skill_service() -> SkillService:
    return SkillService(_factory())


def get_mcp_service() -> McpService:
    return McpService(_factory())


def get_credential_service() -> CredentialService:
    return CredentialService(_factory())


def get_binding_service() -> BindingService:
    return BindingService(_factory())


def _asset_item(view) -> AssetItemResponse:
    return AssetItemResponse.model_validate(view, from_attributes=True)


def _credential_item(view) -> CredentialItemResponse:
    return CredentialItemResponse.model_validate(view, from_attributes=True)


def _scoped_assets(views, request_id: str) -> ScopedAssetListResponse:
    items = [_asset_item(view) for view in views]
    return ScopedAssetListResponse(
        system_items=[item for item in items if item.scope is AssetScope.SYSTEM],
        project_items=[item for item in items if item.scope is AssetScope.PROJECT],
        request_id=request_id,
    )


async def _list_assets(context: ProjectContext, service) -> ScopedAssetListResponse:
    try:
        return _scoped_assets(await service.list_visible(context), context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _asset_call(actor, operation, *, version: bool = False):
    try:
        result = await operation()
        if version:
            return VersionResponse(data=jsonable_encoder(result), request_id=actor.request_id)
        return AssetMutationResponse(item=_asset_item(result), request_id=actor.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


def _decode_skill_files(body: SkillVersionRequest, request_id: str) -> tuple[SkillArchiveFile, ...]:
    try:
        return tuple(
            SkillArchiveFile(
                path=item.path,
                content=base64.b64decode(item.content_base64, validate=True),
                media_type=item.media_type,
            )
            for item in body.files
        )
    except (binascii.Error, ValueError):
        raise_asset_domain(AssetValidationFailed(request_id))


def _mcp_definition(body: McpVersionRequest) -> McpDefinition:
    return McpDefinition(
        description=body.description,
        transport=body.transport,
        command=body.command,
        args=tuple(body.args),
        url=body.url,
        env=dict(body.env),
        headers=dict(body.headers),
        oauth=dict(body.oauth),
        routing=dict(body.routing),
        tool_overrides=dict(body.tool_overrides),
        timeout_seconds=body.timeout_seconds,
        credential_slots=tuple(
            McpCredentialSlot(
                name=slot.name,
                purpose=slot.purpose,
                payload_schema={key: tuple(values) for key, values in slot.payload_schema.items()},
                required=slot.required,
            )
            for slot in body.credential_slots
        ),
    )


def register_asset_mutation_routes(router: APIRouter, actor_dependency) -> None:
    async def create_agent(body: CreateAssetRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        return await _asset_call(actor, lambda: service.create_asset(actor, CreateAgent(body.slug, body.display_name)))

    async def get_agent(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        return await _asset_call(actor, lambda: service.get(actor, asset_id))

    async def create_agent_version(asset_id: uuid.UUID, body: AgentVersionRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        payload = AgentPayload(
            body.description,
            body.soul,
            body.model_ref,
            tuple(body.tool_groups),
            tuple(body.skill_version_ids),
            tuple(body.mcp_version_ids),
        )
        return await _asset_call(actor, lambda: service.create_version(actor, asset_id, payload, expected_asset_version=body.expected_asset_version), version=True)

    async def publish_agent(asset_id: uuid.UUID, version_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        return await _asset_call(actor, lambda: service.publish(actor, asset_id, version_id, expected_asset_version=body.expected_asset_version), version=True)

    async def create_skill(body: CreateAssetRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _asset_call(actor, lambda: service.create_asset(actor, CreateSkill(body.slug, body.display_name)))

    async def get_skill(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _asset_call(actor, lambda: service.get(actor, asset_id))

    async def create_skill_version(asset_id: uuid.UUID, body: SkillVersionRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        files = _decode_skill_files(body, actor.request_id)
        return await _asset_call(actor, lambda: service.create_version_from_archive(actor, asset_id, files, expected_asset_version=body.expected_asset_version), version=True)

    async def publish_skill(asset_id: uuid.UUID, version_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _asset_call(actor, lambda: service.publish(actor, asset_id, version_id, expected_asset_version=body.expected_asset_version), version=True)

    async def create_mcp(body: CreateAssetRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.create_asset(actor, CreateMcpServer(body.slug, body.display_name)))

    async def get_mcp(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.get(actor, asset_id))

    async def create_mcp_version(asset_id: uuid.UUID, body: McpVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.create_version(actor, asset_id, _mcp_definition(body), expected_asset_version=body.expected_asset_version), version=True)

    async def publish_mcp(asset_id: uuid.UUID, version_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.publish(actor, asset_id, version_id, expected_asset_version=body.expected_asset_version), version=True)

    async def submit_mcp(asset_id: uuid.UUID, version_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.submit_approval(actor, asset_id, version_id, expected_asset_version=body.expected_asset_version), version=True)

    async def approve_mcp(asset_id: uuid.UUID, version_id: uuid.UUID, body: McpApproveRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.approve(actor, asset_id, version_id, body.credential_versions, expected_asset_version=body.expected_asset_version), version=True)

    def add_status_routes(segment: str, service_dependency):
        async def change(asset_id: uuid.UUID, action: str, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(service_dependency)):
            if action not in {"archive", "suspend"}:
                raise_asset_domain(AssetNotFound(actor.request_id))
            return await _asset_call(actor, lambda: getattr(service, action)(actor, asset_id, expected_asset_version=body.expected_asset_version))

        router.add_api_route(f"/{segment}/{{asset_id}}/{{action}}", change, methods=["POST"], response_model=AssetMutationResponse, name=f"change_{segment}_status")

    async def create_credential(body: CredentialCreateRequest, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        try:
            view = await service.create(actor, CreateCredential(body.name, body.display_name, body.credential_type), body.payload)
            return CredentialMutationResponse(item=_credential_item(view), request_id=actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def get_credential(credential_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        try:
            view = await service.get(actor, credential_id)
            return CredentialMutationResponse(item=_credential_item(view), request_id=actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def replace_credential(credential_id: uuid.UUID, body: CredentialReplaceRequest, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        return await _asset_call(actor, lambda: service.replace(actor, credential_id, body.payload, expected_credential_version=body.expected_credential_version), version=True)

    async def revoke_credential(credential_id: uuid.UUID, body: CredentialRevokeRequest, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        try:
            view = await service.revoke(actor, credential_id, expected_credential_version=body.expected_credential_version)
            return CredentialMutationResponse(item=_credential_item(view), request_id=actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    for path, endpoint, methods, response_model, code in (
        ("/agents", create_agent, ["POST"], AssetMutationResponse, 201),
        ("/agents/{asset_id}", get_agent, ["GET"], AssetMutationResponse, 200),
        ("/agents/{asset_id}/versions", create_agent_version, ["POST"], VersionResponse, 201),
        ("/agents/{asset_id}/versions/{version_id}/publish", publish_agent, ["POST"], VersionResponse, 200),
        ("/skills", create_skill, ["POST"], AssetMutationResponse, 201),
        ("/skills/{asset_id}", get_skill, ["GET"], AssetMutationResponse, 200),
        ("/skills/{asset_id}/versions", create_skill_version, ["POST"], VersionResponse, 201),
        ("/skills/{asset_id}/versions/{version_id}/publish", publish_skill, ["POST"], VersionResponse, 200),
        ("/mcp-servers", create_mcp, ["POST"], AssetMutationResponse, 201),
        ("/mcp-servers/{asset_id}", get_mcp, ["GET"], AssetMutationResponse, 200),
        ("/mcp-servers/{asset_id}/versions", create_mcp_version, ["POST"], VersionResponse, 201),
        ("/mcp-servers/{asset_id}/versions/{version_id}/publish", publish_mcp, ["POST"], VersionResponse, 200),
        ("/mcp-servers/{asset_id}/versions/{version_id}/submit-approval", submit_mcp, ["POST"], VersionResponse, 200),
        ("/mcp-servers/{asset_id}/versions/{version_id}/approve", approve_mcp, ["POST"], VersionResponse, 200),
        ("/credentials", create_credential, ["POST"], CredentialMutationResponse, 201),
        ("/credentials/{credential_id}", get_credential, ["GET"], CredentialMutationResponse, 200),
        ("/credentials/{credential_id}/replace", replace_credential, ["POST"], VersionResponse, 200),
        ("/credentials/{credential_id}/revoke", revoke_credential, ["POST"], CredentialMutationResponse, 200),
    ):
        router.add_api_route(path, endpoint, methods=methods, response_model=response_model, status_code=code)
    for segment, dependency in (("agents", get_agent_service), ("skills", get_skill_service), ("mcp-servers", get_mcp_service)):
        add_status_routes(segment, dependency)


@project_router.get("/agents", response_model=ScopedAssetListResponse)
async def list_project_agents(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentService, Depends(get_agent_service)],
):
    return await _list_assets(context, service)


@project_router.get("/skills", response_model=ScopedAssetListResponse)
async def list_project_skills(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    return await _list_assets(context, service)


@project_router.get("/mcp-servers", response_model=ScopedAssetListResponse)
async def list_project_mcp_servers(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    return await _list_assets(context, service)


@project_router.get("/credentials", response_model=ScopedCredentialListResponse)
async def list_project_credentials(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[CredentialService, Depends(get_credential_service)],
):
    try:
        items = [_credential_item(view) for view in await service.list_visible(context)]
        return ScopedCredentialListResponse(
            system_items=[item for item in items if item.scope is AssetScope.SYSTEM],
            project_items=[item for item in items if item.scope is AssetScope.PROJECT],
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


_BINDING_KINDS = {
    "agent": AssetKind.AGENT,
    "skill": AssetKind.SKILL,
    "mcp": AssetKind.MCP,
}


def _binding_response(view, request_id: str) -> BindingResponse:
    return BindingResponse(**vars(view), request_id=request_id)


def _register_binding_routes(segment: str, kind: AssetKind) -> None:
    path = f"/system-{segment}-bindings"

    async def enable(
        body: SystemBindingRequest,
        context: Annotated[ProjectContext, Depends(project_asset_context)],
        service: Annotated[BindingService, Depends(get_binding_service)],
    ):
        try:
            view = await service.enable(
                context,
                AssetSelection(kind, body.asset_id, body.version_id),
                expected_binding_version=body.expected_binding_version,
            )
            return _binding_response(view, context.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def move(
        asset_id: uuid.UUID,
        action: str,
        body: MoveSystemBindingRequest,
        context: Annotated[ProjectContext, Depends(project_asset_context)],
        service: Annotated[BindingService, Depends(get_binding_service)],
    ):
        try:
            method: Callable = getattr(service, action)
            view = await method(
                context,
                AssetSelection(kind, asset_id, body.version_id),
                expected_binding_version=body.expected_binding_version,
            )
            return _binding_response(view, context.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def upgrade(
        asset_id: uuid.UUID,
        body: MoveSystemBindingRequest,
        context: Annotated[ProjectContext, Depends(project_asset_context)],
        service: Annotated[BindingService, Depends(get_binding_service)],
    ):
        return await move(asset_id, "upgrade", body, context, service)

    async def rollback(
        asset_id: uuid.UUID,
        body: MoveSystemBindingRequest,
        context: Annotated[ProjectContext, Depends(project_asset_context)],
        service: Annotated[BindingService, Depends(get_binding_service)],
    ):
        return await move(asset_id, "rollback", body, context, service)

    async def disable(
        asset_id: uuid.UUID,
        body: DisableSystemBindingRequest,
        context: Annotated[ProjectContext, Depends(project_asset_context)],
        service: Annotated[BindingService, Depends(get_binding_service)],
    ):
        try:
            view = await service.disable(
                context,
                AssetSelection(kind, asset_id),
                expected_binding_version=body.expected_binding_version,
            )
            return _binding_response(view, context.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    project_router.add_api_route(path, enable, methods=["POST"], response_model=BindingResponse, status_code=status.HTTP_201_CREATED, name=f"enable_system_{segment}_binding")
    project_router.add_api_route(f"{path}/{{asset_id}}/disable", disable, methods=["POST"], response_model=BindingResponse, name=f"disable_system_{segment}_binding")
    project_router.add_api_route(f"{path}/{{asset_id}}/upgrade", upgrade, methods=["POST"], response_model=BindingResponse, name=f"upgrade_system_{segment}_binding")
    project_router.add_api_route(f"{path}/{{asset_id}}/rollback", rollback, methods=["POST"], response_model=BindingResponse, name=f"rollback_system_{segment}_binding")


for _segment, _kind in _BINDING_KINDS.items():
    _register_binding_routes(_segment, _kind)


register_asset_mutation_routes(project_router, project_asset_context)
