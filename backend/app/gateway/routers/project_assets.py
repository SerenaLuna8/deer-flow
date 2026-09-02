"""Compatibility façade for Project Assets Gateway routes."""

from __future__ import annotations

from app.gateway.routers.project_asset_routes.bindings import (
    _binding_response as _binding_response,
)
from app.gateway.routers.project_asset_routes.catalog import (
    catalog_router as catalog_router,
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
from app.gateway.routers.project_asset_routes.mcp import (
    _mcp_definition as _mcp_definition,
)
from app.gateway.routers.project_asset_routes.mcp import (
    _mcp_secret_response as _mcp_secret_response,
)
from app.gateway.routers.project_asset_routes.router import (
    project_router as project_router,
)
from app.gateway.routers.project_asset_routes.router import (
    register_asset_routes as register_asset_routes,
)
