from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, TypeVar
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.mcp_discovery_repository import (
    McpToolDiscoveryAttemptRecord,
    McpToolDiscoveryAttemptRepository,
)
from app.shared_assets.mcp_repository import McpRepository, McpVersionRecord
from app.shared_assets.mcp_secret_closure import lock_mcp_secret_closure
from app.shared_assets.mcp_secret_store import (
    McpSecretStore,
    mcp_secret_closure_digest,
)
from app.shared_assets.mcp_tool_inventory_repository import (
    McpToolInventoryRecord,
    McpToolInventoryRepository,
)
from app.shared_assets.models import AssetScope, WorkflowStatus
from deerflow.mcp_definition_policy import (
    McpEndpointPolicy,
    validate_project_mcp_definition,
)
from deerflow.persistence.shared_assets import (
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
)

_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SLOT_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,62}\Z")
_TRANSPORTS = frozenset({"stdio", "sse", "http", "streamable_http"})
_SCHEMA_SECTIONS = frozenset({"env", "headers", "oauth", "query"})
_OAUTH_FIELDS = frozenset(
    {
        "enabled",
        "token_url",
        "grant_type",
        "scope",
        "audience",
        "token_field",
        "token_type_field",
        "expires_in_field",
        "default_token_type",
        "refresh_skew_seconds",
        "extra_token_params",
    }
)
_SENSITIVE_KEY = re.compile(
    r"(^|_)(api_key|apikey|access_key|private_key|client_secret|refresh_token|secret|token|password|passwd|credential|credentials|auth|authorization|bearer|cookie)(_|$)",
    re.IGNORECASE,
)
_SAFE_CONTROL_KEYS = frozenset({"auth_mode", "authentication_mode", "oauth_mode"})
_SENSITIVE_COMPACT_FRAGMENTS = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
)
_SENSITIVE_VALUE = re.compile(
    r"(?:\b(?:basic|bearer)\s+\S+|\b(?:access[_-]?key|access[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|private[_-]?key|refresh[_-]?token|token)\s*[:=]\s*\S+|-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----)",
    re.IGNORECASE,
)
_URL_CANDIDATE = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "uq_mcp_servers_project_slug",
        "uq_mcp_servers_system_slug",
        "uq_mcp_server_versions_asset_number",
    }
)
_MCP_DISCOVERY_IDEMPOTENCY_DOMAIN = b"actweave:mcp-tool-discovery:v1\0"
_Actor = ProjectContext | SystemAssetGovernanceContext | SystemAssetReadContext
_T = TypeVar("_T")


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "constraint_name", None)
        if isinstance(value, str):
            return value
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


def _normalized_key(value: str) -> str:
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    second = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first)
    return re.sub(r"[^a-z0-9]+", "_", second.lower()).strip("_")


@dataclass(frozen=True)
class CreateMcpServer:
    slug: str
    display_name: str


@dataclass(frozen=True)
class McpSecretSlot:
    name: str
    purpose: str
    payload_schema: Mapping[str, object]
    required: bool = True


@dataclass(frozen=True)
class McpDefinition:
    description: str = ""
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    oauth: Mapping[str, object] = field(default_factory=dict)
    routing: Mapping[str, object] = field(default_factory=dict)
    tool_overrides: Mapping[str, object] = field(default_factory=dict)
    timeout_seconds: int = 30
    secret_slots: tuple[McpSecretSlot, ...] = ()


@dataclass(frozen=True)
class McpAssetView:
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


@dataclass(frozen=True)
class McpSecretSlotView:
    id: uuid.UUID
    name: str
    purpose: str
    payload_schema: Mapping[str, tuple[str, ...]]
    required: bool


@dataclass(frozen=True)
class McpVersionView:
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    version_number: int
    workflow_status: WorkflowStatus
    definition: McpDefinition
    secret_slots: tuple[McpSecretSlotView, ...]
    supersedes_version_id: uuid.UUID | None
    payload_checksum: str
    submitted_at: datetime | None
    reviewed_at: datetime | None
    reviewed_by_user_id: str | None
    created_by_user_id: str
    created_at: datetime


@dataclass(frozen=True)
class ProjectMcpConfiguredCreateResult:
    asset: McpAssetView
    version: McpVersionView


@dataclass(frozen=True)
class McpToolView:
    name: str
    description: str


@dataclass(frozen=True)
class McpToolInventoryView:
    status: Literal[
        "never_discovered",
        "testing",
        "ready",
        "degraded",
        "failed",
        "stale",
    ]
    tools: tuple[McpToolView, ...]
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    error_code: (
        Literal[
            "mcp_discovery_unavailable",
            "mcp_catalog_invalid",
        ]
        | None
    )


@dataclass(frozen=True, slots=True)
class McpToolDiscoveryAttemptView:
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: Literal["mcp_discovery_unavailable", "mcp_catalog_invalid"] | None


def _mcp_tool_inventory_view(
    *,
    payload_checksum: str,
    secret_digest: str,
    inventory: McpToolInventoryRecord | None,
    testing: bool = False,
    latest_attempt: McpToolDiscoveryAttemptRecord | None = None,
) -> McpToolInventoryView:
    latest_failure_at: datetime | None = None
    latest_failure_code: (
        Literal[
            "mcp_discovery_unavailable",
            "mcp_catalog_invalid",
        ]
        | None
    ) = None
    if latest_attempt is not None and latest_attempt.status == "failed" and latest_attempt.payload_checksum == payload_checksum and latest_attempt.secret_digest == secret_digest and latest_attempt.public_error_code is not None:
        candidate_at = latest_attempt.completed_at or latest_attempt.requested_at
        if inventory is None or candidate_at > inventory.last_attempt_at:
            latest_failure_at = candidate_at
            latest_failure_code = latest_attempt.public_error_code
    if inventory is None:
        if latest_failure_at is not None and not testing:
            return McpToolInventoryView(
                status="failed",
                tools=(),
                last_attempt_at=latest_failure_at,
                last_success_at=None,
                error_code=latest_failure_code,
            )
        return McpToolInventoryView(
            status="testing" if testing else "never_discovered",
            tools=(),
            last_attempt_at=None,
            last_success_at=None,
            error_code=None,
        )
    attempt_matches = inventory.attempt_payload_checksum == payload_checksum and inventory.attempt_secret_digest == secret_digest
    tools_match = inventory.tools_payload_checksum == payload_checksum and inventory.tools_secret_digest == secret_digest and inventory.last_success_at is not None
    tools = (
        tuple(
            McpToolView(
                name=item["name"],
                description=item["description"],
            )
            for item in inventory.tools
        )
        if tools_match
        else ()
    )
    if testing:
        status: Literal["testing", "degraded", "failed", "ready", "stale"] = "testing"
        error_code = None
        last_attempt_at = inventory.last_attempt_at
    elif latest_failure_at is not None:
        status = "degraded" if tools_match else "failed"
        error_code = latest_failure_code
        last_attempt_at = latest_failure_at
    elif inventory.attempt_status == "failed" and attempt_matches:
        status = "degraded" if tools_match else "failed"
        error_code = inventory.public_error_code
        last_attempt_at = inventory.last_attempt_at
    elif tools_match:
        status = "ready"
        error_code = None
        last_attempt_at = inventory.last_attempt_at
    else:
        status = "stale"
        error_code = None
        last_attempt_at = inventory.last_attempt_at
    return McpToolInventoryView(
        status=status,
        tools=tools,
        last_attempt_at=last_attempt_at,
        last_success_at=inventory.last_success_at,
        error_code=error_code,
    )


def _mcp_tool_discovery_attempt_view(
    record: McpToolDiscoveryAttemptRecord,
) -> McpToolDiscoveryAttemptView:
    return McpToolDiscoveryAttemptView(
        id=record.attempt_id,
        mcp_server_id=record.mcp_server_id,
        mcp_server_version_id=record.mcp_server_version_id,
        status=record.status,
        requested_at=record.requested_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        error_code=record.public_error_code,
    )


def _mcp_tool_discovery_idempotency_key(
    *,
    project_id: uuid.UUID,
    mcp_server_id: uuid.UUID,
    mcp_server_version_id: uuid.UUID,
    payload_checksum: str,
    secret_digest: str,
    trigger: Literal["auto", "manual"],
    nonce: uuid.UUID | None = None,
) -> str:
    digest = hashlib.sha256(_MCP_DISCOVERY_IDEMPOTENCY_DOMAIN)
    digest.update(project_id.bytes)
    digest.update(mcp_server_id.bytes)
    digest.update(mcp_server_version_id.bytes)
    digest.update(payload_checksum.encode("ascii"))
    digest.update(secret_digest.encode("ascii"))
    digest.update(trigger.encode("ascii"))
    if nonce is not None:
        digest.update(nonce.bytes)
    return digest.hexdigest()


class McpService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        governance_sink: SharedAssetGovernanceEventSink | None = None,
        *,
        endpoint_policy: McpEndpointPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()
        self._endpoint_policy = endpoint_policy

    async def create_asset(self, actor: _Actor, command: CreateMcpServer) -> McpAssetView:
        command = self._validate_create(actor, command)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        scope, project_id = self._scope(actor)

        async def operation(repository: McpRepository) -> McpAssetView:
            row = McpServerRow(
                scope=scope.value,
                project_id=project_id,
                slug=command.slug,
                display_name=command.display_name,
                created_by_user_id=str(actor.user_id),
            )
            if isinstance(actor, ProjectContext):
                await repository.create_project_asset(actor, row)
            elif project_id is not None:
                await repository.create_override_asset(actor, row)
            else:
                await repository.create_system_asset(actor, row)
            return self._asset_view(row)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, result.id, None, "mcp.create"),
        )

    async def create_project_configured(
        self,
        actor: ProjectContext,
        command: CreateMcpServer,
        definition: McpDefinition,
    ) -> ProjectMcpConfiguredCreateResult:
        """Create a project MCP and advance its initial version atomically."""

        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        command = self._validate_create(actor, command)
        definition = self._validate_definition(
            actor,
            definition,
            endpoint_policy=self._endpoint_policy,
        )
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(
            repository: McpRepository,
        ) -> ProjectMcpConfiguredCreateResult:
            asset = McpServerRow(
                scope=AssetScope.PROJECT.value,
                project_id=actor.project_id,
                slug=command.slug,
                display_name=command.display_name,
                created_by_user_id=str(actor.user_id),
            )
            await repository.create_project_asset(actor, asset)
            version_id = uuid.uuid4()
            version = McpServerVersionRow(
                id=version_id,
                mcp_server_id=asset.id,
                version_number=await repository.next_version_number(asset),
                workflow_status=WorkflowStatus.DRAFT.value,
                description=definition.description,
                transport=definition.transport,
                command=definition.command,
                args=list(definition.args),
                url=definition.url,
                non_secret_env=dict(definition.env),
                non_secret_headers=dict(definition.headers),
                oauth_metadata=dict(definition.oauth),
                routing=dict(definition.routing),
                tool_overrides=dict(definition.tool_overrides),
                timeout_seconds=definition.timeout_seconds,
                supersedes_version_id=None,
                payload_checksum=self._checksum(definition),
                created_by_user_id=str(actor.user_id),
            )
            slots = tuple(
                McpSecretSlotRow(
                    mcp_server_version_id=version_id,
                    name=slot.name,
                    purpose=slot.purpose,
                    payload_schema={key: list(values) for key, values in slot.payload_schema.items()},
                    required=slot.required,
                )
                for slot in definition.secret_slots
            )
            record = await repository.add_version(
                asset,
                version,
                slots,
                request_id=actor.request_id,
            )
            asset.version += 1
            await repository.session.flush()

            await self._publish_draft_in_session(
                actor,
                repository,
                asset,
                record,
            )
            if not record.slots:
                await self._enqueue_tool_discovery_in_session(
                    actor,
                    repository,
                    asset_id=asset.id,
                    version_id=record.row.id,
                    payload_checksum=record.row.payload_checksum,
                    secret_digest=hashlib.sha256(b"actweave:mcp-secret-closure:v1\0").hexdigest(),
                    trigger="auto",
                )
            return ProjectMcpConfiguredCreateResult(
                asset=self._asset_view(asset),
                version=self._version_view(record),
            )

        async def governance(
            session: AsyncSession,
            result: ProjectMcpConfiguredCreateResult,
        ) -> None:
            await self._record_governance(
                session,
                actor,
                result.asset.id,
                None,
                "mcp.create",
            )
            await self._record_governance(
                session,
                actor,
                result.asset.id,
                result.version.id,
                "mcp.version.create",
            )
            await self._record_governance(
                session,
                actor,
                result.asset.id,
                result.version.id,
                "mcp.publish",
            )

        return await self._execute(actor, operation, governance=governance)

    async def update_project_configured(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        definition: McpDefinition,
        *,
        expected_asset_version: int,
    ) -> ProjectMcpConfiguredCreateResult:
        """Create and advance one project MCP configuration revision atomically."""

        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        definition = self._validate_definition(
            actor,
            definition,
            endpoint_policy=self._endpoint_policy,
        )
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(
            repository: McpRepository,
        ) -> ProjectMcpConfiguredCreateResult:
            asset = await repository.get_project_asset(
                actor,
                asset_id,
                for_update=True,
            )
            self._require_expected_version(
                actor,
                asset,
                expected_asset_version,
            )
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
            source = None
            if asset.current_published_version_id is not None:
                source = await repository.get_project_version(
                    actor,
                    asset.id,
                    asset.current_published_version_id,
                    for_update=True,
                )
            version_id = uuid.uuid4()
            version = McpServerVersionRow(
                id=version_id,
                mcp_server_id=asset.id,
                version_number=await repository.next_version_number(asset),
                workflow_status=WorkflowStatus.DRAFT.value,
                description=definition.description,
                transport=definition.transport,
                command=definition.command,
                args=list(definition.args),
                url=definition.url,
                non_secret_env=dict(definition.env),
                non_secret_headers=dict(definition.headers),
                oauth_metadata=dict(definition.oauth),
                routing=dict(definition.routing),
                tool_overrides=dict(definition.tool_overrides),
                timeout_seconds=definition.timeout_seconds,
                supersedes_version_id=asset.current_published_version_id,
                payload_checksum=self._checksum(definition),
                created_by_user_id=str(actor.user_id),
            )
            slots = tuple(
                McpSecretSlotRow(
                    mcp_server_version_id=version_id,
                    name=slot.name,
                    purpose=slot.purpose,
                    payload_schema={key: list(values) for key, values in slot.payload_schema.items()},
                    required=slot.required,
                )
                for slot in definition.secret_slots
            )
            record = await repository.add_version(
                asset,
                version,
                slots,
                request_id=actor.request_id,
            )
            asset.version += 1
            await repository.session.flush()
            await self._copy_project_secrets_if_compatible(
                actor,
                repository,
                asset,
                source,
                record,
            )
            await self._publish_draft_in_session(
                actor,
                repository,
                asset,
                record,
            )
            await self._enqueue_if_ready(actor, repository, asset, record)
            return ProjectMcpConfiguredCreateResult(
                asset=self._asset_view(asset),
                version=self._version_view(record),
            )

        async def governance(
            session: AsyncSession,
            result: ProjectMcpConfiguredCreateResult,
        ) -> None:
            await self._record_governance(
                session,
                actor,
                result.asset.id,
                result.version.id,
                "mcp.version.create",
            )
            await self._record_governance(
                session,
                actor,
                result.asset.id,
                result.version.id,
                "mcp.publish",
            )

        return await self._execute(actor, operation, governance=governance)

    async def get_project_configured(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
    ) -> ProjectMcpConfiguredCreateResult:
        """Load only the current editable project configuration.

        Historical definitions remain on the origin-only history surface.
        This path-bearing view is restricted to editors and revalidates the
        exact selected definition against the process-frozen endpoint policy.
        """

        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(
            repository: McpRepository,
        ) -> ProjectMcpConfiguredCreateResult:
            asset = await repository.get_project_asset(
                actor,
                asset_id,
                for_update=True,
            )
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
            record = await repository.get_project_current_configuration(
                actor,
                asset,
                for_update=True,
            )
            if record is None:
                raise AssetConflict(actor.request_id)
            self._validate_transition_definition(actor, record)
            return ProjectMcpConfiguredCreateResult(
                asset=self._asset_view(asset),
                version=self._version_view(record),
            )

        return await self._execute(actor, operation)

    async def create_version(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        definition: McpDefinition,
        *,
        expected_asset_version: int,
    ) -> McpVersionView:
        definition = self._validate_definition(
            actor,
            definition,
            endpoint_policy=self._endpoint_policy,
        )
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: McpRepository) -> McpVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
            source = None
            if isinstance(actor, ProjectContext) and asset.current_published_version_id is not None:
                source = await repository.get_project_version(
                    actor,
                    asset.id,
                    asset.current_published_version_id,
                    for_update=True,
                )
            version_id = uuid.uuid4()
            version = McpServerVersionRow(
                id=version_id,
                mcp_server_id=asset.id,
                version_number=await repository.next_version_number(asset),
                workflow_status=WorkflowStatus.DRAFT.value,
                description=definition.description,
                transport=definition.transport,
                command=definition.command,
                args=list(definition.args),
                url=definition.url,
                non_secret_env=dict(definition.env),
                non_secret_headers=dict(definition.headers),
                oauth_metadata=dict(definition.oauth),
                routing=dict(definition.routing),
                tool_overrides=dict(definition.tool_overrides),
                timeout_seconds=definition.timeout_seconds,
                supersedes_version_id=asset.current_published_version_id,
                payload_checksum=self._checksum(definition),
                created_by_user_id=str(actor.user_id),
            )
            slots = tuple(
                McpSecretSlotRow(
                    mcp_server_version_id=version_id,
                    name=slot.name,
                    purpose=slot.purpose,
                    payload_schema={key: list(values) for key, values in slot.payload_schema.items()},
                    required=slot.required,
                )
                for slot in definition.secret_slots
            )
            record = await repository.add_version(
                asset,
                version,
                slots,
                request_id=actor.request_id,
            )
            asset.version += 1
            await repository.session.flush()
            if isinstance(actor, ProjectContext):
                await self._copy_project_secrets_if_compatible(
                    actor,
                    repository,
                    asset,
                    source,
                    record,
                )
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, asset_id, result.id, "mcp.version.create"),
        )

    async def submit_approval(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> McpVersionView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: McpRepository) -> McpVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            record = await self._get_version(repository, actor, asset_id, version_id, for_update=True)
            await self._submit_draft_for_approval_in_session(
                actor,
                repository,
                asset,
                record,
            )
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, asset_id, result.id, "mcp.submit_approval"),
        )

    async def publish(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> McpVersionView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: McpRepository) -> McpVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            record = await self._get_version(repository, actor, asset_id, version_id, for_update=True)
            await self._publish_draft_in_session(
                actor,
                repository,
                asset,
                record,
            )
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, asset_id, result.id, "mcp.publish"),
        )

    async def _submit_draft_for_approval_in_session(
        self,
        actor: _Actor,
        repository: McpRepository,
        asset: McpServerRow,
        record: McpVersionRecord,
    ) -> None:
        if asset.status != "active" or record.row.workflow_status != WorkflowStatus.DRAFT.value or not record.slots:
            raise AssetConflict(actor.request_id)
        self._require_current_lineage(actor, asset, record)
        self._validate_transition_definition(actor, record)
        record.row.workflow_status = WorkflowStatus.PENDING_APPROVAL.value
        record.row.submitted_at = datetime.now(UTC)
        asset.version += 1
        await repository.session.flush()

    async def _publish_draft_in_session(
        self,
        actor: _Actor,
        repository: McpRepository,
        asset: McpServerRow,
        record: McpVersionRecord,
    ) -> None:
        if asset.status != "active" or record.row.workflow_status != WorkflowStatus.DRAFT.value:
            raise AssetConflict(actor.request_id)
        self._require_current_lineage(actor, asset, record)
        self._validate_transition_definition(actor, record)
        record.row.workflow_status = WorkflowStatus.PUBLISHED.value
        asset.current_published_version_id = record.row.id
        asset.version += 1
        await repository.session.flush()

    @staticmethod
    def _require_current_lineage(
        actor: _Actor,
        asset: McpServerRow,
        record: McpVersionRecord,
    ) -> None:
        try:
            matches = record.row.supersedes_version_id == asset.current_published_version_id
        except AttributeError:
            matches = False
        if not matches:
            raise AssetConflict(actor.request_id)

    async def archive(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> McpAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        return await self._change_status(
            actor,
            asset_id,
            expected_asset_version,
            "archived",
            "mcp.archive",
        )

    async def delete(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> None:
        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: McpRepository) -> None:
            asset = await repository.get_project_asset(
                actor,
                asset_id,
                for_update=True,
            )
            self._require_expected_version(actor, asset, expected_asset_version)
            version_ids = await repository.plan_project_asset_deletion(actor, asset)
            await repository.delete_project_asset(actor, asset, version_ids)

        await self._execute(
            actor,
            operation,
            governance=lambda session, _result: self._record_governance(
                session,
                actor,
                asset_id,
                None,
                "mcp.delete",
            ),
        )

    async def suspend(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> McpAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)

        async def operation(repository: McpRepository) -> McpAssetView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "active" or asset.current_published_version_id is None:
                raise AssetConflict(actor.request_id)
            current = await self._get_version(
                repository,
                actor,
                asset.id,
                asset.current_published_version_id,
                for_update=True,
            )
            if current.row.workflow_status != WorkflowStatus.PUBLISHED.value:
                raise AssetConflict(actor.request_id)
            asset.status = "suspended"
            asset.version += 1
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                result.id,
                None,
                "mcp.suspend",
            ),
        )

    async def activate(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> McpAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)

        async def operation(repository: McpRepository) -> McpAssetView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "suspended" or asset.current_published_version_id is None:
                raise AssetConflict(actor.request_id)
            current = await self._get_version(
                repository,
                actor,
                asset.id,
                asset.current_published_version_id,
                for_update=True,
            )
            if current.row.workflow_status != WorkflowStatus.PUBLISHED.value:
                raise AssetConflict(actor.request_id)
            self._validate_transition_definition(actor, current)
            if current.slots:
                project_id = getattr(actor, "project_id", None)
                if not isinstance(project_id, uuid.UUID):
                    raise AssetForbidden(actor.request_id)
                await lock_mcp_secret_closure(
                    repository.session,
                    project_id=project_id,
                    mcp_server_id=asset.id,
                    mcp_server_version_id=current.row.id,
                    slots=current.slots,
                    request_id=actor.request_id,
                )
            asset.status = "active"
            asset.version += 1
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                result.id,
                result.current_published_version_id,
                "mcp.activate",
            ),
        )

    async def get(self, actor: _Actor, asset_id: uuid.UUID) -> McpAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: McpRepository) -> McpAssetView:
            return self._asset_view(await self._get_asset(repository, actor, asset_id))

        return await self._execute(actor, operation)

    async def list_visible(self, actor: _Actor) -> tuple[McpAssetView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: McpRepository) -> tuple[McpAssetView, ...]:
            if isinstance(actor, ProjectContext):
                rows = await repository.list_project_visible(actor)
            elif actor.project_id is not None:
                rows = await repository.list_override_visible(actor)
            else:
                rows = await repository.list_system_visible(actor)
            return tuple(self._asset_view(row) for row in rows)

        return await self._execute(actor, operation)

    async def get_version_history(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
    ) -> tuple[McpVersionView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: McpRepository) -> tuple[McpVersionView, ...]:
            if isinstance(actor, ProjectContext):
                records = await repository.get_project_version_history(actor, asset_id)
            elif actor.project_id is not None:
                records = await repository.get_override_version_history(actor, asset_id)
            else:
                records = await repository.get_system_version_history(actor, asset_id)
            return tuple(self._version_view(record) for record in records)

        return await self._execute(actor, operation)

    async def request_tool_discovery(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> McpToolDiscoveryAttemptView:
        """Queue one exact current project MCP discovery without contacting it."""

        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        self._require_capability(actor, Capability.SHARED_ASSETS_EXECUTE)

        async def operation(repository: McpRepository) -> McpToolDiscoveryAttemptView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            record = await self._get_version(
                repository,
                actor,
                asset_id,
                version_id,
                for_update=True,
            )
            if asset.status != "active" or asset.current_published_version_id != version_id or record.row.workflow_status != WorkflowStatus.PUBLISHED.value:
                raise AssetConflict(actor.request_id)
            self._validate_transition_definition(actor, record)
            closure = await lock_mcp_secret_closure(
                repository.session,
                project_id=actor.project_id,
                mcp_server_id=asset_id,
                mcp_server_version_id=version_id,
                slots=record.slots,
                request_id=actor.request_id,
            )
            attempts = McpToolDiscoveryAttemptRepository(repository.session)
            try:
                active = await attempts.active_for_closure(
                    actor.project_id,
                    asset_id,
                    version_id,
                    record.row.payload_checksum,
                    closure.digest,
                )
            except (TypeError, ValueError):
                raise AssetStorageUnavailable(actor.request_id) from None
            if active is not None:
                return _mcp_tool_discovery_attempt_view(active)
            return _mcp_tool_discovery_attempt_view(
                await self._enqueue_tool_discovery_in_session(
                    actor,
                    repository,
                    asset_id=asset_id,
                    version_id=version_id,
                    payload_checksum=record.row.payload_checksum,
                    secret_digest=closure.digest,
                    trigger="manual",
                )
            )

        return await self._execute(actor, operation)

    async def get_tool_discovery_attempt(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        attempt_id: uuid.UUID | None = None,
    ) -> McpToolDiscoveryAttemptView:
        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: McpRepository) -> McpToolDiscoveryAttemptView:
            await repository.get_project_visible_version(actor, asset_id, version_id)
            attempts = McpToolDiscoveryAttemptRepository(repository.session)
            try:
                if attempt_id is None:
                    record = await attempts.latest_for_version(
                        actor.project_id,
                        asset_id,
                        version_id,
                    )
                else:
                    record = await attempts.get(actor.project_id, attempt_id)
            except (TypeError, ValueError):
                raise AssetStorageUnavailable(actor.request_id) from None
            if record is None or record.mcp_server_id != asset_id or record.mcp_server_version_id != version_id:
                raise AssetNotFound(actor.request_id)
            return _mcp_tool_discovery_attempt_view(record)

        return await self._execute(actor, operation)

    async def get_tool_inventory(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> McpToolInventoryView:
        """Read one project-scoped Worker observation without contacting MCP."""

        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: McpRepository) -> McpToolInventoryView:
            record = await repository.get_project_visible_version(
                actor,
                asset_id,
                version_id,
            )
            try:
                inventory = await McpToolInventoryRepository(repository.session).get(
                    project_id=actor.project_id,
                    mcp_server_id=asset_id,
                    mcp_server_version_id=version_id,
                )
                materials = await McpSecretStore(repository.session).load_materials(
                    project_id=actor.project_id,
                    mcp_server_id=asset_id,
                    mcp_server_version_id=version_id,
                    slots=record.slots,
                    require_required=False,
                    for_update=False,
                    request_id=actor.request_id,
                )
                secret_digest = mcp_secret_closure_digest(materials)
                attempts = McpToolDiscoveryAttemptRepository(repository.session)
                active_discovery = await attempts.active_for_closure(
                    actor.project_id,
                    asset_id,
                    version_id,
                    record.row.payload_checksum,
                    secret_digest,
                )
                latest_discovery = await attempts.latest_for_version(
                    actor.project_id,
                    asset_id,
                    version_id,
                )
            except (TypeError, ValueError):
                raise AssetStorageUnavailable(actor.request_id) from None
            return _mcp_tool_inventory_view(
                payload_checksum=record.row.payload_checksum,
                secret_digest=secret_digest,
                inventory=inventory,
                testing=active_discovery is not None,
                latest_attempt=latest_discovery,
            )

        return await self._execute(actor, operation)

    async def _enqueue_tool_discovery_in_session(
        self,
        actor: ProjectContext,
        repository: McpRepository,
        *,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        payload_checksum: str,
        secret_digest: str,
        trigger: Literal["auto", "manual"],
    ) -> McpToolDiscoveryAttemptRecord:
        idempotency_key = _mcp_tool_discovery_idempotency_key(
            project_id=actor.project_id,
            mcp_server_id=asset_id,
            mcp_server_version_id=version_id,
            payload_checksum=payload_checksum,
            secret_digest=secret_digest,
            trigger=trigger,
            nonce=uuid.uuid4() if trigger == "manual" else None,
        )
        try:
            return await McpToolDiscoveryAttemptRepository(repository.session).enqueue(
                project_id=actor.project_id,
                requested_by_user_id=actor.user_id,
                mcp_server_id=asset_id,
                mcp_server_version_id=version_id,
                payload_checksum=payload_checksum,
                secret_digest=secret_digest,
                trigger=trigger,
                idempotency_key=idempotency_key,
            )
        except (TypeError, ValueError, RuntimeError):
            raise AssetStorageUnavailable(actor.request_id) from None

    async def _copy_project_secrets_if_compatible(
        self,
        actor: ProjectContext,
        repository: McpRepository,
        asset: McpServerRow,
        source: McpVersionRecord | None,
        target: McpVersionRecord,
    ) -> None:
        if source is None or Capability.SHARED_ASSETS_MANAGE_BINDINGS not in actor.capabilities or not self._mcp_secret_copy_compatible(source, target):
            return
        store = McpSecretStore(repository.session)
        copied = await store.copy_compatible(
            project_id=actor.project_id,
            mcp_server_id=asset.id,
            source_version_id=source.row.id,
            source_slots=source.slots,
            target_version_id=target.row.id,
            target_slots=target.slots,
            actor_user_id=str(actor.user_id),
            request_id=actor.request_id,
        )
        if not copied:
            return
        current = await store.list_states(
            project_id=actor.project_id,
            mcp_server_id=asset.id,
            mcp_server_version_id=target.row.id,
            for_update=True,
        )
        by_slot = {state.slot_id: state for state in current}
        readiness = "ready" if all(not slot.required or (slot.id in by_slot and by_slot[slot.id].current_generation_id is not None) for slot in target.slots) else "unready"
        slots = {slot.id: slot for slot in target.slots}
        for state in copied:
            slot = slots[state.slot_id]
            await self._governance_sink.append_project(
                repository.session,
                actor=actor.user_id,
                project_id=actor.project_id,
                asset_id=asset.id,
                version_id=target.row.id,
                action="mcp.secret.copy",
                request_id=actor.request_id,
                asset_kind="mcp",
                secret_metadata={
                    "version_id": target.row.id,
                    "slot_id": slot.id,
                    "secret_name": slot.name,
                    "generation_id": state.current_generation_id,
                    "revision": int(state.revision),
                    "result": "copied",
                    "reason": "compatible_copy",
                    "readiness": readiness,
                },
            )

    async def _enqueue_if_ready(
        self,
        actor: ProjectContext,
        repository: McpRepository,
        asset: McpServerRow,
        record: McpVersionRecord,
    ) -> None:
        materials = await McpSecretStore(repository.session).load_materials(
            project_id=actor.project_id,
            mcp_server_id=asset.id,
            mcp_server_version_id=record.row.id,
            slots=record.slots,
            require_required=False,
            for_update=True,
            request_id=actor.request_id,
        )
        configured = {item.slot_id for item in materials}
        if any(slot.required and slot.id not in configured for slot in record.slots):
            return
        await self._enqueue_tool_discovery_in_session(
            actor,
            repository,
            asset_id=asset.id,
            version_id=record.row.id,
            payload_checksum=record.row.payload_checksum,
            secret_digest=mcp_secret_closure_digest(materials),
            trigger="auto",
        )

    @staticmethod
    def _mcp_secret_copy_compatible(
        source: McpVersionRecord,
        target: McpVersionRecord,
    ) -> bool:
        if source.row.transport != target.row.transport:
            return False

        def origin(value: str | None) -> tuple[str, str, int | None] | None:
            if value is None:
                return None
            parsed = urlsplit(value)
            port = parsed.port
            if port is None:
                port = 443 if parsed.scheme.lower() in {"https", "wss"} else 80
            return parsed.scheme.lower(), (parsed.hostname or "").lower(), port

        if origin(source.row.url) != origin(target.row.url):
            return False

        def schemas(record: McpVersionRecord):
            return tuple(
                sorted(
                    (
                        slot.name,
                        tuple(sorted((key, tuple(values)) for key, values in slot.payload_schema.items())),
                        slot.required,
                    )
                    for slot in record.slots
                )
            )

        return schemas(source) == schemas(target)

    async def _change_status(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        expected: int,
        status: str,
        audit_action: str,
    ) -> McpAssetView:
        async def operation(repository: McpRepository) -> McpAssetView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected)
            if asset.status == status:
                raise AssetConflict(actor.request_id)
            asset.status = status
            asset.version += 1
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, result.id, None, audit_action),
        )

    async def _execute(
        self,
        actor: _Actor,
        operation: Callable[[McpRepository], Awaitable[_T]],
        governance: Callable[[AsyncSession, _T], Awaitable[None]] | None = None,
    ) -> _T:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await operation(McpRepository(session))
                    if governance is not None:
                        await governance(session, result)
                    return result
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(actor.request_id) from None
            raise AssetStorageUnavailable(actor.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(actor.request_id) from None

    @staticmethod
    async def _get_asset(
        repository: McpRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
        lock_project: bool = True,
    ) -> McpServerRow:
        if isinstance(actor, ProjectContext):
            if for_update and not lock_project:
                return await repository._get_project_asset_after_lock(actor, asset_id)
            return await repository.get_project_asset(
                actor,
                asset_id,
                for_update=for_update,
            )
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            if for_update and not lock_project:
                return await repository._get_override_asset_after_lock(actor, asset_id)
            return await repository.get_override_asset(
                actor,
                asset_id,
                for_update=for_update,
            )
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_asset(actor, asset_id, for_update=for_update)
        raise AssetForbidden("unknown")

    @staticmethod
    async def _get_version(
        repository: McpRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool,
    ) -> McpVersionRecord:
        if isinstance(actor, ProjectContext):
            return await repository.get_project_version(actor, asset_id, version_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.get_override_version(actor, asset_id, version_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_version(actor, asset_id, version_id, for_update=for_update)
        raise AssetForbidden("unknown")

    @staticmethod
    async def _lock_project_first(repository: McpRepository, actor: _Actor) -> None:
        if isinstance(actor, ProjectContext):
            await repository.lock_project(actor)
        elif isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            await repository.lock_override_project(actor)

    @staticmethod
    def _validate_create(actor: _Actor, command: CreateMcpServer) -> CreateMcpServer:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(command, CreateMcpServer):
            raise AssetValidationFailed(request_id)
        slug = command.slug.strip()
        display_name = command.display_name.strip()
        if _SLUG_PATTERN.fullmatch(slug) is None or not display_name or len(display_name) > 120:
            raise AssetValidationFailed(request_id)
        return CreateMcpServer(slug, display_name)

    @classmethod
    def _validate_definition(
        cls,
        actor: _Actor,
        definition: McpDefinition,
        *,
        endpoint_policy: McpEndpointPolicy | None = None,
    ) -> McpDefinition:
        request_id = getattr(actor, "request_id", "unknown")
        try:
            if not isinstance(definition, McpDefinition):
                raise ValueError
            description = definition.description
            transport = definition.transport.strip()
            command = definition.command.strip() if isinstance(definition.command, str) else None
            url = definition.url.strip() if isinstance(definition.url, str) else None
            args = tuple(definition.args)
            env = dict(definition.env)
            headers = dict(definition.headers)
            oauth = cls._copy_json_mapping(definition.oauth)
            routing = cls._copy_json_mapping(definition.routing)
            tool_overrides = cls._copy_json_mapping(definition.tool_overrides)
            slots = tuple(cls._validate_slot(slot) for slot in definition.secret_slots)
            if cls._contains_sensitive_cli_option(args) or (command is not None and cls._contains_sensitive_cli_option(shlex.split(command))):
                raise ValueError
            if (
                not isinstance(description, str)
                or len(description) > 20_000
                or transport not in _TRANSPORTS
                or not isinstance(definition.timeout_seconds, int)
                or isinstance(definition.timeout_seconds, bool)
                or not 1 <= definition.timeout_seconds <= 86_400
                or any(not isinstance(value, str) or len(value) > 16_384 for value in args)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items())
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items())
                or len({slot.name for slot in slots}) != len(slots)
            ):
                raise ValueError
            slots = tuple(sorted(slots, key=lambda slot: slot.name))
            if transport == "stdio":
                if not command or url is not None:
                    raise ValueError
            elif not url or command is not None or args:
                raise ValueError
            if isinstance(actor, ProjectContext) or (isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None):
                url = validate_project_mcp_definition(
                    transport=transport,
                    url=url,
                    env=env,
                    headers=headers,
                    oauth=oauth,
                    secret_slot_schemas=tuple(slot.payload_schema for slot in slots),
                    endpoint_policy=endpoint_policy,
                )
            persistent_strings = (
                description,
                command,
                args,
                url,
                env,
                headers,
                oauth,
                routing,
                tool_overrides,
                tuple(slot.purpose for slot in slots),
            )
            if any(cls._sensitive_key(key) for key in env) or any(cls._sensitive_header_key(key) for key in headers) or cls._contains_sensitive_value(persistent_strings):
                raise ValueError
            if (
                not set(oauth).issubset(_OAUTH_FIELDS)
                or (oauth and oauth.get("grant_type", "client_credentials") != "client_credentials")
                or cls._contains_sensitive_key(oauth.get("extra_token_params", {}))
                or cls._contains_sensitive_key(routing)
                or cls._contains_sensitive_key(tool_overrides)
                or cls._contains_sensitive_value(oauth)
                or cls._contains_sensitive_value(routing)
                or cls._contains_sensitive_value(tool_overrides)
            ):
                raise ValueError
            return McpDefinition(
                description=description,
                transport=transport,
                command=command,
                args=args,
                url=url,
                env=MappingProxyType(env),
                headers=MappingProxyType(headers),
                oauth=MappingProxyType(oauth),
                routing=MappingProxyType(routing),
                tool_overrides=MappingProxyType(tool_overrides),
                timeout_seconds=definition.timeout_seconds,
                secret_slots=slots,
            )
        except (AttributeError, RecursionError, TypeError, ValueError):
            raise AssetValidationFailed(request_id) from None

    @classmethod
    def _validate_slot(cls, slot: McpSecretSlot) -> McpSecretSlot:
        if not isinstance(slot, McpSecretSlot):
            raise ValueError
        name = slot.name.strip()
        purpose = slot.purpose.strip()
        if _SLOT_PATTERN.fullmatch(name) is None or len(purpose) > 2_000 or not isinstance(slot.required, bool):
            raise ValueError
        schema = dict(slot.payload_schema)
        if not schema or not set(schema).issubset(_SCHEMA_SECTIONS):
            raise ValueError
        normalized: dict[str, tuple[str, ...]] = {}
        for section, raw_names in schema.items():
            if not isinstance(raw_names, (tuple, list)) or not raw_names:
                raise ValueError
            names = tuple(raw_names)
            if any(not isinstance(value, str) or not value or len(value) > 255 for value in names) or len(set(names)) != len(names):
                raise ValueError
            normalized[section] = tuple(sorted(names))
        return McpSecretSlot(name, purpose, MappingProxyType(normalized), slot.required)

    @classmethod
    def _copy_json_mapping(cls, value: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError
        # Historical rows are reconstructed behind MappingProxyType so callers
        # cannot mutate persisted definition data during transition checks.
        # json.dumps does not recognize mappingproxy directly even though it
        # satisfies Mapping; copy the top-level view before the JSON round-trip.
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise ValueError
        return decoded

    @staticmethod
    def _sensitive_key(value: str) -> bool:
        normalized = _normalized_key(value)
        if normalized in _SAFE_CONTROL_KEYS:
            return False
        compact = normalized.replace("_", "")
        return bool(_SENSITIVE_KEY.search(normalized)) or any(fragment in compact for fragment in _SENSITIVE_COMPACT_FRAGMENTS)

    @classmethod
    def _sensitive_header_key(cls, value: str) -> bool:
        normalized = _normalized_key(value)
        return cls._sensitive_key(value) or "auth" in normalized.split("_")

    @classmethod
    def _contains_sensitive_key(cls, value: object) -> bool:
        if isinstance(value, Mapping):
            return any(cls._sensitive_key(str(key)) or cls._contains_sensitive_key(nested) for key, nested in value.items())
        if isinstance(value, (list, tuple)):
            return any(cls._contains_sensitive_key(nested) for nested in value)
        return False

    @classmethod
    def _contains_sensitive_value(cls, value: object) -> bool:
        if isinstance(value, Mapping):
            return any(cls._contains_sensitive_value(nested) for nested in value.values())
        if isinstance(value, (list, tuple)):
            return any(cls._contains_sensitive_value(nested) for nested in value)
        return isinstance(value, str) and (bool(_SENSITIVE_VALUE.search(value)) or cls._contains_sensitive_url(value))

    @classmethod
    def _is_sensitive_cli_option(cls, token: str) -> bool:
        option_name = _normalized_key(re.split(r"[:=]", token.lstrip("-"), maxsplit=1)[0])
        return bool(option_name) and cls._sensitive_key(option_name)

    @classmethod
    def _contains_sensitive_cli_option(cls, tokens: tuple[str, ...] | list[str]) -> bool:
        return any(cls._is_sensitive_cli_option(token) for token in tokens)

    @classmethod
    def _contains_sensitive_url(cls, value: str) -> bool:
        for candidate in _URL_CANDIDATE.findall(value):
            parsed = urlsplit(candidate.rstrip("),.;]"))
            if parsed.username is not None or parsed.password is not None:
                return True
            for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
                if cls._sensitive_key(key) or bool(_SENSITIVE_VALUE.search(query_value)):
                    return True
            if bool(_SENSITIVE_VALUE.search(parsed.fragment)):
                return True
        return False

    @staticmethod
    def _scope(actor: _Actor) -> tuple[AssetScope, uuid.UUID | None]:
        if isinstance(actor, ProjectContext):
            return AssetScope.PROJECT, uuid.UUID(str(actor.project_id))
        if isinstance(actor, SystemAssetGovernanceContext):
            if actor.project_id is not None:
                return AssetScope.PROJECT, uuid.UUID(str(actor.project_id))
            return AssetScope.SYSTEM, None
        raise AssetForbidden("unknown")

    @staticmethod
    def _require_capability(actor: _Actor, capability: Capability) -> None:
        if isinstance(actor, SystemAssetGovernanceContext):
            if actor.project_id is not None or capability is Capability.SHARED_ASSETS_READ:
                return
            raise AssetForbidden(actor.request_id)
        if isinstance(actor, SystemAssetReadContext) and capability is Capability.SHARED_ASSETS_READ:
            return
        if isinstance(actor, ProjectContext) and capability in actor.capabilities:
            return
        raise AssetForbidden(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _require_expected_version(actor: _Actor, row: McpServerRow, expected: int) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool) or row.version != expected:
            raise AssetConflict(actor.request_id)

    @staticmethod
    def _checksum(definition: McpDefinition) -> str:
        canonical = {
            "args": list(definition.args),
            "command": definition.command,
            "secret_slots": [
                {
                    "name": slot.name,
                    "payload_schema": {key: list(values) for key, values in slot.payload_schema.items()},
                    "purpose": slot.purpose,
                    "required": slot.required,
                }
                for slot in definition.secret_slots
            ],
            "description": definition.description,
            "env": dict(definition.env),
            "headers": dict(definition.headers),
            "oauth": dict(definition.oauth),
            "routing": dict(definition.routing),
            "timeout_seconds": definition.timeout_seconds,
            "tool_overrides": dict(definition.tool_overrides),
            "transport": definition.transport,
            "url": definition.url,
        }
        value = json.dumps(canonical, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(value).hexdigest()

    def _validate_transition_definition(
        self,
        actor: _Actor,
        record: McpVersionRecord,
    ) -> McpDefinition:
        """Revalidate locked historical data at every publish boundary."""

        try:
            definition = self._definition_from_record(record)
        except (AttributeError, RecursionError, TypeError, ValueError):
            raise AssetValidationFailed(actor.request_id) from None
        definition = self._validate_definition(
            actor,
            definition,
            endpoint_policy=self._endpoint_policy,
        )
        if self._checksum(definition) != record.row.payload_checksum:
            raise AssetValidationFailed(actor.request_id)
        return definition

    @staticmethod
    def _definition_from_record(record: McpVersionRecord) -> McpDefinition:
        return McpDefinition(
            description=record.row.description,
            transport=record.row.transport,
            command=record.row.command,
            args=tuple(record.row.args),
            url=record.row.url,
            env=MappingProxyType(dict(record.row.non_secret_env)),
            headers=MappingProxyType(dict(record.row.non_secret_headers)),
            oauth=MappingProxyType(dict(record.row.oauth_metadata)),
            routing=MappingProxyType(dict(record.row.routing)),
            tool_overrides=MappingProxyType(dict(record.row.tool_overrides)),
            timeout_seconds=record.row.timeout_seconds,
            secret_slots=tuple(
                McpSecretSlot(
                    slot.name,
                    slot.purpose,
                    MappingProxyType({key: tuple(values) for key, values in slot.payload_schema.items()}),
                    slot.required,
                )
                for slot in record.slots
            ),
        )

    @classmethod
    def _version_view(cls, record: McpVersionRecord) -> McpVersionView:
        return McpVersionView(
            id=record.row.id,
            mcp_server_id=record.row.mcp_server_id,
            version_number=record.row.version_number,
            workflow_status=WorkflowStatus(record.row.workflow_status),
            definition=cls._definition_from_record(record),
            secret_slots=tuple(
                McpSecretSlotView(
                    id=slot.id,
                    name=slot.name,
                    purpose=slot.purpose,
                    payload_schema=MappingProxyType({key: tuple(values) for key, values in slot.payload_schema.items()}),
                    required=slot.required,
                )
                for slot in record.slots
            ),
            supersedes_version_id=record.row.supersedes_version_id,
            payload_checksum=record.row.payload_checksum,
            submitted_at=record.row.submitted_at,
            reviewed_at=record.row.reviewed_at,
            reviewed_by_user_id=record.row.reviewed_by_user_id,
            created_by_user_id=record.row.created_by_user_id,
            created_at=record.row.created_at,
        )

    @staticmethod
    def _asset_view(row: McpServerRow) -> McpAssetView:
        return McpAssetView(
            id=row.id,
            scope=AssetScope(row.scope),
            project_id=row.project_id,
            slug=row.slug,
            display_name=row.display_name,
            status=row.status,
            current_published_version_id=row.current_published_version_id,
            version=row.version,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def _record_governance(
        self,
        session: AsyncSession,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
    ) -> None:
        if isinstance(actor, ProjectContext):
            await self._governance_sink.append_project(
                session,
                actor=actor.user_id,
                project_id=actor.project_id,
                asset_id=asset_id,
                version_id=version_id,
                action=action,
                request_id=actor.request_id,
                asset_kind="mcp",
            )
            return
        if not isinstance(actor, SystemAssetGovernanceContext):
            return
        await self._governance_sink.append_override(
            session,
            actor=actor.user_id,
            project_id=actor.project_id,
            asset_id=asset_id,
            version_id=version_id,
            action=action,
            request_id=actor.request_id,
            asset_kind="mcp",
        )
