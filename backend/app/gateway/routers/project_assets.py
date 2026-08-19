from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable, Mapping
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Annotated, Any, Literal, NoReturn
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import get_config, get_current_user_from_request
from app.private_work.agent_runtime_assessment import (
    MAX_AGENT_RUNTIME_ASSESSMENTS,
    AgentRuntimeAssessmentService,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectForbidden,
    ProjectNotFound,
)
from app.shared_assets import (
    MAX_AGENT_INSTRUCTION_FIELD_BYTES,
    AgentCapabilityBindings,
    AgentDesignConflictUnresolved,
    AgentDesignSecretDetected,
    AgentDesignSessionLimitExceeded,
    AgentDesignSlugConflict,
    AgentInstructions,
    AgentModelSettings,
    AgentPayload,
    AgentService,
    AssetConflict,
    AssetForbidden,
    AssetInUse,
    AssetKind,
    AssetNotFound,
    AssetRunQuotaExceeded,
    AssetScope,
    AssetSelection,
    AssetStorageQuotaExceeded,
    AssetStorageUnavailable,
    AssetValidationFailed,
    BindingService,
    CreateAgent,
    CreateCredential,
    CreateMcpServer,
    CredentialService,
    McpCredentialSlot,
    McpDefinition,
    McpService,
    ProjectDefaultAgentService,
    SharedAssetError,
    SkillArchiveFile,
    SkillArchiveLimitExceeded,
    SkillCredentialBindingInput,
    SkillCredentialBindingInvalid,
    SkillCredentialBindingService,
    SkillCredentialBindingsIncomplete,
    SkillCredentialSelectionStale,
    SkillDesignBaseStale,
    SkillDesignNoChanges,
    SkillDesignTargetDeleted,
    SkillDesignTargetSessionExists,
    SkillDesignTargetUnsupported,
    SkillFileChange,
    SkillPublishBaseStale,
    SkillRuntimeNameConflict,
    SkillService,
    WorkflowStatus,
)
from app.shared_assets.agent_catalog import (
    AgentCatalogValidator,
    StaticToolGroupCatalog,
)
from app.shared_assets.contexts import SystemAssetReadContext, resolve_asset_reader
from app.shared_assets.skill_archive import MAX_SKILL_ARCHIVE_UPLOAD_BYTES
from app.shared_assets.skill_service import (
    MAX_SKILL_ARCHIVE_BYTES,
    MAX_SKILL_ARCHIVE_FILES,
)
from app.system_settings.credential_migration import (
    SystemModelCredentialMigrationAdapter,
)
from deerflow.config.app_config import AppConfig
from deerflow.mcp_definition_policy import NetworkMcpEndpointPolicy
from deerflow.mcp_endpoint_policy import validate_remote_mcp_endpoint_syntax
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
catalog_router = APIRouter(
    prefix="/api/assets/catalog",
    tags=["asset-catalog"],
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


class ProjectAssetItemResponse(AssetItemResponse):
    capabilities: list[Capability]
    binding: BindingItemResponse | None
    description: str = ""


class ProjectSkillItemResponse(ProjectAssetItemResponse):
    description: str


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


class ProjectCredentialItemResponse(CredentialItemResponse):
    capabilities: list[Capability]


class ScopedAssetListResponse(_StrictModel):
    system_items: list[ProjectAssetItemResponse]
    project_items: list[ProjectAssetItemResponse]
    request_id: str


class ScopedSkillAssetListResponse(_StrictModel):
    system_items: list[ProjectSkillItemResponse]
    project_items: list[ProjectSkillItemResponse]
    request_id: str


class ScopedCredentialListResponse(_StrictModel):
    system_items: list[ProjectCredentialItemResponse]
    project_items: list[ProjectCredentialItemResponse]
    request_id: str


class SystemAssetCatalogResponse(_StrictModel):
    items: list[AssetItemResponse]
    request_id: str


class AssetMutationResponse(_StrictModel):
    item: AssetItemResponse
    request_id: str


class CredentialMutationResponse(_StrictModel):
    item: CredentialItemResponse
    request_id: str


class ProjectDefaultAgentResponse(_StrictModel):
    agent_asset_id: uuid.UUID | None
    revision: int = Field(ge=0, le=9_223_372_036_854_775_807)
    request_id: str


class CreateAssetRequest(_StrictModel):
    slug: str
    display_name: str


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
    skill_version_ids: list[uuid.UUID]
    mcp_version_ids: list[uuid.UUID]


class ExpectedAssetVersionRequest(_StrictModel):
    expected_asset_version: int = Field(ge=1)


class SkillPublishCredentialBindingRequest(_StrictModel):
    name: str = Field(max_length=255, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    credential_version_id: uuid.UUID


class SkillPublishRequest(ExpectedAssetVersionRequest):
    acknowledge_stale_base: bool = False
    expected_payload_checksum: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_binding_revision: int | None = Field(default=None, ge=0)
    credential_bindings: list[SkillPublishCredentialBindingRequest] | None = Field(
        default=None,
        max_length=256,
    )

    @model_validator(mode="after")
    def validate_credential_binding_cas(self) -> SkillPublishRequest:
        if "credential_bindings" not in self.model_fields_set:
            return self
        if self.credential_bindings is None or self.expected_payload_checksum is None or self.expected_binding_revision is None or len({item.name for item in self.credential_bindings}) != len(self.credential_bindings):
            raise ValueError("Skill publish credential binding CAS is invalid")
        return self


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


class AgentInstructionsRequest(_StrictModel):
    agents_instructions: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    soul: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    identity: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    user_context: str = Field(max_length=MAX_AGENT_INSTRUCTION_FIELD_BYTES)
    expected_asset_version: int = Field(ge=1)


class AgentCapabilityBindingsRequest(_StrictModel):
    skill_version_ids: list[uuid.UUID]
    mcp_version_ids: list[uuid.UUID]
    expected_asset_version: int = Field(ge=1)


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
    selected_version_id: uuid.UUID | None
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
            if self.selected_version_id is None or self.reason_code is not None:
                raise ValueError("ready Agent runtime assessment is invalid")
        elif self.reason_code == "agent_unavailable":
            if self.selected_version_id is not None:
                raise ValueError("unavailable Agent runtime assessment is invalid")
        elif self.reason_code in {
            "runtime_dependency_unavailable",
            "model_unavailable",
        }:
            if self.selected_version_id is None:
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
    expected_asset_version: int = Field(ge=1)


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
    expected_asset_version: int = Field(ge=1)
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
    credential_slots: list[McpSlotRequest] = Field(default_factory=list)
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
    credential_slots: list[McpSlotRequest] = Field(default_factory=list)


class McpApproveRequest(_StrictModel):
    credential_versions: dict[str, uuid.UUID]
    expected_asset_version: int = Field(ge=1)


class SystemMcpCredentialGrantRequest(_StrictModel):
    credential_versions: dict[str, uuid.UUID]
    expected_active_grant_versions: dict[str, int] = Field(default_factory=dict)


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


class CredentialDeleteRequest(_StrictModel):
    expected_credential_version: int = Field(ge=1)


class CredentialGrantMigrationRequest(_StrictModel):
    expected_credential_version: int = Field(ge=1)


class AgentVersionItemResponse(_StrictModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    workflow_status: WorkflowStatus
    description: str
    agents_instructions: str
    soul: str
    identity: str
    user_context: str
    model_ref: str
    model_settings: AgentModelSettings
    tool_groups: list[str]
    skill_version_ids: list[uuid.UUID]
    mcp_version_ids: list[uuid.UUID]
    supersedes_version_id: uuid.UUID | None
    payload_schema_version: int
    payload_checksum: str
    created_by_user_id: str
    created_at: datetime


class SkillSecretRequirementResponse(_StrictModel):
    name: str
    optional: bool


class EligibleSkillCredentialResponse(_StrictModel):
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    display_name: str
    version_number: int


class SkillCredentialRequirementResponse(_StrictModel):
    name: str
    optional: bool
    configured: bool
    credential_id: uuid.UUID | None
    credential_version_id: uuid.UUID | None
    credential_display_name: str | None
    credential_version_number: int | None
    eligible_credentials: list[EligibleSkillCredentialResponse]


class SkillCredentialBindingSetResponse(_StrictModel):
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    revision: int = Field(ge=0)
    requirements: list[SkillCredentialRequirementResponse]
    request_id: str


class SkillCredentialPublishRequirementResponse(_StrictModel):
    name: str
    optional: bool
    suggested_credential_version_id: uuid.UUID | None
    eligible_credentials: list[EligibleSkillCredentialResponse]


class SkillPublishPlanResponse(_StrictModel):
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    asset_version: int = Field(ge=1)
    payload_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_revision: int = Field(ge=0)
    secrets_autonomous: bool
    requirements: list[SkillCredentialPublishRequirementResponse]
    request_id: str


class SkillCredentialBindingRequest(_StrictModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=255)
    credential_version_id: uuid.UUID


class SkillCredentialBindingReplaceRequest(_StrictModel):
    expected_revision: int = Field(ge=0)
    bindings: list[SkillCredentialBindingRequest] = Field(
        max_length=256,
    )


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
    asset_version: int


class SkillVersionItemResponse(_StrictModel):
    id: uuid.UUID
    skill_id: uuid.UUID
    version_number: int
    workflow_status: WorkflowStatus
    description: str
    frontmatter: dict[str, Any]
    compatibility: str | None
    secret_requirements: list[SkillSecretRequirementResponse]
    scan_decision: Literal["allow", "warn", "block"]
    scan_rule_ids: list[str]
    scan_summary: dict[str, Any]
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
    credential_slots: list[McpDefinitionSlotResponse]


class McpCredentialSlotResponse(_StrictModel):
    id: uuid.UUID
    name: str
    purpose: str
    payload_schema: dict[str, list[str]]
    required: bool


class CredentialGrantResponse(_StrictModel):
    id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    credential_slot_id: uuid.UUID
    credential_version_id: uuid.UUID
    status: Literal["active", "revoked"]
    version: int
    created_by_user_id: str
    created_at: datetime


class McpVersionItemResponse(_StrictModel):
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    version_number: int
    workflow_status: WorkflowStatus
    definition: McpDefinitionResponse
    credential_slots: list[McpCredentialSlotResponse]
    credential_grants: list[CredentialGrantResponse]
    supersedes_version_id: uuid.UUID | None
    payload_checksum: str
    submitted_at: datetime | None
    reviewed_at: datetime | None
    reviewed_by_user_id: str | None
    created_by_user_id: str
    created_at: datetime


class CredentialVersionItemResponse(_StrictModel):
    id: uuid.UUID
    credential_id: uuid.UUID
    version_number: int
    status: Literal["active", "retired", "revoked"]
    payload_schema_version: int
    payload_schema: dict[str, list[str]]
    supersedes_version_id: uuid.UUID | None
    created_by_user_id: str
    created_at: datetime


class CredentialGrantMigrationResponse(_StrictModel):
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    migrated_count: int = Field(ge=0)
    migrated_model_count: int = Field(ge=0)
    request_id: str


class AgentVersionResponse(_StrictModel):
    data: AgentVersionItemResponse
    request_id: str


class AgentCreateResponse(_StrictModel):
    item: AssetItemResponse
    version: AgentVersionItemResponse
    request_id: str


class SkillVersionResponse(_StrictModel):
    data: SkillVersionItemResponse
    request_id: str


class SkillArchiveImportResponse(_StrictModel):
    item: AssetItemResponse
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


class CredentialPendingMigrationResponse(_StrictModel):
    total: int = Field(ge=0)
    system_model_count: int = Field(ge=0)


class CredentialVersionResponse(_StrictModel):
    data: CredentialVersionItemResponse
    # Null when the pending count is not derivable, never a silent zero.
    pending_migration: CredentialPendingMigrationResponse | None
    request_id: str


class AgentVersionHistoryResponse(_StrictModel):
    data: list[AgentVersionItemResponse]
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


class CredentialVersionHistoryResponse(_StrictModel):
    data: list[CredentialVersionItemResponse]
    request_id: str


ASSET_ERRORS = (
    AssetNotFound,
    AssetForbidden,
    AssetInUse,
    AssetConflict,
    AssetValidationFailed,
    AssetStorageUnavailable,
    AssetStorageQuotaExceeded,
    AssetRunQuotaExceeded,
    SkillDesignTargetUnsupported,
    SkillDesignTargetSessionExists,
    SkillDesignTargetDeleted,
    SkillDesignBaseStale,
    SkillDesignNoChanges,
    SkillCredentialBindingInvalid,
    SkillCredentialBindingsIncomplete,
    SkillCredentialSelectionStale,
    SkillPublishBaseStale,
    SkillRuntimeNameConflict,
    AgentDesignSecretDetected,
    AgentDesignSessionLimitExceeded,
    AgentDesignSlugConflict,
    AgentDesignConflictUnresolved,
    SkillArchiveLimitExceeded,
)


def raise_asset_domain(exc: SharedAssetError, request_id: str | None = None) -> NoReturn:
    known = {
        AssetNotFound: 404,
        AssetForbidden: 403,
        AssetInUse: 409,
        AssetConflict: 409,
        AssetValidationFailed: 422,
        AssetStorageQuotaExceeded: 429,
        AssetRunQuotaExceeded: 429,
        AssetStorageUnavailable: 503,
        SkillDesignTargetUnsupported: 422,
        SkillDesignTargetSessionExists: 409,
        SkillDesignTargetDeleted: 409,
        SkillDesignBaseStale: 409,
        SkillDesignNoChanges: 409,
        SkillCredentialBindingInvalid: 422,
        SkillCredentialBindingsIncomplete: 422,
        SkillCredentialSelectionStale: 409,
        SkillPublishBaseStale: 409,
        SkillRuntimeNameConflict: 409,
        AgentDesignSecretDetected: 422,
        AgentDesignSessionLimitExceeded: 429,
        AgentDesignSlugConflict: 409,
        AgentDesignConflictUnresolved: 409,
        SkillArchiveLimitExceeded: 413,
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
        headers={"Retry-After": "1"} if type(exc) in {AssetStorageQuotaExceeded, AssetRunQuotaExceeded} else None,
    ) from None


async def authenticated_asset_identity(
    user=Depends(get_current_user_from_request),
) -> tuple[uuid.UUID, str]:
    return uuid.UUID(str(user.id)), get_current_trace_id() or generate_trace_id()


async def system_asset_catalog_actor(
    user=Depends(get_current_user_from_request),
) -> SystemAssetReadContext:
    request_id = get_current_trace_id() or generate_trace_id()
    try:
        return resolve_asset_reader(user, request_id=request_id)
    except AssetForbidden as exc:
        raise_asset_domain(exc)


async def asset_session():
    from deerflow.persistence.engine import get_session_factory as resolve_session_factory

    request_id = get_current_trace_id() or generate_trace_id()
    try:
        factory = resolve_session_factory()
    except RuntimeError:
        raise_asset_domain(AssetStorageUnavailable(request_id))
    async with factory() as session:
        yield session


async def project_asset_context(
    project_id: uuid.UUID,
    identity: Annotated[tuple[uuid.UUID, str], Depends(authenticated_asset_identity)],
    session: Annotated[AsyncSession, Depends(asset_session)],
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


def _governance_sink(request: Request):
    value = getattr(request.app.state, "shared_asset_audit_sink", None)
    if value is None:
        request_id = get_current_trace_id() or generate_trace_id()
        raise_asset_domain(AssetStorageUnavailable(request_id))
    return value


def _agent_tool_group_catalog(config: AppConfig) -> StaticToolGroupCatalog:
    return StaticToolGroupCatalog(
        (
            *(group.name for group in config.tool_groups),
            *(tool.group for tool in config.tools),
            "task",
        )
    )


def get_agent_service(
    request: Request,
    config: AppConfig = Depends(get_config),
) -> AgentService:
    return AgentService(
        _factory(),
        governance_sink=_governance_sink(request),
        catalog_validator=AgentCatalogValidator(
            _agent_tool_group_catalog(config),
        ),
    )


def get_agent_runtime_assessment_service(
    request: Request,
) -> AgentRuntimeAssessmentService:
    endpoint_policy = getattr(request.app.state, "mcp_endpoint_policy", None)
    if not isinstance(endpoint_policy, NetworkMcpEndpointPolicy):
        request_id = get_current_trace_id() or generate_trace_id()
        raise_asset_domain(AssetStorageUnavailable(request_id))
    return AgentRuntimeAssessmentService(
        _factory(),
        endpoint_policy=endpoint_policy,
    )


def get_skill_service(request: Request) -> SkillService:
    quota = getattr(request.app.state, "project_quota_enforcer", None)
    return SkillService(
        _factory(),
        governance_sink=_governance_sink(request),
        quota=quota,
    )


def get_mcp_service(request: Request) -> McpService:
    endpoint_policy = getattr(request.app.state, "mcp_endpoint_policy", None)
    if not isinstance(endpoint_policy, NetworkMcpEndpointPolicy):
        request_id = get_current_trace_id() or generate_trace_id()
        raise_asset_domain(AssetStorageUnavailable(request_id))
    return McpService(
        _factory(),
        governance_sink=_governance_sink(request),
        endpoint_policy=endpoint_policy,
    )


def get_credential_service(request: Request) -> CredentialService:
    return CredentialService(
        _factory(),
        governance_sink=_governance_sink(request),
        system_models=SystemModelCredentialMigrationAdapter(),
    )


def get_binding_service(request: Request) -> BindingService:
    return BindingService(_factory(), governance_sink=_governance_sink(request))


def get_project_default_agent_service(
    request: Request,
) -> ProjectDefaultAgentService:
    return ProjectDefaultAgentService(
        _factory(),
        governance_sink=_governance_sink(request),
    )


def get_skill_credential_binding_service(
    request: Request,
) -> SkillCredentialBindingService:
    return SkillCredentialBindingService(
        _factory(),
        governance_sink=_governance_sink(request),
    )


def _asset_item(view) -> AssetItemResponse:
    return AssetItemResponse.model_validate(view, from_attributes=True)


def _credential_item(view) -> CredentialItemResponse:
    return CredentialItemResponse.model_validate(view, from_attributes=True)


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


@catalog_router.get("/agents", response_model=SystemAssetCatalogResponse)
async def list_system_catalog_agents(
    actor: Annotated[SystemAssetReadContext, Depends(system_asset_catalog_actor)],
    service: Annotated[AgentService, Depends(get_agent_service)],
):
    return await _list_system_catalog(actor, service)


@catalog_router.get("/skills", response_model=SystemAssetCatalogResponse)
async def list_system_catalog_skills(
    actor: Annotated[SystemAssetReadContext, Depends(system_asset_catalog_actor)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    return await _list_system_catalog(actor, service)


@catalog_router.get("/mcp-servers", response_model=SystemAssetCatalogResponse)
async def list_system_catalog_mcp_servers(
    actor: Annotated[SystemAssetReadContext, Depends(system_asset_catalog_actor)],
    service: Annotated[McpService, Depends(get_mcp_service)],
):
    return await _list_system_catalog(actor, service)


def _asset_item_capabilities(
    context: ProjectContext,
    scope: AssetScope,
    kind: AssetKind,
) -> list[Capability]:
    allowed = {
        Capability.SHARED_ASSETS_READ,
        Capability.SHARED_ASSETS_EXECUTE,
        Capability.SHARED_ASSETS_MANAGE_BINDINGS,
    }
    if scope is AssetScope.PROJECT:
        allowed.add(Capability.SHARED_ASSETS_EDIT)
        if kind is AssetKind.MCP:
            allowed.add(Capability.MCP_CREDENTIALS_APPROVE)
    return sorted(context.capabilities & allowed, key=str)


def _credential_item_capabilities(
    context: ProjectContext,
    scope: AssetScope,
) -> list[Capability]:
    allowed = {Capability.SHARED_ASSETS_READ}
    if scope is AssetScope.PROJECT:
        allowed.add(Capability.MCP_CREDENTIALS_APPROVE)
    return sorted(context.capabilities & allowed, key=str)


def _scoped_assets(
    views,
    bindings,
    context: ProjectContext,
    kind: AssetKind,
) -> ScopedAssetListResponse | ScopedSkillAssetListResponse:
    by_asset_id = {binding.asset_id: binding for binding in bindings}
    item_model = ProjectSkillItemResponse if kind is AssetKind.SKILL else ProjectAssetItemResponse
    items = [
        item_model(
            **vars(view),
            capabilities=_asset_item_capabilities(context, view.scope, kind),
            binding=(BindingItemResponse(**vars(by_asset_id[view.id])) if view.scope is AssetScope.SYSTEM and view.id in by_asset_id else None),
        )
        for view in views
    ]
    response_model = ScopedSkillAssetListResponse if kind is AssetKind.SKILL else ScopedAssetListResponse
    return response_model(
        system_items=[item for item in items if item.scope is AssetScope.SYSTEM],
        project_items=[item for item in items if item.scope is AssetScope.PROJECT],
        request_id=context.request_id,
    )


async def _list_assets(
    context: ProjectContext,
    kind: AssetKind,
    service,
    binding_service: BindingService,
) -> ScopedAssetListResponse | ScopedSkillAssetListResponse:
    try:
        views = await service.list_visible(context)
        bindings = await binding_service.list_visible(context, kind)
        return _scoped_assets(views, bindings, context, kind)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _asset_call(actor, operation):
    try:
        result = await operation()
        return AssetMutationResponse(item=_asset_item(result), request_id=actor.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _version_call(actor, operation, response_model: type[_StrictModel]):
    try:
        result = await operation()
        return response_model(
            data=_response_data(
                result,
                redact_project_mcp=_is_project_asset_actor(actor),
            ),
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


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


async def _version_history(actor, operation, response_model: type[_StrictModel]):
    try:
        versions = await operation()
        return response_model(
            data=[
                _response_data(
                    version,
                    redact_project_mcp=_is_project_asset_actor(actor),
                )
                for version in versions
            ],
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


def _is_project_asset_actor(actor: object) -> bool:
    return (
        isinstance(actor, ProjectContext)
        or getattr(
            actor,
            "project_id",
            None,
        )
        is not None
    )


def _redacted_project_mcp_url(value: object) -> str | None:
    """Expose only a non-secret HTTP(S) origin from historical Project rows."""

    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not hostname or "*" in hostname or parsed.username is not None or parsed.password is not None or "#" in value or parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
        return None
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _editable_project_mcp_url(value: object) -> str | None:
    """Expose a path only for a structurally safe IP-literal endpoint.

    The service revalidates the selected current definition against the
    process-frozen CIDR policy before this response projection is reached.
    Unsafe or non-current-compatible values fall back to the historical
    origin-only representation.
    """

    origin = _redacted_project_mcp_url(value)
    if origin is None or not isinstance(value, str):
        return origin
    try:
        endpoint = validate_remote_mcp_endpoint_syntax(value)
        hostname = urlsplit(endpoint).hostname
        if hostname is None:
            return origin
        ip_address(hostname)
    except ValueError:
        return origin
    return endpoint


def _response_data(
    value: object,
    *,
    redact_project_mcp: bool = False,
    editable_project_mcp: bool = False,
) -> object:
    """Copy immutable domain views into ordinary response-safe containers."""
    if is_dataclass(value) and not isinstance(value, type):
        response = {
            field.name: _response_data(
                getattr(value, field.name),
                redact_project_mcp=redact_project_mcp,
                editable_project_mcp=editable_project_mcp,
            )
            for field in dataclass_fields(value)
        }
        if isinstance(value, McpDefinition):
            # Historical versions may contain values that were labelled
            # "non-secret" at authoring time. Arbitrary values cannot be
            # classified reliably, so public API responses expose only the
            # Credential-slot schema and never replay persisted env/header
            # values.
            response["env"] = {}
            response["headers"] = {}
            if redact_project_mcp:
                response["command"] = None
                response["args"] = []
                project_url = getattr(value, "url", None)
                response["url"] = _editable_project_mcp_url(project_url) if editable_project_mcp else _redacted_project_mcp_url(project_url)
                response["oauth"] = {}
                response["routing"] = {}
                response["tool_overrides"] = {}
        return response
    if isinstance(value, Mapping):
        return {
            str(key): _response_data(
                item,
                redact_project_mcp=redact_project_mcp,
                editable_project_mcp=editable_project_mcp,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _response_data(
                item,
                redact_project_mcp=redact_project_mcp,
                editable_project_mcp=editable_project_mcp,
            )
            for item in value
        ]
    return value


def _decode_skill_files(body: SkillVersionRequest, request_id: str) -> tuple[SkillArchiveFile, ...]:
    try:
        if sum(len(item.content_base64) for item in body.files) > MAX_SKILL_ARCHIVE_BASE64_CHARS:
            raise AssetValidationFailed(request_id)
        files: list[SkillArchiveFile] = []
        total_decoded_bytes = 0
        for item in body.files:
            content = base64.b64decode(item.content_base64, validate=True)
            total_decoded_bytes += len(content)
            if total_decoded_bytes > MAX_SKILL_ARCHIVE_BYTES:
                raise AssetValidationFailed(request_id)
            files.append(
                SkillArchiveFile(
                    path=item.path,
                    content=content,
                    media_type=item.media_type,
                )
            )
        return tuple(files)
    except AssetValidationFailed as exc:
        raise_asset_domain(exc)
    except (binascii.Error, ValueError):
        raise_asset_domain(AssetValidationFailed(request_id))


async def _read_skill_archive_upload(
    archive: UploadFile,
    request_id: str,
) -> tuple[bytes, str]:
    filename = archive.filename
    if not isinstance(filename, str) or not filename.strip():
        raise AssetValidationFailed(request_id)
    payload = bytearray()
    try:
        while True:
            remaining = MAX_SKILL_ARCHIVE_UPLOAD_BYTES - len(payload)
            chunk = await archive.read(min(1024 * 1024, remaining + 1))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_SKILL_ARCHIVE_UPLOAD_BYTES:
                raise SkillArchiveLimitExceeded(request_id)
    finally:
        await archive.close()
    if not payload:
        raise AssetValidationFailed(request_id)
    return bytes(payload), filename


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


def register_asset_routes(
    router: APIRouter,
    actor_dependency,
    *,
    include_shared_asset_mutations: bool = True,
    include_project_asset_delete: bool = False,
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
                    skill_version_ids=tuple(body.skill_version_ids),
                    mcp_version_ids=tuple(body.mcp_version_ids),
                ),
            )
            return AgentCreateResponse(
                item=_asset_item(result.asset),
                version=AgentVersionItemResponse.model_validate(_response_data(result.version)),
                request_id=actor.request_id,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def get_agent(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        return await _asset_call(actor, lambda: service.get(actor, asset_id))

    async def update_agent_instructions(asset_id: uuid.UUID, body: AgentInstructionsRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        instructions = AgentInstructions(
            agents_instructions=body.agents_instructions,
            soul=body.soul,
            identity=body.identity,
            user_context=body.user_context,
        )
        return await _version_call(
            actor,
            lambda: service.update_instructions(
                actor,
                asset_id,
                instructions,
                expected_asset_version=body.expected_asset_version,
            ),
            AgentVersionResponse,
        )

    async def update_agent_capability_bindings(
        asset_id: uuid.UUID,
        body: AgentCapabilityBindingsRequest,
        actor=Depends(actor_dependency),
        service=Depends(get_agent_service),
    ):
        return await _version_call(
            actor,
            lambda: service.update_capability_bindings(
                actor,
                asset_id,
                AgentCapabilityBindings(
                    tuple(body.skill_version_ids),
                    tuple(body.mcp_version_ids),
                ),
                expected_asset_version=body.expected_asset_version,
            ),
            AgentVersionResponse,
        )

    async def restore_agent_version(
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        body: ExpectedAssetVersionRequest,
        actor=Depends(actor_dependency),
        service=Depends(get_agent_service),
    ):
        return await _version_call(
            actor,
            lambda: service.restore_version(
                actor,
                asset_id,
                version_id,
                expected_asset_version=body.expected_asset_version,
            ),
            AgentVersionResponse,
        )

    async def publish_agent_version(
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        body: ExpectedAssetVersionRequest,
        actor=Depends(actor_dependency),
        service=Depends(get_agent_service),
    ):
        return await _version_call(
            actor,
            lambda: service.publish(
                actor,
                asset_id,
                version_id,
                expected_asset_version=body.expected_asset_version,
            ),
            AgentVersionResponse,
        )

    async def get_agent_versions(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        return await _version_history(actor, lambda: service.get_version_history(actor, asset_id), AgentVersionHistoryResponse)

    async def delete_agent(asset_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        try:
            await service.delete(
                actor,
                asset_id,
                expected_asset_version=body.expected_asset_version,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def get_skill(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _asset_call(actor, lambda: service.get(actor, asset_id))

    async def create_skill_version(asset_id: uuid.UUID, body: SkillVersionRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        files = _decode_skill_files(body, actor.request_id)
        return await _version_call(actor, lambda: service.create_version_from_archive(actor, asset_id, files, expected_asset_version=body.expected_asset_version), SkillVersionResponse)

    async def get_skill_versions(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _version_history(actor, lambda: service.get_version_history(actor, asset_id), SkillVersionHistoryResponse)

    async def publish_skill(asset_id: uuid.UUID, version_id: uuid.UUID, body: SkillPublishRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _version_call(
            actor,
            lambda: service.publish(
                actor,
                asset_id,
                version_id,
                expected_asset_version=body.expected_asset_version,
                acknowledge_stale_base=body.acknowledge_stale_base,
                expected_payload_checksum=body.expected_payload_checksum,
                expected_binding_revision=body.expected_binding_revision,
                credential_bindings=(
                    None
                    if body.credential_bindings is None
                    else tuple(
                        SkillCredentialBindingInput(
                            item.name,
                            item.credential_version_id,
                        )
                        for item in body.credential_bindings
                    )
                ),
            ),
            SkillVersionResponse,
        )

    async def delete_skill(asset_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        try:
            await service.delete(
                actor,
                asset_id,
                expected_asset_version=body.expected_asset_version,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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

    async def approve_mcp(asset_id: uuid.UUID, version_id: uuid.UUID, body: McpApproveRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_call(actor, lambda: service.approve(actor, asset_id, version_id, body.credential_versions, expected_asset_version=body.expected_asset_version), McpVersionResponse)

    def add_status_route(
        segment: str,
        action: Literal["activate", "archive", "suspend"],
        service_dependency,
    ) -> None:
        async def change(asset_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(service_dependency)):
            return await _asset_call(actor, lambda: getattr(service, action)(actor, asset_id, expected_asset_version=body.expected_asset_version))

        router.add_api_route(
            f"/{segment}/{{asset_id}}/{action}",
            change,
            methods=["POST"],
            response_model=AssetMutationResponse,
            name=f"{action}_{segment}",
        )

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

    async def get_credential_versions(credential_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        return await _version_history(actor, lambda: service.get_version_history(actor, credential_id), CredentialVersionHistoryResponse)

    async def replace_credential(credential_id: uuid.UUID, body: CredentialReplaceRequest, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        try:
            result = await service.replace(actor, credential_id, body.payload, expected_credential_version=body.expected_credential_version)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        pending = result.pending_migration
        return CredentialVersionResponse(
            data=_response_data(result.version, redact_project_mcp=_is_project_asset_actor(actor)),
            pending_migration=CredentialPendingMigrationResponse(**vars(pending)) if pending is not None else None,
            request_id=actor.request_id,
        )

    async def revoke_credential(credential_id: uuid.UUID, body: CredentialRevokeRequest, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        try:
            view = await service.revoke(actor, credential_id, expected_credential_version=body.expected_credential_version)
            return CredentialMutationResponse(item=_credential_item(view), request_id=actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def delete_credential(credential_id: uuid.UUID, body: CredentialDeleteRequest, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        try:
            await service.delete(
                actor,
                credential_id,
                expected_credential_version=body.expected_credential_version,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def migrate_credential_grants(credential_id: uuid.UUID, body: CredentialGrantMigrationRequest, actor=Depends(actor_dependency), service=Depends(get_credential_service)):
        try:
            view = await service.migrate_grants(
                actor,
                credential_id,
                expected_credential_version=body.expected_credential_version,
            )
            return CredentialGrantMigrationResponse(**vars(view), request_id=actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    read_routes = (
        ("/agents/{asset_id}", get_agent, ["GET"], AssetMutationResponse, 200),
        ("/agents/{asset_id}/versions", get_agent_versions, ["GET"], AgentVersionHistoryResponse, 200),
        ("/skills/{asset_id}", get_skill, ["GET"], AssetMutationResponse, 200),
        ("/skills/{asset_id}/versions", get_skill_versions, ["GET"], SkillVersionHistoryResponse, 200),
        ("/mcp-servers/{asset_id}", get_mcp, ["GET"], AssetMutationResponse, 200),
        ("/mcp-servers/{asset_id}/versions", get_mcp_versions, ["GET"], McpVersionHistoryResponse, 200),
    )
    shared_asset_write_routes = (
        ("/agents", create_agent, ["POST"], AgentCreateResponse, 201),
        ("/agents/{asset_id}/instructions", update_agent_instructions, ["PUT"], AgentVersionResponse, 200),
        ("/agents/{asset_id}/capability-bindings", update_agent_capability_bindings, ["PUT"], AgentVersionResponse, 200),
        ("/agents/{asset_id}/versions/{version_id}/restore", restore_agent_version, ["POST"], AgentVersionResponse, 200),
        ("/agents/{asset_id}/versions/{version_id}/publish", publish_agent_version, ["POST"], AgentVersionResponse, 200),
        ("/skills/{asset_id}/versions", create_skill_version, ["POST"], SkillVersionResponse, 201),
        ("/skills/{asset_id}/versions/{version_id}/publish", publish_skill, ["POST"], SkillVersionResponse, 200),
        ("/mcp-servers", create_mcp, ["POST"], AssetMutationResponse, 201),
        ("/mcp-servers/{asset_id}/versions", create_mcp_version, ["POST"], McpVersionResponse, 201),
        ("/mcp-servers/{asset_id}/versions/{version_id}/publish", publish_mcp, ["POST"], McpVersionResponse, 200),
        ("/mcp-servers/{asset_id}/versions/{version_id}/submit-approval", submit_mcp, ["POST"], McpVersionResponse, 200),
        ("/mcp-servers/{asset_id}/versions/{version_id}/approve", approve_mcp, ["POST"], McpVersionResponse, 200),
    )
    credential_routes = (
        ("/credentials", create_credential, ["POST"], CredentialMutationResponse, 201),
        ("/credentials/{credential_id}", get_credential, ["GET"], CredentialMutationResponse, 200),
        ("/credentials/{credential_id}/versions", get_credential_versions, ["GET"], CredentialVersionHistoryResponse, 200),
        ("/credentials/{credential_id}/replace", replace_credential, ["POST"], CredentialVersionResponse, 200),
        ("/credentials/{credential_id}/revoke", revoke_credential, ["POST"], CredentialMutationResponse, 200),
        ("/credentials/{credential_id}/migrate-grants", migrate_credential_grants, ["POST"], CredentialGrantMigrationResponse, 200),
        ("/credentials/{credential_id}", delete_credential, ["DELETE"], None, status.HTTP_204_NO_CONTENT),
    )
    routes = (*read_routes, *credential_routes)
    if include_shared_asset_mutations:
        routes = (*routes, *shared_asset_write_routes)
    for path, endpoint, methods, response_model, code in routes:
        router.add_api_route(path, endpoint, methods=methods, response_model=response_model, status_code=code)
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
            response_model=None,
            status_code=status.HTTP_204_NO_CONTENT,
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
        add_status_route("agents", "activate", get_agent_service)
        add_status_route("agents", "suspend", get_agent_service)
        add_status_route("mcp-servers", "archive", get_mcp_service)
        add_status_route("mcp-servers", "activate", get_mcp_service)
        add_status_route("mcp-servers", "suspend", get_mcp_service)
        add_status_route("skills", "activate", get_skill_service)
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
            expected_asset_version=body.expected_asset_version,
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
            item=_asset_item(result.asset),
            version=SkillVersionItemResponse.model_validate(
                _response_data(result.version),
            ),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


def _skill_credential_binding_response(
    value,
    request_id: str,
) -> SkillCredentialBindingSetResponse:
    return SkillCredentialBindingSetResponse(
        **_response_data(value),
        request_id=request_id,
    )


@project_router.get(
    "/skills/{skill_id}/versions/{version_id}/publish-plan",
    response_model=SkillPublishPlanResponse,
)
async def get_project_skill_publish_plan(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillCredentialBindingService,
        Depends(get_skill_credential_binding_service),
    ],
):
    try:
        value = await service.get_for_version(context, skill_id, version_id)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return SkillPublishPlanResponse(
            **_response_data(value),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.get(
    "/skills/{skill_id}/credential-bindings",
    response_model=SkillCredentialBindingSetResponse,
)
async def get_project_skill_credential_bindings(
    skill_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillCredentialBindingService,
        Depends(get_skill_credential_binding_service),
    ],
):
    try:
        return _skill_credential_binding_response(
            await service.get(context, skill_id),
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.put(
    "/skills/{skill_id}/credential-bindings",
    response_model=SkillCredentialBindingSetResponse,
)
async def replace_project_skill_credential_bindings(
    skill_id: uuid.UUID,
    body: SkillCredentialBindingReplaceRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillCredentialBindingService,
        Depends(get_skill_credential_binding_service),
    ],
):
    try:
        return _skill_credential_binding_response(
            await service.replace(
                context,
                skill_id,
                tuple(
                    SkillCredentialBindingInput(
                        item.name,
                        item.credential_version_id,
                    )
                    for item in body.bindings
                ),
                expected_revision=body.expected_revision,
            ),
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@project_router.get("/agents", response_model=ScopedAssetListResponse)
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


@project_router.get("/skills", response_model=ScopedSkillAssetListResponse)
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


@project_router.get("/credentials", response_model=ScopedCredentialListResponse)
async def list_project_credentials(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[CredentialService, Depends(get_credential_service)],
):
    try:
        items = [
            ProjectCredentialItemResponse(
                **vars(view),
                capabilities=_credential_item_capabilities(context, view.scope),
            )
            for view in await service.list_visible(context)
        ]
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

    project_router.add_api_route(path, enable, methods=["POST"], response_model=BindingResponse, status_code=status.HTTP_201_CREATED, name=f"enable_system_{segment}_binding")
    project_router.add_api_route(f"{path}/{{asset_id}}/disable", disable, methods=["POST"], response_model=BindingResponse, name=f"disable_system_{segment}_binding")
    project_router.add_api_route(f"{path}/{{asset_id}}/upgrade", upgrade, methods=["POST"], response_model=BindingResponse, name=f"upgrade_system_{segment}_binding")
    project_router.add_api_route(f"{path}/{{asset_id}}/rollback", rollback, methods=["POST"], response_model=BindingResponse, name=f"rollback_system_{segment}_binding")
    if kind is AssetKind.MCP:
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
