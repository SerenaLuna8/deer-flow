from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StrictInt

from app.gateway.deps import get_current_user_from_request, require_admin_user
from app.gateway.routers.project_assets import (
    ASSET_ERRORS,
    AssetItemResponse,
    AssetRoute,
    BindingItemResponse,
    BindingResponse,
    CredentialItemResponse,
    CurrentBindingResponse,
    CurrentSystemBindingRequest,
    CurrentVersionAssetItemResponse,
    DisableSystemBindingRequest,
    McpVersionResponse,
    MoveSystemBindingRequest,
    ProjectAssetItemResponse,
    ProjectCredentialItemResponse,
    ProjectCurrentVersionAssetItemResponse,
    ProjectCurrentVersionSkillItemResponse,
    ScopedAssetListResponse,
    ScopedCredentialListResponse,
    ScopedCurrentVersionAssetListResponse,
    ScopedCurrentVersionSkillAssetListResponse,
    SkillVersionResponse,
    SystemBindingRequest,
    SystemMcpCredentialGrantRequest,
    _asset_item,
    _binding_response,
    _credential_item,
    _current_version_asset_item,
    _StrictModel,
    _version_call,
    get_agent_service,
    get_binding_service,
    get_credential_service,
    get_mcp_service,
    get_skill_service,
    raise_asset_domain,
    register_asset_routes,
)
from app.projects.capabilities import Capability
from app.shared_assets import AgentService, AssetKind, AssetSelection, BindingService, CredentialService, McpService, SkillService
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext, resolve_asset_actor
from app.shared_assets.errors import AssetForbidden, AssetValidationFailed
from app.shared_assets.models import AssetScope
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


class AdminCurrentVersionAssetListResponse(_StrictModel):
    items: list[CurrentVersionAssetItemResponse]
    request_id: str


class AdminCredentialListResponse(_StrictModel):
    items: list[CredentialItemResponse]
    request_id: str


class CredentialRotationStatusResponse(_StrictModel):
    eligible_total: int
    current: int
    pending: int
    status: Literal["current", "pending"]


class SystemSkillVersionRevocationRequest(_StrictModel):
    expected_revision: StrictInt = Field(ge=1)
    reason_code: Literal["security", "policy", "integrity"]


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


async def _list_assets(
    actor: SystemAssetGovernanceContext,
    service,
    *,
    current_version: bool = False,
) -> AdminAssetListResponse | AdminCurrentVersionAssetListResponse:
    try:
        views = await service.list_visible(actor)
        if current_version:
            return AdminCurrentVersionAssetListResponse(
                items=[_current_version_asset_item(view) for view in views],
                request_id=actor.request_id,
            )
        return AdminAssetListResponse(
            items=[_asset_item(view) for view in views],
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


def _override_asset_capabilities(scope: AssetScope, kind: AssetKind) -> list[Capability]:
    allowed = {
        Capability.SHARED_ASSETS_READ,
        Capability.SHARED_ASSETS_EXECUTE,
        Capability.SHARED_ASSETS_MANAGE_BINDINGS,
    }
    if scope is AssetScope.PROJECT:
        allowed.add(Capability.SHARED_ASSETS_EDIT)
        if kind in {AssetKind.MCP, AssetKind.SKILL}:
            allowed.add(Capability.MCP_CREDENTIALS_APPROVE)
    return sorted(allowed, key=str)


async def _list_override_assets(
    actor: SystemAssetGovernanceContext,
    kind: AssetKind,
    service,
    binding_service,
) -> ScopedAssetListResponse | ScopedCurrentVersionAssetListResponse | ScopedCurrentVersionSkillAssetListResponse:
    try:
        project_views = await service.list_visible(actor)
        catalog_actor = SystemAssetReadContext(
            user_id=actor.user_id,
            request_id=actor.request_id,
        )
        system_views = await service.list_visible(catalog_actor)
        bindings = await binding_service.list_visible(actor, kind)
        if any(view.scope is not AssetScope.PROJECT or view.project_id != actor.project_id for view in project_views):
            raise AssetValidationFailed(actor.request_id)
        if any(view.scope is not AssetScope.SYSTEM or view.project_id is not None for view in system_views):
            raise AssetValidationFailed(actor.request_id)
        by_asset_id = {binding.asset_id: binding for binding in bindings}
        if kind is AssetKind.SKILL:
            item_model = ProjectCurrentVersionSkillItemResponse
            response_model = ScopedCurrentVersionSkillAssetListResponse
        elif kind is AssetKind.AGENT:
            item_model = ProjectCurrentVersionAssetItemResponse
            response_model = ScopedCurrentVersionAssetListResponse
        else:
            item_model = ProjectAssetItemResponse
            response_model = ScopedAssetListResponse
        system_items = [
            item_model(
                **vars(view),
                capabilities=_override_asset_capabilities(view.scope, kind),
                binding=(BindingItemResponse(**vars(by_asset_id[view.id])) if view.id in by_asset_id else None),
            )
            for view in system_views
        ]
        project_items = [
            item_model(
                **vars(view),
                capabilities=_override_asset_capabilities(view.scope, kind),
                binding=None,
            )
            for view in project_views
        ]
        return response_model(
            system_items=system_items,
            project_items=project_items,
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _scoped_override_credentials(
    actor: SystemAssetGovernanceContext,
    service: CredentialService,
) -> ScopedCredentialListResponse:
    try:
        views = await service.list_visible(actor)
        if any(view.scope is not AssetScope.PROJECT or view.project_id != actor.project_id for view in views):
            raise AssetValidationFailed(actor.request_id)
        return ScopedCredentialListResponse(
            system_items=[],
            project_items=[
                ProjectCredentialItemResponse(
                    **vars(view),
                    capabilities=[
                        Capability.SHARED_ASSETS_READ,
                        Capability.MCP_CREDENTIALS_APPROVE,
                    ],
                )
                for view in views
            ],
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@admin_router.get(
    "/agents",
    response_model=AdminCurrentVersionAssetListResponse,
)
async def list_system_agents(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[AgentService, Depends(get_agent_service)],
):
    return await _list_assets(actor, service, current_version=True)


@admin_router.get(
    "/skills",
    response_model=AdminCurrentVersionAssetListResponse,
)
async def list_system_skills(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    return await _list_assets(actor, service, current_version=True)


@admin_router.post(
    "/skills/{asset_id}/versions/{version_id}/revoke",
    response_model=SkillVersionResponse,
)
async def revoke_system_skill_version(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    body: SystemSkillVersionRevocationRequest,
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    return await _version_call(
        actor,
        lambda: service.revoke_version(
            actor,
            asset_id,
            version_id,
            expected_asset_version=body.expected_revision,
            reason_code=body.reason_code,
        ),
        SkillVersionResponse,
    )


@admin_router.get("/mcp-servers", response_model=AdminAssetListResponse)
async def list_system_mcp_servers(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    return await _list_assets(actor, service)


@admin_router.post(
    "/mcp-servers/{asset_id}/versions/{version_id}/credential-grants",
    response_model=McpVersionResponse,
)
async def configure_system_mcp_credential_grants(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    body: SystemMcpCredentialGrantRequest,
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    return await _version_call(
        actor,
        lambda: service.configure_system_credential_grants(
            actor,
            asset_id,
            version_id,
            body.credential_versions,
            body.expected_active_grant_versions,
        ),
        McpVersionResponse,
    )


@admin_router.get("/credentials", response_model=AdminCredentialListResponse)
async def list_system_credentials(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[CredentialService, Depends(get_credential_service)],
):
    return await _list_credentials(actor, service)


@admin_router.get(
    "/credentials/rotation-status",
    response_model=CredentialRotationStatusResponse,
)
async def get_system_credential_rotation_status(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_actor)],
    service: Annotated[CredentialService, Depends(get_credential_service)],
):
    try:
        view = await service.rotation_status(actor)
        return CredentialRotationStatusResponse(**vars(view))
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@admin_project_router.get(
    "/agents",
    response_model=ScopedCurrentVersionAssetListResponse,
)
async def list_override_agents(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_project_actor)],
    service: Annotated[AgentService, Depends(get_agent_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_override_assets(actor, AssetKind.AGENT, service, binding_service)


@admin_project_router.get(
    "/skills",
    response_model=ScopedCurrentVersionSkillAssetListResponse,
)
async def list_override_skills(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_project_actor)],
    service: Annotated[SkillService, Depends(get_skill_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_override_assets(actor, AssetKind.SKILL, service, binding_service)


@admin_project_router.get("/mcp-servers", response_model=ScopedAssetListResponse)
async def list_override_mcp_servers(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_project_actor)],
    service: Annotated[McpService, Depends(get_mcp_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_override_assets(actor, AssetKind.MCP, service, binding_service)


@admin_project_router.get("/credentials", response_model=ScopedCredentialListResponse)
async def list_override_credentials(
    actor: Annotated[SystemAssetGovernanceContext, Depends(_admin_project_actor)],
    service: Annotated[CredentialService, Depends(get_credential_service)],
):
    return await _scoped_override_credentials(actor, service)


register_asset_routes(
    admin_router,
    _admin_actor,
    include_shared_asset_mutations=False,
)
register_asset_routes(
    admin_project_router,
    _admin_project_actor,
    include_skill_export=False,
)


def _register_override_binding_routes(segment: str, kind: AssetKind) -> None:
    path = f"/system-{segment}-bindings"

    async def enable_exact(body: SystemBindingRequest, actor=Depends(_admin_project_actor), service=Depends(get_binding_service)):
        try:
            view = await service.enable(
                actor,
                AssetSelection(kind, body.asset_id, body.version_id),
                expected_binding_version=body.expected_binding_version,
            )
            return _binding_response(view, actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def enable_current(body: CurrentSystemBindingRequest, actor=Depends(_admin_project_actor), service=Depends(get_binding_service)):
        try:
            view = await service.enable(
                actor,
                AssetSelection(kind, body.asset_id),
                expected_binding_version=body.expected_binding_version,
            )
            return _binding_response(view, actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def move(asset_id: uuid.UUID, body: MoveSystemBindingRequest, action: str, actor, service):
        try:
            view = await getattr(service, action)(
                actor,
                AssetSelection(kind, asset_id, body.version_id),
                expected_binding_version=body.expected_binding_version,
            )
            return _binding_response(view, actor.request_id)
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
            return _binding_response(view, actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    response_model = BindingResponse if kind is AssetKind.MCP else CurrentBindingResponse
    admin_project_router.add_api_route(
        path,
        enable_exact if kind is AssetKind.MCP else enable_current,
        methods=["POST"],
        response_model=response_model,
        status_code=201,
    )
    admin_project_router.add_api_route(f"{path}/{{asset_id}}/disable", disable, methods=["POST"], response_model=response_model)
    if kind is AssetKind.MCP:
        admin_project_router.add_api_route(f"{path}/{{asset_id}}/upgrade", upgrade, methods=["POST"], response_model=BindingResponse)
        admin_project_router.add_api_route(f"{path}/{{asset_id}}/rollback", rollback, methods=["POST"], response_model=BindingResponse)


for _segment, _kind in (("agent", AssetKind.AGENT), ("skill", AssetKind.SKILL), ("mcp", AssetKind.MCP)):
    _register_override_binding_routes(_segment, _kind)
