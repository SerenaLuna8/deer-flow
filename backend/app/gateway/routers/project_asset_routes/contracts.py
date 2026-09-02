from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.private_work.agent_runtime_assessment import MAX_AGENT_RUNTIME_ASSESSMENTS
from app.projects.capabilities import Capability
from app.shared_assets import (
    MAX_AGENT_INSTRUCTION_FIELD_BYTES,
    AgentModelSettings,
    AssetKind,
    AssetScope,
    VersionRelation,
    WorkflowStatus,
)
from app.shared_assets.skill_service import (
    MAX_SKILL_ARCHIVE_BYTES,
    MAX_SKILL_ARCHIVE_FILES,
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


class CurrentVersionAssetItemResponse(_StrictModel):
    """Public Skill aggregate contract.

    MCP intentionally retains ``AssetItemResponse`` and its established
    release workflow; Current Version unification does not change MCP.
    """

    id: uuid.UUID
    scope: AssetScope
    project_id: uuid.UUID | None
    slug: str
    display_name: str
    status: str
    current_version_id: uuid.UUID | None
    revision: int
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime


class AgentAssetItemResponse(_StrictModel):
    id: uuid.UUID
    scope: AssetScope
    project_id: uuid.UUID | None
    slug: str
    display_name: str
    status: str
    definition_id: uuid.UUID
    revision: int
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime


class BindingItemResponse(_StrictModel):
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


class CurrentBindingItemResponse(_StrictModel):
    project_id: uuid.UUID
    kind: AssetKind
    asset_id: uuid.UUID
    current_version_id: uuid.UUID
    enabled: bool
    version: int
    created_by_user_id: str
    updated_by_user_id: str
    created_at: datetime
    updated_at: datetime


class AgentBindingItemResponse(_StrictModel):
    project_id: uuid.UUID
    kind: Literal[AssetKind.AGENT]
    asset_id: uuid.UUID
    definition_id: uuid.UUID
    enabled: bool
    version: int
    created_by_user_id: str
    updated_by_user_id: str
    created_at: datetime
    updated_at: datetime


class ProjectAssetItemResponse(AssetItemResponse):
    capabilities: list[Capability]
    binding: BindingItemResponse | None
    description: str = ""


class ProjectSkillItemResponse(ProjectAssetItemResponse):
    description: str


class ProjectCurrentVersionAssetItemResponse(CurrentVersionAssetItemResponse):
    capabilities: list[Capability]
    binding: CurrentBindingItemResponse | None
    description: str = ""


class ProjectAgentItemResponse(AgentAssetItemResponse):
    capabilities: list[Capability]
    binding: AgentBindingItemResponse | None
    description: str = ""


class ProjectCurrentVersionSkillItemResponse(
    ProjectCurrentVersionAssetItemResponse,
):
    description: str


class ScopedAssetListResponse(_StrictModel):
    system_items: list[ProjectAssetItemResponse]
    project_items: list[ProjectAssetItemResponse]
    request_id: str


class ScopedSkillAssetListResponse(_StrictModel):
    system_items: list[ProjectSkillItemResponse]
    project_items: list[ProjectSkillItemResponse]
    request_id: str


class ScopedCurrentVersionAssetListResponse(_StrictModel):
    system_items: list[ProjectCurrentVersionAssetItemResponse]
    project_items: list[ProjectCurrentVersionAssetItemResponse]
    request_id: str


class ScopedAgentAssetListResponse(_StrictModel):
    system_items: list[ProjectAgentItemResponse]
    project_items: list[ProjectAgentItemResponse]
    request_id: str


class ScopedCurrentVersionSkillAssetListResponse(_StrictModel):
    system_items: list[ProjectCurrentVersionSkillItemResponse]
    project_items: list[ProjectCurrentVersionSkillItemResponse]
    request_id: str


class SystemAssetCatalogResponse(_StrictModel):
    items: list[AssetItemResponse]
    request_id: str


class SystemCurrentVersionCatalogResponse(_StrictModel):
    items: list[CurrentVersionAssetItemResponse]
    request_id: str


class SystemAgentCatalogResponse(_StrictModel):
    items: list[AgentAssetItemResponse]
    request_id: str


class AssetMutationResponse(_StrictModel):
    item: AssetItemResponse
    request_id: str


class CurrentVersionAssetMutationResponse(_StrictModel):
    item: CurrentVersionAssetItemResponse
    request_id: str


class AgentAssetMutationResponse(_StrictModel):
    item: AgentAssetItemResponse
    request_id: str


class SkillDeleteResponse(_StrictModel):
    skill_id: uuid.UUID
    affected_agent_count: int = Field(ge=0)
    request_id: str


class ProjectDefaultAgentResponse(_StrictModel):
    agent_asset_id: uuid.UUID | None
    revision: int = Field(ge=0, le=9_223_372_036_854_775_807)
    request_id: str


class CreateAssetRequest(_StrictModel):
    slug: str
    display_name: str


class SkillAssetRefRequest(_StrictModel):
    scope: AssetScope
    asset_id: uuid.UUID


class AgentCreateRequest(_StrictModel):
    slug: str
    display_name: str
    description: str
    agents_instructions: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    soul: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    identity: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    user_context: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    model_ref: str = Field(
        min_length=7,
        max_length=36,
        pattern=(
            r"^(?:default|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})$"
        ),
    )
    model_settings: AgentModelSettings
    tool_groups: list[str]
    skill_refs: list[SkillAssetRefRequest]
    mcp_version_ids: list[uuid.UUID]


class ExpectedAssetVersionRequest(_StrictModel):
    expected_asset_version: int = Field(ge=1)


class ExpectedRevisionRequest(_StrictModel):
    expected_revision: int = Field(ge=1)


class SkillActivationRequest(ExpectedRevisionRequest):
    expected_payload_checksum: str = Field(
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_secret_revision: int = Field(ge=0)


class ProjectDefaultAgentRequest(_StrictModel):
    agent_asset_id: uuid.UUID | None
    expected_revision: int = Field(
        ge=0,
        le=9_223_372_036_854_775_807,
    )


class SystemBindingRequest(_StrictModel):
    asset_id: uuid.UUID
    version_id: uuid.UUID
    expected_binding_version: int | None = Field(default=None, ge=1)


class CurrentSystemBindingRequest(_StrictModel):
    asset_id: uuid.UUID
    expected_binding_version: int | None = Field(default=None, ge=1)


class MoveSystemBindingRequest(_StrictModel):
    version_id: uuid.UUID
    expected_binding_version: int = Field(ge=1)


class DisableSystemBindingRequest(_StrictModel):
    expected_binding_version: int = Field(ge=1)


class SyncCurrentSystemMcpBindingRequest(_StrictModel):
    expected_binding_version: int | None = Field(
        default=None,
        ge=1,
        strict=True,
    )


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


class CurrentBindingResponse(_StrictModel):
    project_id: uuid.UUID
    kind: AssetKind
    asset_id: uuid.UUID
    current_version_id: uuid.UUID
    enabled: bool
    version: int
    created_by_user_id: str
    updated_by_user_id: str
    created_at: datetime
    updated_at: datetime
    request_id: str


class AgentBindingResponse(_StrictModel):
    project_id: uuid.UUID
    kind: Literal[AssetKind.AGENT]
    asset_id: uuid.UUID
    definition_id: uuid.UUID
    enabled: bool
    version: int
    created_by_user_id: str
    updated_by_user_id: str
    created_at: datetime
    updated_at: datetime
    request_id: str


class AgentInstructionsRequest(_StrictModel):
    agents_instructions: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    soul: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    identity: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    user_context: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    expected_revision: int = Field(ge=1)


class AgentCapabilityBindingsRequest(_StrictModel):
    skill_refs: list[SkillAssetRefRequest]
    mcp_version_ids: list[uuid.UUID]
    expected_revision: int = Field(ge=1)


class AgentRuntimeAssessmentsRequest(_StrictModel):
    agent_ids: tuple[uuid.UUID, ...] = Field(
        min_length=1,
        max_length=MAX_AGENT_RUNTIME_ASSESSMENTS,
    )

    @model_validator(mode="after")
    def validate_unique_agent_ids(self) -> AgentRuntimeAssessmentsRequest:
        if len(set(self.agent_ids)) != len(self.agent_ids):
            raise ValueError("Agent runtime assessment IDs must be unique")
        return self


class AgentRuntimeAssessmentItemResponse(_StrictModel):
    agent_asset_id: uuid.UUID
    selected_definition_id: uuid.UUID | None
    status: Literal["ready", "blocked"]
    reason_code: (
        Literal[
            "agent_unavailable",
            "runtime_dependency_unavailable",
            "model_unavailable",
        ]
        | None
    )

    @model_validator(mode="after")
    def validate_runtime_assessment(self) -> AgentRuntimeAssessmentItemResponse:
        if self.status == "ready":
            if self.selected_definition_id is None or self.reason_code is not None:
                raise ValueError("ready Agent runtime assessment is invalid")
        elif self.reason_code == "agent_unavailable":
            if self.selected_definition_id is not None:
                raise ValueError("unavailable Agent runtime assessment is invalid")
        elif self.reason_code in {
            "runtime_dependency_unavailable",
            "model_unavailable",
        }:
            if self.selected_definition_id is None:
                raise ValueError("blocked Agent runtime assessment is invalid")
        else:
            raise ValueError("blocked Agent runtime assessment reason is invalid")
        return self


class AgentRuntimeAssessmentsResponse(_StrictModel):
    items: list[AgentRuntimeAssessmentItemResponse]
    request_id: str


MAX_SKILL_BASE64_FILE_CHARS = 4 * ((MAX_SKILL_ARCHIVE_BYTES + 2) // 3)
MAX_SKILL_ARCHIVE_BASE64_CHARS = MAX_SKILL_BASE64_FILE_CHARS + 4 * MAX_SKILL_ARCHIVE_FILES


class SkillFileRequest(_StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    content_base64: str = Field(max_length=MAX_SKILL_BASE64_FILE_CHARS)
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=255)


class SkillVersionRequest(_StrictModel):
    files: list[SkillFileRequest] = Field(
        min_length=1,
        max_length=MAX_SKILL_ARCHIVE_FILES,
    )
    expected_revision: int = Field(ge=1)


class SkillFileCreateChangeRequest(_StrictModel):
    op: Literal["create"]
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=1048576)
    media_type: str = Field(min_length=1, max_length=255)


class SkillFileReplaceChangeRequest(_StrictModel):
    op: Literal["replace"]
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=1048576)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)


class SkillFileDeleteChangeRequest(_StrictModel):
    op: Literal["delete"]
    path: str = Field(min_length=1, max_length=1024)


SkillFileChangeRequest = Annotated[
    SkillFileCreateChangeRequest | SkillFileReplaceChangeRequest | SkillFileDeleteChangeRequest,
    Field(discriminator="op"),
]


class SkillForkRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    expected_source_payload_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[SkillFileChangeRequest] = Field(min_length=1, max_length=256)


class McpSlotRequest(_StrictModel):
    name: str
    purpose: str = ""
    payload_schema: dict[str, list[str]]
    required: bool = True


class McpVersionRequest(_StrictModel):
    description: str = ""
    transport: Literal["sse", "http"] = "http"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    oauth: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, Any] = Field(default_factory=dict)
    tool_overrides: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 30
    secret_slots: list[McpSlotRequest] = Field(default_factory=list)
    expected_asset_version: int = Field(ge=1)


class McpConfiguredRequest(_StrictModel):
    slug: str
    display_name: str
    description: str = ""
    transport: Literal["sse", "http"] = "http"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    oauth: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, Any] = Field(default_factory=dict)
    tool_overrides: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 30
    secret_slots: list[McpSlotRequest] = Field(default_factory=list)


class McpSecretReplaceRequest(_StrictModel):
    payload: dict[str, dict[str, str]] = Field(repr=False)


class McpSecretClearRequest(_StrictModel):
    confirmed: Literal[True]


class AgentDefinitionItemResponse(_StrictModel):
    definition_id: uuid.UUID
    agent_id: uuid.UUID
    description: str
    agents_instructions: str
    soul: str
    identity: str
    user_context: str
    model_ref: str
    model_settings: AgentModelSettings
    tool_groups: list[str]
    skill_refs: list[SkillAssetRefRequest]
    mcp_version_ids: list[uuid.UUID]
    payload_schema_version: int
    payload_checksum: str
    updated_by_user_id: str
    updated_at: datetime


class SkillSecretRequirementResponse(_StrictModel):
    name: str
    target_env: str
    optional: bool


class SkillSecretStatusResponse(_StrictModel):
    name: str
    target_env: str
    optional: bool
    configured: bool
    revision: int = Field(ge=0)


class SkillSecretSetResponse(_StrictModel):
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    revision: int = Field(ge=0)
    readiness: Literal["ready", "unready"]
    requirements: list[SkillSecretStatusResponse]
    request_id: str


class SkillSecretReadinessRequirementResponse(_StrictModel):
    name: str
    target_env: str
    optional: bool
    configured: bool


class SkillActivationReadinessResponse(_StrictModel):
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    revision: int = Field(ge=1)
    payload_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    secret_revision: int = Field(ge=0)
    secrets_autonomous: bool
    ready: bool
    required_count: int = Field(ge=0)
    configured_required_count: int = Field(ge=0)
    requirements: list[SkillSecretReadinessRequirementResponse]
    request_id: str


class SkillSecretReplaceRequest(_StrictModel):
    expected_skill_version_id: uuid.UUID
    secrets: dict[str, str] = Field(max_length=256, repr=False)


class SkillSecretExactReplaceRequest(_StrictModel):
    secrets: dict[str, str] = Field(max_length=256, repr=False)


class SkillSecretClearRequest(_StrictModel):
    confirmed: Literal[True]


class SkillFileResponse(_StrictModel):
    path: str
    media_type: str
    size_bytes: int
    sha256: str


class SkillFileContentItemResponse(_StrictModel):
    path: str
    media_type: str
    size_bytes: int
    sha256: str
    preview_status: Literal["ready", "binary", "too_large"]
    encoding: Literal["utf-8"] | None
    content: str | None
    source_payload_checksum: str
    asset_revision: int


class SkillVersionItemResponse(_StrictModel):
    id: uuid.UUID
    skill_id: uuid.UUID
    version_number: int
    relation: VersionRelation
    description: str
    frontmatter: dict[str, Any]
    compatibility: str | None
    secret_requirements: list[SkillSecretRequirementResponse]
    file_views: list[SkillFileResponse]
    supersedes_version_id: uuid.UUID | None
    payload_checksum: str
    revoked_at: datetime | None
    revoked_by_user_id: str | None
    revocation_reason_code: Literal["security", "policy", "integrity"] | None
    governance_status: Literal["active", "revoked"]
    binding_eligible: bool
    created_by_user_id: str
    created_at: datetime


class McpDefinitionSlotResponse(_StrictModel):
    name: str
    purpose: str
    payload_schema: dict[str, list[str]]
    required: bool


class McpDefinitionResponse(_StrictModel):
    description: str
    transport: Literal["stdio", "sse", "http", "streamable_http"]
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]
    oauth: dict[str, Any]
    routing: dict[str, Any]
    tool_overrides: dict[str, Any]
    timeout_seconds: int
    secret_slots: list[McpDefinitionSlotResponse]


class McpSecretSlotResponse(_StrictModel):
    id: uuid.UUID
    name: str
    purpose: str
    payload_schema: dict[str, list[str]]
    required: bool


class McpSecretSlotStatusResponse(McpSecretSlotResponse):
    configured: bool
    revision: int = Field(ge=0)


class McpSecretSetResponse(_StrictModel):
    mcp_server_id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    revision: int = Field(ge=0)
    readiness: Literal["ready", "unready"]
    slots: list[McpSecretSlotStatusResponse]
    request_id: str


class McpVersionItemResponse(_StrictModel):
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    version_number: int
    workflow_status: WorkflowStatus
    definition: McpDefinitionResponse
    secret_slots: list[McpSecretSlotResponse]
    supersedes_version_id: uuid.UUID | None
    payload_checksum: str
    submitted_at: datetime | None
    reviewed_at: datetime | None
    reviewed_by_user_id: str | None
    created_by_user_id: str
    created_at: datetime


class AgentDefinitionResponse(_StrictModel):
    item: AgentAssetItemResponse
    definition: AgentDefinitionItemResponse
    request_id: str


class SkillVersionResponse(_StrictModel):
    data: SkillVersionItemResponse
    request_id: str


class SkillArchiveImportResponse(_StrictModel):
    item: CurrentVersionAssetItemResponse
    version: SkillVersionItemResponse
    request_id: str


class SkillFileContentResponse(_StrictModel):
    data: SkillFileContentItemResponse
    request_id: str


class McpVersionResponse(_StrictModel):
    data: McpVersionItemResponse
    request_id: str


class McpConfiguredResponse(_StrictModel):
    item: AssetItemResponse
    version: McpVersionItemResponse
    request_id: str


class SkillVersionHistoryResponse(_StrictModel):
    data: list[SkillVersionItemResponse]
    request_id: str


class McpVersionHistoryResponse(_StrictModel):
    data: list[McpVersionItemResponse]
    request_id: str


class McpToolResponse(_StrictModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field(max_length=4096)


class McpToolInventoryItemResponse(_StrictModel):
    status: Literal[
        "never_discovered",
        "testing",
        "ready",
        "degraded",
        "failed",
        "stale",
    ]
    tools: list[McpToolResponse] = Field(max_length=128)
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    error_code: (
        Literal[
            "mcp_discovery_unavailable",
            "mcp_catalog_invalid",
        ]
        | None
    )


class McpToolInventoryResponse(_StrictModel):
    data: McpToolInventoryItemResponse
    request_id: str


class McpToolDiscoveryAttemptItemResponse(_StrictModel):
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: (
        Literal[
            "mcp_discovery_unavailable",
            "mcp_catalog_invalid",
        ]
        | None
    )


class McpToolDiscoveryAttemptResponse(_StrictModel):
    data: McpToolDiscoveryAttemptItemResponse
    request_id: str
