from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.gateway.routers.project_asset_routes.common import (
    ASSET_ERRORS,
    AssetRoute,
    _agent_asset_item,
    _agent_definition_response,
    _asset_item,
    _current_version_asset_item,
    _version_history,
    get_agent_service,
    get_mcp_service,
    get_skill_service,
    raise_asset_domain,
    system_asset_catalog_actor,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentDefinitionResponse,
    SkillVersionHistoryResponse,
    SystemAgentCatalogResponse,
    SystemAssetCatalogResponse,
    SystemCurrentVersionCatalogResponse,
)
from app.shared_assets import AgentService, McpService, SkillService
from app.shared_assets.contexts import SystemAssetReadContext

catalog_router = APIRouter(
    prefix="/api/assets/catalog",
    tags=["asset-catalog"],
    route_class=AssetRoute,
)


async def _list_system_catalog(
    actor: SystemAssetReadContext,
    service,
) -> SystemAssetCatalogResponse:
    try:
        return SystemAssetCatalogResponse(
            items=[_asset_item(view) for view in await service.list_visible(actor)],
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _list_system_current_version_catalog(
    actor: SystemAssetReadContext,
    service,
) -> SystemCurrentVersionCatalogResponse:
    try:
        return SystemCurrentVersionCatalogResponse(
            items=[_current_version_asset_item(view) for view in await service.list_visible(actor)],
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _list_system_agent_catalog(
    actor: SystemAssetReadContext,
    service: AgentService,
) -> SystemAgentCatalogResponse:
    try:
        return SystemAgentCatalogResponse(
            items=[_agent_asset_item(view) for view in await service.list_visible(actor)],
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@catalog_router.get(
    "/agents",
    response_model=SystemAgentCatalogResponse,
)
async def list_system_catalog_agents(
    actor: Annotated[SystemAssetReadContext, Depends(system_asset_catalog_actor)],
    service: Annotated[AgentService, Depends(get_agent_service)],
):
    return await _list_system_agent_catalog(actor, service)


@catalog_router.get(
    "/skills",
    response_model=SystemCurrentVersionCatalogResponse,
)
async def list_system_catalog_skills(
    actor: Annotated[SystemAssetReadContext, Depends(system_asset_catalog_actor)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    return await _list_system_current_version_catalog(actor, service)


@catalog_router.get(
    "/agents/{asset_id}",
    response_model=AgentDefinitionResponse,
)
async def get_system_catalog_agent(
    asset_id: uuid.UUID,
    actor: Annotated[SystemAssetReadContext, Depends(system_asset_catalog_actor)],
    service: Annotated[AgentService, Depends(get_agent_service)],
):
    try:
        return _agent_definition_response(
            await service.get(actor, asset_id),
            actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@catalog_router.get(
    "/skills/{asset_id}/versions",
    response_model=SkillVersionHistoryResponse,
)
async def list_system_catalog_skill_versions(
    asset_id: uuid.UUID,
    actor: Annotated[SystemAssetReadContext, Depends(system_asset_catalog_actor)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    """Return the immutable Current Version definition for a visible System Skill."""

    return await _version_history(
        actor,
        lambda: service.get_version_history(actor, asset_id),
        SkillVersionHistoryResponse,
    )


@catalog_router.get("/mcp-servers", response_model=SystemAssetCatalogResponse)
async def list_system_catalog_mcp_servers(
    actor: Annotated[SystemAssetReadContext, Depends(system_asset_catalog_actor)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    return await _list_system_catalog(actor, service)
