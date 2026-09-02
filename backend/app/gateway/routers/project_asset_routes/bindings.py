from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.projects.context import ProjectContext
from app.shared_assets import AssetKind, AssetSelection, BindingService

from .common import (
    ASSET_ERRORS,
    get_binding_service,
    project_asset_context,
    raise_asset_domain,
)
from .contracts import (
    AgentBindingResponse,
    BindingResponse,
    CurrentBindingResponse,
    CurrentSystemBindingRequest,
    DisableSystemBindingRequest,
    MoveSystemBindingRequest,
    SyncCurrentSystemMcpBindingRequest,
    SystemBindingRequest,
)

_BINDING_KINDS = {
    "agent": AssetKind.AGENT,
    "skill": AssetKind.SKILL,
    "mcp": AssetKind.MCP,
}


def _binding_response(
    view,
    request_id: str,
) -> AgentBindingResponse | BindingResponse | CurrentBindingResponse:
    values = vars(view)
    if view.kind is AssetKind.MCP:
        return BindingResponse(**values, request_id=request_id)
    if view.kind is AssetKind.AGENT:
        return AgentBindingResponse(
            **{key: value for key, value in values.items() if key != "version_id"},
            definition_id=view.version_id,
            request_id=request_id,
        )
    return CurrentBindingResponse(
        **{key: value for key, value in values.items() if key != "version_id"},
        current_version_id=view.version_id,
        request_id=request_id,
    )


def _register_binding_routes(router: APIRouter, segment: str, kind: AssetKind) -> None:
    path = f"/system-{segment}-bindings"

    async def enable_exact(
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

    async def enable_current(
        body: CurrentSystemBindingRequest,
        context: Annotated[ProjectContext, Depends(project_asset_context)],
        service: Annotated[BindingService, Depends(get_binding_service)],
    ):
        try:
            view = await service.enable(
                context,
                AssetSelection(kind, body.asset_id),
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

    async def sync_current_mcp(
        asset_id: uuid.UUID,
        body: SyncCurrentSystemMcpBindingRequest,
        context: Annotated[ProjectContext, Depends(project_asset_context)],
        service: Annotated[BindingService, Depends(get_binding_service)],
    ):
        try:
            view = await service.sync_current_mcp(
                context,
                asset_id,
                expected_binding_version=body.expected_binding_version,
            )
            return _binding_response(view, context.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    response_model = BindingResponse if kind is AssetKind.MCP else AgentBindingResponse if kind is AssetKind.AGENT else CurrentBindingResponse
    router.add_api_route(
        path,
        enable_exact if kind is AssetKind.MCP else enable_current,
        methods=["POST"],
        response_model=response_model,
        status_code=status.HTTP_201_CREATED,
        name=f"enable_system_{segment}_binding",
    )
    router.add_api_route(f"{path}/{{asset_id}}/disable", disable, methods=["POST"], response_model=response_model, name=f"disable_system_{segment}_binding")
    if kind is AssetKind.MCP:
        router.add_api_route(f"{path}/{{asset_id}}/upgrade", upgrade, methods=["POST"], response_model=BindingResponse, name=f"upgrade_system_{segment}_binding")
        router.add_api_route(f"{path}/{{asset_id}}/rollback", rollback, methods=["POST"], response_model=BindingResponse, name=f"rollback_system_{segment}_binding")
        router.add_api_route(
            f"{path}/{{asset_id}}/sync-current",
            sync_current_mcp,
            methods=["POST"],
            response_model=BindingResponse,
            name="sync_current_system_mcp_binding",
        )


def register_binding_routes(router: APIRouter) -> None:
    for segment, kind in _BINDING_KINDS.items():
        _register_binding_routes(router, segment, kind)
