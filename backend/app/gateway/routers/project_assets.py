from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Query,
    Response,
    UploadFile,
    status,
)

from app.gateway.routers.project_asset_routes.common import (
    ASSET_ERRORS as ASSET_ERRORS,
)
from app.gateway.routers.project_asset_routes.common import (
    MAX_SKILL_ARCHIVE_UPLOAD_BYTES as MAX_SKILL_ARCHIVE_UPLOAD_BYTES,
)
from app.gateway.routers.project_asset_routes.common import (
    AssetRoute as AssetRoute,
)
from app.gateway.routers.project_asset_routes.common import (
    _agent_asset_item as _agent_asset_item,
)
from app.gateway.routers.project_asset_routes.common import (
    _agent_definition_call as _agent_definition_call,
)
from app.gateway.routers.project_asset_routes.common import (
    _agent_definition_response as _agent_definition_response,
)
from app.gateway.routers.project_asset_routes.common import (
    _agent_tool_group_catalog as _agent_tool_group_catalog,
)
from app.gateway.routers.project_asset_routes.common import (
    _asset_call as _asset_call,
)
from app.gateway.routers.project_asset_routes.common import (
    _asset_item as _asset_item,
)
from app.gateway.routers.project_asset_routes.common import (
    _asset_item_capabilities as _asset_item_capabilities,
)
from app.gateway.routers.project_asset_routes.common import (
    _binding_item_response as _binding_item_response,
)
from app.gateway.routers.project_asset_routes.common import (
    _current_version_asset_call as _current_version_asset_call,
)
from app.gateway.routers.project_asset_routes.common import (
    _current_version_asset_item as _current_version_asset_item,
)
from app.gateway.routers.project_asset_routes.common import (
    _decode_skill_files as _decode_skill_files,
)
from app.gateway.routers.project_asset_routes.common import (
    _editable_project_mcp_url as _editable_project_mcp_url,
)
from app.gateway.routers.project_asset_routes.common import (
    _factory as _factory,
)
from app.gateway.routers.project_asset_routes.common import (
    _governance_sink as _governance_sink,
)
from app.gateway.routers.project_asset_routes.common import (
    _is_project_asset_actor as _is_project_asset_actor,
)
from app.gateway.routers.project_asset_routes.common import (
    _list_assets as _list_assets,
)
from app.gateway.routers.project_asset_routes.common import (
    _read_skill_archive_upload as _read_skill_archive_upload,
)
from app.gateway.routers.project_asset_routes.common import (
    _redacted_project_mcp_url as _redacted_project_mcp_url,
)
from app.gateway.routers.project_asset_routes.common import (
    _response_data as _response_data,
)
from app.gateway.routers.project_asset_routes.common import (
    _scoped_assets as _scoped_assets,
)
from app.gateway.routers.project_asset_routes.common import (
    _version_call as _version_call,
)
from app.gateway.routers.project_asset_routes.common import (
    _version_history as _version_history,
)
from app.gateway.routers.project_asset_routes.common import (
    asset_session as asset_session,
)
from app.gateway.routers.project_asset_routes.common import (
    authenticated_asset_identity as authenticated_asset_identity,
)
from app.gateway.routers.project_asset_routes.common import (
    get_agent_runtime_assessment_service as get_agent_runtime_assessment_service,
)
from app.gateway.routers.project_asset_routes.common import (
    get_agent_service as get_agent_service,
)
from app.gateway.routers.project_asset_routes.common import (
    get_binding_service as get_binding_service,
)
from app.gateway.routers.project_asset_routes.common import (
    get_mcp_secret_service as get_mcp_secret_service,
)
from app.gateway.routers.project_asset_routes.common import (
    get_mcp_service as get_mcp_service,
)
from app.gateway.routers.project_asset_routes.common import (
    get_project_default_agent_service as get_project_default_agent_service,
)
from app.gateway.routers.project_asset_routes.common import (
    get_skill_secret_service as get_skill_secret_service,
)
from app.gateway.routers.project_asset_routes.common import (
    get_skill_service as get_skill_service,
)
from app.gateway.routers.project_asset_routes.common import (
    project_asset_context as project_asset_context,
)
from app.gateway.routers.project_asset_routes.common import (
    raise_asset_domain as raise_asset_domain,
)
from app.gateway.routers.project_asset_routes.common import (
    system_asset_catalog_actor as system_asset_catalog_actor,
)
from app.gateway.routers.project_asset_routes.contracts import (
    MAX_SKILL_ARCHIVE_BASE64_CHARS as MAX_SKILL_ARCHIVE_BASE64_CHARS,
)
from app.gateway.routers.project_asset_routes.contracts import (
    MAX_SKILL_BASE64_FILE_CHARS as MAX_SKILL_BASE64_FILE_CHARS,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentAssetItemResponse as AgentAssetItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentAssetMutationResponse as AgentAssetMutationResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentBindingItemResponse as AgentBindingItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentBindingResponse as AgentBindingResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentCapabilityBindingsRequest as AgentCapabilityBindingsRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentCreateRequest as AgentCreateRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentDefinitionItemResponse as AgentDefinitionItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentDefinitionResponse as AgentDefinitionResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentInstructionsRequest as AgentInstructionsRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentRuntimeAssessmentItemResponse as AgentRuntimeAssessmentItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentRuntimeAssessmentsRequest as AgentRuntimeAssessmentsRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentRuntimeAssessmentsResponse as AgentRuntimeAssessmentsResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AssetItemResponse as AssetItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AssetMutationResponse as AssetMutationResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    BindingItemResponse as BindingItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    BindingResponse as BindingResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    CreateAssetRequest as CreateAssetRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    CurrentBindingItemResponse as CurrentBindingItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    CurrentBindingResponse as CurrentBindingResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    CurrentSystemBindingRequest as CurrentSystemBindingRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    CurrentVersionAssetItemResponse as CurrentVersionAssetItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    CurrentVersionAssetMutationResponse as CurrentVersionAssetMutationResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    DisableSystemBindingRequest as DisableSystemBindingRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ExpectedAssetVersionRequest as ExpectedAssetVersionRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ExpectedRevisionRequest as ExpectedRevisionRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpConfiguredRequest as McpConfiguredRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpConfiguredResponse as McpConfiguredResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpDefinitionResponse as McpDefinitionResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpDefinitionSlotResponse as McpDefinitionSlotResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpSecretClearRequest as McpSecretClearRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpSecretReplaceRequest as McpSecretReplaceRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpSecretSetResponse as McpSecretSetResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpSecretSlotResponse as McpSecretSlotResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpSecretSlotStatusResponse as McpSecretSlotStatusResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpSlotRequest as McpSlotRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpToolDiscoveryAttemptItemResponse as McpToolDiscoveryAttemptItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpToolDiscoveryAttemptResponse as McpToolDiscoveryAttemptResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpToolInventoryItemResponse as McpToolInventoryItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpToolInventoryResponse as McpToolInventoryResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpToolResponse as McpToolResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpVersionHistoryResponse as McpVersionHistoryResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpVersionItemResponse as McpVersionItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpVersionRequest as McpVersionRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    McpVersionResponse as McpVersionResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    MoveSystemBindingRequest as MoveSystemBindingRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ProjectAgentItemResponse as ProjectAgentItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ProjectAssetItemResponse as ProjectAssetItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ProjectCurrentVersionAssetItemResponse as ProjectCurrentVersionAssetItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ProjectCurrentVersionSkillItemResponse as ProjectCurrentVersionSkillItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ProjectDefaultAgentRequest as ProjectDefaultAgentRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ProjectDefaultAgentResponse as ProjectDefaultAgentResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ProjectSkillItemResponse as ProjectSkillItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ScopedAgentAssetListResponse as ScopedAgentAssetListResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ScopedAssetListResponse as ScopedAssetListResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ScopedCurrentVersionAssetListResponse as ScopedCurrentVersionAssetListResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ScopedCurrentVersionSkillAssetListResponse as ScopedCurrentVersionSkillAssetListResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ScopedSkillAssetListResponse as ScopedSkillAssetListResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillActivationReadinessResponse as SkillActivationReadinessResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillActivationRequest as SkillActivationRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillArchiveImportResponse as SkillArchiveImportResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillAssetRefRequest as SkillAssetRefRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillDeleteResponse as SkillDeleteResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillFileChangeRequest as SkillFileChangeRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillFileContentItemResponse as SkillFileContentItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillFileContentResponse as SkillFileContentResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillFileCreateChangeRequest as SkillFileCreateChangeRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillFileDeleteChangeRequest as SkillFileDeleteChangeRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillFileReplaceChangeRequest as SkillFileReplaceChangeRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillFileRequest as SkillFileRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillFileResponse as SkillFileResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillForkRequest as SkillForkRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillSecretClearRequest as SkillSecretClearRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillSecretExactReplaceRequest as SkillSecretExactReplaceRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillSecretReadinessRequirementResponse as SkillSecretReadinessRequirementResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillSecretReplaceRequest as SkillSecretReplaceRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillSecretRequirementResponse as SkillSecretRequirementResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillSecretSetResponse as SkillSecretSetResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillSecretStatusResponse as SkillSecretStatusResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillVersionHistoryResponse as SkillVersionHistoryResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillVersionItemResponse as SkillVersionItemResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillVersionRequest as SkillVersionRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SkillVersionResponse as SkillVersionResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SyncCurrentSystemMcpBindingRequest as SyncCurrentSystemMcpBindingRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SystemAgentCatalogResponse as SystemAgentCatalogResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SystemAssetCatalogResponse as SystemAssetCatalogResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SystemBindingRequest as SystemBindingRequest,
)
from app.gateway.routers.project_asset_routes.contracts import (
    SystemCurrentVersionCatalogResponse as SystemCurrentVersionCatalogResponse,
)
from app.gateway.routers.project_asset_routes.contracts import (
    _StrictModel as _StrictModel,
)
from app.gateway.routers.project_asset_routes.mcp import _mcp_definition as _mcp_definition
from app.private_work.agent_runtime_assessment import (
    AgentRuntimeAssessmentService,
)
from app.projects.context import ProjectContext
from app.shared_assets import (
    AgentCapabilityBindings,
    AgentInstructions,
    AgentPayload,
    AgentService,
    AssetKind,
    AssetSelection,
    AssetStorageUnavailable,
    BindingService,
    CreateAgent,
    CreateMcpServer,
    McpService,
    ProjectDefaultAgentService,
    SkillAssetRef,
    SkillFileChange,
    SkillService,
)
from app.shared_assets.contexts import SystemAssetReadContext
from app.shared_assets.mcp_secret_service import McpSecretService, McpSecretSetView
from app.shared_assets.skill_secret_service import SkillSecretService

project_router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["project-assets"],
    route_class=AssetRoute,
)
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


def register_asset_routes(
    router: APIRouter,
    actor_dependency,
    *,
    include_shared_asset_mutations: bool = True,
    include_project_asset_delete: bool = False,
    include_skill_export: bool = True,
) -> None:
    async def create_agent(body: AgentCreateRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        try:
            result = await service.create_project(
                actor,
                CreateAgent(body.slug, body.display_name),
                AgentPayload(
                    description=body.description,
                    agents_instructions=body.agents_instructions,
                    soul=body.soul,
                    identity=body.identity,
                    user_context=body.user_context,
                    model_ref=body.model_ref,
                    model_settings=body.model_settings,
                    tool_groups=tuple(body.tool_groups),
                    skill_refs=tuple(SkillAssetRef(item.scope, item.asset_id) for item in body.skill_refs),
                    mcp_version_ids=tuple(body.mcp_version_ids),
                ),
            )
            return _agent_definition_response(result, actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def get_agent(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        return await _agent_definition_call(
            actor,
            lambda: service.get(actor, asset_id),
        )

    async def update_agent_instructions(asset_id: uuid.UUID, body: AgentInstructionsRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        instructions = AgentInstructions(
            agents_instructions=body.agents_instructions,
            soul=body.soul,
            identity=body.identity,
            user_context=body.user_context,
        )
        return await _agent_definition_call(
            actor,
            lambda: service.update_instructions(
                actor,
                asset_id,
                instructions,
                expected_asset_version=body.expected_revision,
            ),
        )

    async def update_agent_capability_bindings(
        asset_id: uuid.UUID,
        body: AgentCapabilityBindingsRequest,
        actor=Depends(actor_dependency),
        service=Depends(get_agent_service),
    ):
        return await _agent_definition_call(
            actor,
            lambda: service.update_capability_bindings(
                actor,
                asset_id,
                AgentCapabilityBindings(
                    tuple(SkillAssetRef(item.scope, item.asset_id) for item in body.skill_refs),
                    tuple(body.mcp_version_ids),
                ),
                expected_asset_version=body.expected_revision,
            ),
        )

    async def delete_agent(asset_id: uuid.UUID, body: ExpectedRevisionRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        try:
            await service.delete(
                actor,
                asset_id,
                expected_asset_version=body.expected_revision,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def get_skill(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _current_version_asset_call(
            actor,
            lambda: service.get(actor, asset_id),
        )

    async def create_skill_version(asset_id: uuid.UUID, body: SkillVersionRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        files = _decode_skill_files(body, actor.request_id)
        return await _version_call(actor, lambda: service.create_version_from_archive(actor, asset_id, files, expected_asset_version=body.expected_revision), SkillVersionResponse)

    async def get_skill_versions(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _version_history(actor, lambda: service.get_version_history(actor, asset_id), SkillVersionHistoryResponse)

    async def activate_skill_version(asset_id: uuid.UUID, version_id: uuid.UUID, body: SkillActivationRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _version_call(
            actor,
            lambda: service.activate_version(
                actor,
                asset_id,
                version_id,
                expected_asset_version=body.expected_revision,
                expected_payload_checksum=body.expected_payload_checksum,
                expected_secret_revision=body.expected_secret_revision,
            ),
            SkillVersionResponse,
        )

    async def delete_skill(asset_id: uuid.UUID, body: ExpectedRevisionRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        try:
            result = await service.delete(
                actor,
                asset_id,
                expected_asset_version=body.expected_revision,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return SkillDeleteResponse(
            skill_id=asset_id,
            affected_agent_count=result.affected_agent_count,
            request_id=actor.request_id,
        )

    async def create_mcp(body: CreateAssetRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.create_asset(actor, CreateMcpServer(body.slug, body.display_name)))

    async def get_mcp(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.get(actor, asset_id))

    async def create_mcp_version(asset_id: uuid.UUID, body: McpVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_call(actor, lambda: service.create_version(actor, asset_id, _mcp_definition(body), expected_asset_version=body.expected_asset_version), McpVersionResponse)

    async def delete_mcp(asset_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        try:
            await service.delete(
                actor,
                asset_id,
                expected_asset_version=body.expected_asset_version,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def get_mcp_versions(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_history(actor, lambda: service.get_version_history(actor, asset_id), McpVersionHistoryResponse)

    async def publish_mcp(asset_id: uuid.UUID, version_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_call(actor, lambda: service.publish(actor, asset_id, version_id, expected_asset_version=body.expected_asset_version), McpVersionResponse)

    async def submit_mcp(asset_id: uuid.UUID, version_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_call(actor, lambda: service.submit_approval(actor, asset_id, version_id, expected_asset_version=body.expected_asset_version), McpVersionResponse)

    def add_status_route(
        segment: str,
        action: Literal["activate", "archive", "enable", "suspend"],
        service_dependency,
    ) -> None:
        async def change_current(asset_id: uuid.UUID, body: ExpectedRevisionRequest, actor=Depends(actor_dependency), service=Depends(service_dependency)):
            async def operation():
                return await getattr(service, action)(
                    actor,
                    asset_id,
                    expected_asset_version=body.expected_revision,
                )

            return await _current_version_asset_call(actor, operation)

        async def change_legacy(asset_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(service_dependency)):
            async def operation():
                return await getattr(service, action)(
                    actor,
                    asset_id,
                    expected_asset_version=body.expected_asset_version,
                )

            return await _asset_call(actor, operation)

        current_contract = segment == "skills"
        agent_contract = segment == "agents"

        async def change_agent(asset_id: uuid.UUID, body: ExpectedRevisionRequest, actor=Depends(actor_dependency), service=Depends(service_dependency)):
            try:
                result = await getattr(service, action)(
                    actor,
                    asset_id,
                    expected_asset_version=body.expected_revision,
                )
                return AgentAssetMutationResponse(
                    item=_agent_asset_item(result),
                    request_id=actor.request_id,
                )
            except ASSET_ERRORS as exc:
                raise_asset_domain(exc)

        router.add_api_route(
            f"/{segment}/{{asset_id}}/{action}",
            change_agent if agent_contract else (change_current if current_contract else change_legacy),
            methods=["POST"],
            response_model=(AgentAssetMutationResponse if agent_contract else (CurrentVersionAssetMutationResponse if current_contract else AssetMutationResponse)),
            name=f"{action}_{segment}",
        )

    async def export_skill_version(
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        actor=Depends(actor_dependency),
        service=Depends(get_skill_service),
    ):
        try:
            package = await service.export_distribution_package(
                actor,
                asset_id,
                version_id,
            )
            if (
                not isinstance(package.filename, str)
                or re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?-v[1-9][0-9]*\.zip",
                    package.filename,
                )
                is None
                or not isinstance(package.content, bytes)
            ):
                raise AssetStorageUnavailable(actor.request_id)
            return Response(
                content=package.content,
                media_type="application/zip",
                headers={
                    "Content-Disposition": (f'attachment; filename="{package.filename}"'),
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    read_routes = (
        ("/agents/{asset_id}", get_agent, ["GET"], AgentDefinitionResponse, 200),
        ("/skills/{asset_id}", get_skill, ["GET"], CurrentVersionAssetMutationResponse, 200),
        ("/skills/{asset_id}/versions", get_skill_versions, ["GET"], SkillVersionHistoryResponse, 200),
        ("/mcp-servers/{asset_id}", get_mcp, ["GET"], AssetMutationResponse, 200),
        ("/mcp-servers/{asset_id}/versions", get_mcp_versions, ["GET"], McpVersionHistoryResponse, 200),
    )
    shared_asset_write_routes = (
        ("/agents", create_agent, ["POST"], AgentDefinitionResponse, 201),
        ("/agents/{asset_id}/instructions", update_agent_instructions, ["PUT"], AgentDefinitionResponse, 200),
        ("/agents/{asset_id}/capability-bindings", update_agent_capability_bindings, ["PUT"], AgentDefinitionResponse, 200),
        ("/skills/{asset_id}/versions", create_skill_version, ["POST"], SkillVersionResponse, 201),
        ("/skills/{asset_id}/versions/{version_id}/activate", activate_skill_version, ["POST"], SkillVersionResponse, 200),
        ("/mcp-servers", create_mcp, ["POST"], AssetMutationResponse, 201),
        ("/mcp-servers/{asset_id}/versions", create_mcp_version, ["POST"], McpVersionResponse, 201),
        ("/mcp-servers/{asset_id}/versions/{version_id}/publish", publish_mcp, ["POST"], McpVersionResponse, 200),
        ("/mcp-servers/{asset_id}/versions/{version_id}/submit-approval", submit_mcp, ["POST"], McpVersionResponse, 200),
    )
    routes = read_routes
    if include_shared_asset_mutations:
        routes = (*routes, *shared_asset_write_routes)
    for path, endpoint, methods, response_model, code in routes:
        router.add_api_route(path, endpoint, methods=methods, response_model=response_model, status_code=code)
    if include_skill_export:
        router.add_api_route(
            "/skills/{asset_id}/versions/{version_id}/export",
            export_skill_version,
            methods=["GET"],
            response_model=None,
            status_code=status.HTTP_200_OK,
            name="export_skill_version",
        )
    if include_project_asset_delete:
        router.add_api_route(
            "/agents/{asset_id}",
            delete_agent,
            methods=["DELETE"],
            response_model=None,
            status_code=status.HTTP_204_NO_CONTENT,
            name="delete_project_agent",
        )
        router.add_api_route(
            "/skills/{asset_id}",
            delete_skill,
            methods=["DELETE"],
            response_model=SkillDeleteResponse,
            status_code=status.HTTP_200_OK,
            name="delete_project_skill",
        )
        router.add_api_route(
            "/mcp-servers/{asset_id}",
            delete_mcp,
            methods=["DELETE"],
            response_model=None,
            status_code=status.HTTP_204_NO_CONTENT,
            name="delete_project_mcp",
        )
    if include_shared_asset_mutations:
        add_status_route("agents", "enable", get_agent_service)
        add_status_route("agents", "suspend", get_agent_service)
        add_status_route("mcp-servers", "archive", get_mcp_service)
        add_status_route("mcp-servers", "activate", get_mcp_service)
        add_status_route("mcp-servers", "suspend", get_mcp_service)
        add_status_route("skills", "enable", get_skill_service)
        add_status_route("skills", "suspend", get_skill_service)


@project_router.get(
    "/skills/{asset_id}/versions/{version_id}/files/content",
    response_model=SkillFileContentResponse,
)
async def preview_project_skill_file(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    try:
        view = await service.preview_version_file(
            context,
            asset_id,
            version_id,
            path,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return SkillFileContentResponse(
            data=SkillFileContentItemResponse(**_response_data(view)),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.post(
    "/skills/{asset_id}/versions/{source_version_id}/fork",
    response_model=SkillVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def fork_project_skill_version(
    asset_id: uuid.UUID,
    source_version_id: uuid.UUID,
    body: SkillForkRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    changes = tuple(
        SkillFileChange(
            op=item.op,
            path=item.path,
            content=getattr(item, "content", None),
            media_type=getattr(item, "media_type", None),
        )
        for item in body.changes
    )
    return await _version_call(
        context,
        lambda: service.fork_version(
            context,
            asset_id,
            source_version_id,
            changes,
            expected_asset_version=body.expected_revision,
            expected_source_payload_checksum=body.expected_source_payload_checksum,
        ),
        SkillVersionResponse,
    )


@project_router.post(
    "/skills/import",
    response_model=SkillArchiveImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_project_skill_archive(
    archive: Annotated[UploadFile, File(description="Skill package archive")],
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    try:
        payload, filename = await _read_skill_archive_upload(
            archive,
            context.request_id,
        )
        result = await service.create_project_from_archive_upload(
            context,
            payload,
            filename=filename,
        )
        return SkillArchiveImportResponse(
            item=_current_version_asset_item(result.asset),
            version=SkillVersionItemResponse.model_validate(
                _response_data(result.version),
            ),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


def _skill_secret_response(
    value,
    request_id: str,
) -> SkillSecretSetResponse:
    return SkillSecretSetResponse(
        **_response_data(value),
        request_id=request_id,
    )


@project_router.get(
    "/skills/{skill_id}/versions/{version_id}/activation-readiness",
    response_model=SkillActivationReadinessResponse,
)
async def get_project_skill_activation_readiness(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        value = await service.get_for_version(context, skill_id, version_id)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return SkillActivationReadinessResponse(
            **_response_data(value),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.get(
    "/skills/{skill_id}/versions/{version_id}/secrets",
    response_model=SkillSecretSetResponse,
)
async def get_project_skill_version_secrets(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        value = await service.get_exact(context, skill_id, version_id)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return _skill_secret_response(
            value,
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.put(
    "/skills/{skill_id}/versions/{version_id}/secrets",
    response_model=SkillSecretSetResponse,
)
async def replace_project_skill_version_secrets(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    body: SkillSecretExactReplaceRequest,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        value = await service.replace_for_version(
            context,
            skill_id,
            version_id,
            body.secrets,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return _skill_secret_response(
            value,
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.get(
    "/skills/{skill_id}/secrets",
    response_model=SkillSecretSetResponse,
)
async def get_project_skill_secrets(
    skill_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        return _skill_secret_response(
            await service.get(context, skill_id),
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.put(
    "/skills/{skill_id}/secrets",
    response_model=SkillSecretSetResponse,
)
async def replace_project_skill_secrets(
    skill_id: uuid.UUID,
    body: SkillSecretReplaceRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        return _skill_secret_response(
            await service.replace(
                context,
                skill_id,
                body.secrets,
                expected_skill_version_id=body.expected_skill_version_id,
            ),
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.post(
    "/skills/{skill_id}/versions/{version_id}/secrets/{secret_name}/clear",
    response_model=SkillSecretSetResponse,
)
async def clear_project_skill_version_secret(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    secret_name: str,
    body: SkillSecretClearRequest,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillSecretService, Depends(get_skill_secret_service)],
):
    try:
        value = await service.clear(
            context,
            skill_id,
            version_id,
            secret_name,
            confirmed=body.confirmed,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return _skill_secret_response(value, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.get(
    "/agents",
    response_model=ScopedAgentAssetListResponse,
)
async def list_project_agents(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[AgentService, Depends(get_agent_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_assets(context, AssetKind.AGENT, service, binding_service)


@project_router.post(
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


@project_router.get(
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


@project_router.put(
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


@project_router.get(
    "/skills",
    response_model=ScopedCurrentVersionSkillAssetListResponse,
)
async def list_project_skills(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_assets(context, AssetKind.SKILL, service, binding_service)


@project_router.get("/mcp-servers", response_model=ScopedAssetListResponse)
async def list_project_mcp_servers(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[McpService, Depends(get_mcp_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_assets(context, AssetKind.MCP, service, binding_service)


@project_router.post(
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


@project_router.put(
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


@project_router.get(
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


@project_router.get(
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


@project_router.put(
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


@project_router.post(
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


def _register_binding_routes(segment: str, kind: AssetKind) -> None:
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
    project_router.add_api_route(
        path,
        enable_exact if kind is AssetKind.MCP else enable_current,
        methods=["POST"],
        response_model=response_model,
        status_code=status.HTTP_201_CREATED,
        name=f"enable_system_{segment}_binding",
    )
    project_router.add_api_route(f"{path}/{{asset_id}}/disable", disable, methods=["POST"], response_model=response_model, name=f"disable_system_{segment}_binding")
    if kind is AssetKind.MCP:
        project_router.add_api_route(f"{path}/{{asset_id}}/upgrade", upgrade, methods=["POST"], response_model=BindingResponse, name=f"upgrade_system_{segment}_binding")
        project_router.add_api_route(f"{path}/{{asset_id}}/rollback", rollback, methods=["POST"], response_model=BindingResponse, name=f"rollback_system_{segment}_binding")
        project_router.add_api_route(
            f"{path}/{{asset_id}}/sync-current",
            sync_current_mcp,
            methods=["POST"],
            response_model=BindingResponse,
            name="sync_current_system_mcp_binding",
        )


for _segment, _kind in _BINDING_KINDS.items():
    _register_binding_routes(_segment, _kind)


register_asset_routes(
    project_router,
    project_asset_context,
    include_project_asset_delete=True,
)


@project_router.get(
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


@project_router.post(
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


@project_router.get(
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
