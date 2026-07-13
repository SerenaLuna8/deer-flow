from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.gateway.deps import get_current_user_from_request, require_admin_user
from app.gateway.routers.project_assets import (
    ASSET_ERRORS,
    AssetItemResponse,
    AssetRoute,
    BindingResponse,
    CredentialItemResponse,
    DisableSystemBindingRequest,
    MoveSystemBindingRequest,
    SystemBindingRequest,
    _asset_item,
    _credential_item,
    _StrictModel,
    get_agent_service,
    get_binding_service,
    get_credential_service,
    get_mcp_service,
    get_skill_service,
    raise_asset_domain,
    register_asset_mutation_routes,
)
from app.shared_assets import AgentService, AssetKind, AssetSelection, CredentialService, McpService, SkillService
from app.shared_assets.contexts import SystemAssetGovernanceContext, resolve_asset_actor
from app.shared_assets.errors import AssetForbidden
from deerflow.trace_context import generate_trace_id, get_current_trace_id

admin_router = APIRouter(
    prefix="/api/admin/assets",
    tags=["admin-assets"],
    route_class=AssetRoute,
)
admin_project_router = APIRouter(
    prefix="/api/admin/projects/{project_id}/assets",
    tags=["admin-project-assets"],
    route_class=AssetRoute,
)


class AdminAssetListResponse(_StrictModel):
    items: list[AssetItemResponse]
    request_id: str


class AdminCredentialListResponse(_StrictModel):
    items: list[CredentialItemResponse]
    request_id: str


async def _admin_actor(
    request: Request,
    user=Depends(get_current_user_from_request),
) -> SystemAssetGovernanceContext:
    request.state.user = user
    await require_admin_user(request, detail="System administrator privileges required.")
    request_id = get_current_trace_id() or generate_trace_id()
    try:
        return resolve_asset_actor(user, request_id=request_id)
    except AssetForbidden as exc:
        raise_asset_domain(exc)


async def _admin_project_actor(
    project_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user_from_request),
) -> SystemAssetGovernanceContext:
    request.state.user = user
    await require_admin_user(request, detail="System administrator privileges required.")
    request_id = get_current_trace_id() or generate_trace_id()
    try:
        return resolve_asset_actor(user, request_id=request_id, project_id=project_id)
    except AssetForbidden as exc:
        raise_asset_domain(exc)


async def _list_assets(actor: SystemAssetGovernanceContext, service) -> AdminAssetListResponse:
    try:
        return AdminAssetListResponse(
            items=[_asset_item(view) for view in await service.list_visible(actor)],
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _list_credentials(
    actor: SystemAssetGovernanceContext,
    service: CredentialService,
) -> AdminCredentialListResponse:
    try:
        return AdminCredentialListResponse(
            items=[_credential_item(view) for view in await service.list_visible(actor)],
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@admin_router.get("/agents", response_model=AdminAssetListResponse)
async def list_system_agents(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[AgentService, Depends(get_agent_service)],
):
    return await _list_assets(actor, service)


@admin_router.get("/skills", response_model=AdminAssetListResponse)
async def list_system_skills(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    return await _list_assets(actor, service)


@admin_router.get("/mcp-servers", response_model=AdminAssetListResponse)
async def list_system_mcp_servers(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    return await _list_assets(actor, service)


@admin_router.get("/credentials", response_model=AdminCredentialListResponse)
async def list_system_credentials(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[CredentialService, Depends(get_credential_service)],
):
    return await _list_credentials(actor, service)


@admin_project_router.get("/agents", response_model=AdminAssetListResponse)
async def list_override_agents(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_project_actor)],
    service: Annotated[AgentService, Depends(get_agent_service)],
):
    return await _list_assets(actor, service)


@admin_project_router.get("/skills", response_model=AdminAssetListResponse)
async def list_override_skills(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_project_actor)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    return await _list_assets(actor, service)


@admin_project_router.get("/mcp-servers", response_model=AdminAssetListResponse)
async def list_override_mcp_servers(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_project_actor)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    return await _list_assets(actor, service)


@admin_project_router.get("/credentials", response_model=AdminCredentialListResponse)
async def list_override_credentials(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_project_actor)],
    service: Annotated[CredentialService, Depends(get_credential_service)],
):
    return await _list_credentials(actor, service)


register_asset_mutation_routes(admin_router, _admin_actor)
register_asset_mutation_routes(admin_project_router, _admin_project_actor)


def _register_override_binding_routes(segment: str, kind: AssetKind) -> None:
    path = f"/system-{segment}-bindings"

    async def enable(body: SystemBindingRequest, actor=Depends(_admin_project_actor), service=Depends(get_binding_service)):
        try:
            view = await service.enable(
                actor,
                AssetSelection(kind, body.asset_id, body.version_id),
                expected_binding_version=body.expected_binding_version,
            )
            return BindingResponse(**vars(view), request_id=actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def move(asset_id: uuid.UUID, body: MoveSystemBindingRequest, action: str, actor, service):
        try:
            view = await getattr(service, action)(
                actor,
                AssetSelection(kind, asset_id, body.version_id),
                expected_binding_version=body.expected_binding_version,
            )
            return BindingResponse(**vars(view), request_id=actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def upgrade(asset_id: uuid.UUID, body: MoveSystemBindingRequest, actor=Depends(_admin_project_actor), service=Depends(get_binding_service)):
        return await move(asset_id, body, "upgrade", actor, service)

    async def rollback(asset_id: uuid.UUID, body: MoveSystemBindingRequest, actor=Depends(_admin_project_actor), service=Depends(get_binding_service)):
        return await move(asset_id, body, "rollback", actor, service)

    async def disable(asset_id: uuid.UUID, body: DisableSystemBindingRequest, actor=Depends(_admin_project_actor), service=Depends(get_binding_service)):
        try:
            view = await service.disable(
                actor,
                AssetSelection(kind, asset_id),
                expected_binding_version=body.expected_binding_version,
            )
            return BindingResponse(**vars(view), request_id=actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    admin_project_router.add_api_route(path, enable, methods=["POST"], response_model=BindingResponse, status_code=201)
    admin_project_router.add_api_route(f"{path}/{{asset_id}}/upgrade", upgrade, methods=["POST"], response_model=BindingResponse)
    admin_project_router.add_api_route(f"{path}/{{asset_id}}/rollback", rollback, methods=["POST"], response_model=BindingResponse)
    admin_project_router.add_api_route(f"{path}/{{asset_id}}/disable", disable, methods=["POST"], response_model=BindingResponse)


for _segment, _kind in (("agent", AssetKind.AGENT), ("skill", AssetKind.SKILL), ("mcp", AssetKind.MCP)):
    _register_override_binding_routes(_segment, _kind)
