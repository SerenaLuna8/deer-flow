from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.projects.context import ProjectContext
from app.shared_assets import (
    AssetKind,
    BindingService,
    CreateMcpServer,
    McpDefinition,
    McpSecretSlot,
    McpService,
)
from app.shared_assets.mcp_secret_service import McpSecretService, McpSecretSetView

from .common import (
    ASSET_ERRORS,
    AssetRoute,
    _asset_item,
    _list_assets,
    _response_data,
    get_binding_service,
    get_mcp_secret_service,
    get_mcp_service,
    project_asset_context,
    raise_asset_domain,
)
from .contracts import (
    McpConfiguredRequest,
    McpConfiguredResponse,
    McpSecretClearRequest,
    McpSecretReplaceRequest,
    McpSecretSetResponse,
    McpToolDiscoveryAttemptItemResponse,
    McpToolDiscoveryAttemptResponse,
    McpToolInventoryItemResponse,
    McpToolInventoryResponse,
    McpVersionItemResponse,
    McpVersionRequest,
    ScopedAssetListResponse,
)

configuration_router = APIRouter(route_class=AssetRoute)
discovery_router = APIRouter(route_class=AssetRoute)


def _mcp_definition(body: McpVersionRequest | McpConfiguredRequest) -> McpDefinition:
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
        secret_slots=tuple(
            McpSecretSlot(
                name=slot.name,
                purpose=slot.purpose,
                payload_schema={key: tuple(values) for key, values in slot.payload_schema.items()},
                required=slot.required,
            )
            for slot in body.secret_slots
        ),
    )


def _configured_mcp_response(result, request_id: str) -> McpConfiguredResponse:
    return McpConfiguredResponse(
        item=_asset_item(result.asset),
        version=McpVersionItemResponse.model_validate(
            _response_data(
                result.version,
                redact_project_mcp=True,
                editable_project_mcp=True,
            )
        ),
        request_id=request_id,
    )


@configuration_router.get("/mcp-servers", response_model=ScopedAssetListResponse)
async def list_project_mcp_servers(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_assets(context, AssetKind.MCP, service, binding_service)


@configuration_router.post(
    "/mcp-servers/configured",
    response_model=McpConfiguredResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_configured_mcp(
    body: McpConfiguredRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    try:
        result = await service.create_project_configured(
            context,
            CreateMcpServer(body.slug, body.display_name),
            _mcp_definition(body),
        )
        return _configured_mcp_response(result, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@configuration_router.put(
    "/mcp-servers/{asset_id}/configured",
    response_model=McpConfiguredResponse,
)
async def update_project_configured_mcp(
    asset_id: uuid.UUID,
    body: McpVersionRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    try:
        result = await service.update_project_configured(
            context,
            asset_id,
            _mcp_definition(body),
            expected_asset_version=body.expected_asset_version,
        )
        return _configured_mcp_response(result, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@configuration_router.get(
    "/mcp-servers/{asset_id}/configured",
    response_model=McpConfiguredResponse,
)
async def get_project_configured_mcp(
    asset_id: uuid.UUID,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = await service.get_project_configured(context, asset_id)
        return _configured_mcp_response(result, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


def _mcp_secret_response(
    value: McpSecretSetView,
    request_id: str,
) -> McpSecretSetResponse:
    return McpSecretSetResponse.model_validate(
        {
            "mcp_server_id": value.mcp_server_id,
            "mcp_server_version_id": value.mcp_server_version_id,
            "revision": value.revision,
            "readiness": value.readiness,
            "slots": [
                {
                    "id": slot.id,
                    "name": slot.name,
                    "purpose": slot.purpose,
                    "payload_schema": {group: list(fields) for group, fields in slot.payload_schema.items()},
                    "required": slot.required,
                    "configured": slot.configured,
                    "revision": slot.revision,
                }
                for slot in value.slots
            ],
            "request_id": request_id,
        }
    )


@configuration_router.get(
    "/mcp-servers/{asset_id}/versions/{version_id}/secrets",
    response_model=McpSecretSetResponse,
)
async def get_project_mcp_secrets(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpSecretService, Depends(get_mcp_secret_service)],
):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        value = await service.get(context, asset_id, version_id)
        return _mcp_secret_response(value, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@configuration_router.put(
    "/mcp-servers/{asset_id}/versions/{version_id}/secrets/{slot_name}",
    response_model=McpSecretSetResponse,
)
async def replace_project_mcp_secret(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    slot_name: str,
    body: McpSecretReplaceRequest,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpSecretService, Depends(get_mcp_secret_service)],
):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        value = await service.replace(
            context,
            asset_id,
            version_id,
            slot_name,
            body.payload,
        )
        return _mcp_secret_response(value, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@configuration_router.post(
    "/mcp-servers/{asset_id}/versions/{version_id}/secrets/{slot_name}/clear",
    response_model=McpSecretSetResponse,
)
async def clear_project_mcp_secret(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    slot_name: str,
    body: McpSecretClearRequest,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpSecretService, Depends(get_mcp_secret_service)],
):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        value = await service.clear(
            context,
            asset_id,
            version_id,
            slot_name,
            confirmed=body.confirmed,
        )
        return _mcp_secret_response(value, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@discovery_router.get(
    "/mcp-servers/{asset_id}/versions/{version_id}/tools",
    response_model=McpToolInventoryResponse,
)
async def get_project_mcp_tool_inventory(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    try:
        view = await service.get_tool_inventory(context, asset_id, version_id)
        response.headers["Cache-Control"] = "private, no-store"
        return McpToolInventoryResponse(
            data=McpToolInventoryItemResponse.model_validate(
                _response_data(view),
            ),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


def _mcp_tool_discovery_attempt_response(
    view: object,
    request_id: str,
) -> McpToolDiscoveryAttemptResponse:
    return McpToolDiscoveryAttemptResponse(
        data=McpToolDiscoveryAttemptItemResponse(
            id=getattr(view, "id"),
            mcp_server_id=getattr(view, "mcp_server_id"),
            mcp_server_version_id=getattr(view, "mcp_server_version_id"),
            status=getattr(view, "status"),
            requested_at=getattr(view, "requested_at"),
            started_at=getattr(view, "started_at"),
            completed_at=getattr(view, "completed_at"),
            error_code=getattr(view, "error_code"),
        ),
        request_id=request_id,
    )


@discovery_router.post(
    "/mcp-servers/{asset_id}/versions/{version_id}/tool-discovery",
    response_model=McpToolDiscoveryAttemptResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_project_mcp_tool_discovery(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    try:
        view = await service.request_tool_discovery(context, asset_id, version_id)
        return _mcp_tool_discovery_attempt_response(view, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@discovery_router.get(
    "/mcp-servers/{asset_id}/versions/{version_id}/tool-discovery",
    response_model=McpToolDiscoveryAttemptResponse,
)
async def get_project_mcp_tool_discovery_attempt(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
    attempt_id: uuid.UUID | None = None,
):
    try:
        view = await service.get_tool_discovery_attempt(
            context,
            asset_id,
            version_id,
            attempt_id=attempt_id,
        )
        return _mcp_tool_discovery_attempt_response(view, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)
