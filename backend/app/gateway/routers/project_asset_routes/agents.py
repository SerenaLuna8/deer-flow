from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.gateway.routers.project_asset_routes.common import (
    ASSET_ERRORS,
    AssetRoute,
    _list_assets,
    get_agent_runtime_assessment_service,
    get_agent_service,
    get_binding_service,
    get_project_default_agent_service,
    project_asset_context,
    raise_asset_domain,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentRuntimeAssessmentItemResponse,
    AgentRuntimeAssessmentsRequest,
    AgentRuntimeAssessmentsResponse,
    ProjectDefaultAgentRequest,
    ProjectDefaultAgentResponse,
    ScopedAgentAssetListResponse,
)
from app.private_work.agent_runtime_assessment import AgentRuntimeAssessmentService
from app.projects.context import ProjectContext
from app.shared_assets import (
    AgentService,
    AssetKind,
    BindingService,
    ProjectDefaultAgentService,
)

router = APIRouter(route_class=AssetRoute)


@router.get(
    "/agents",
    response_model=ScopedAgentAssetListResponse,
)
async def list_project_agents(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentService, Depends(get_agent_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_assets(context, AssetKind.AGENT, service, binding_service)


@router.post(
    "/agents/runtime-assessments",
    response_model=AgentRuntimeAssessmentsResponse,
)
async def assess_project_agent_runtime(
    body: AgentRuntimeAssessmentsRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        AgentRuntimeAssessmentService,
        Depends(get_agent_runtime_assessment_service),
    ],
):
    try:
        items = await service.assess(context, body.agent_ids)
        return AgentRuntimeAssessmentsResponse(
            items=[
                AgentRuntimeAssessmentItemResponse.model_validate(
                    item,
                    from_attributes=True,
                )
                for item in items
            ],
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.get(
    "/default-agent",
    response_model=ProjectDefaultAgentResponse,
)
async def get_project_default_agent(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        ProjectDefaultAgentService,
        Depends(get_project_default_agent_service),
    ],
):
    try:
        selection = await service.get(context)
        return ProjectDefaultAgentResponse(
            agent_asset_id=selection.agent_asset_id,
            revision=selection.revision,
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@router.put(
    "/default-agent",
    response_model=ProjectDefaultAgentResponse,
)
async def replace_project_default_agent(
    body: ProjectDefaultAgentRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        ProjectDefaultAgentService,
        Depends(get_project_default_agent_service),
    ],
):
    try:
        selection = await service.replace(
            context,
            body.agent_asset_id,
            expected_revision=body.expected_revision,
        )
        return ProjectDefaultAgentResponse(
            agent_asset_id=selection.agent_asset_id,
            revision=selection.revision,
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)
