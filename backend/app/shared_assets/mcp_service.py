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
from typing import TypeVar
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.credential_repository import CredentialRepository, LockedCredentialVersion
from app.shared_assets.credential_service import CredentialGrantView
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.mcp_repository import GrantState, McpRepository, McpVersionRecord
from app.shared_assets.models import AssetScope, WorkflowStatus
from deerflow.persistence.shared_assets import (
    CredentialVersionRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
)

_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SLOT_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,62}\Z")
_TRANSPORTS = frozenset({"stdio", "sse", "http", "streamable_http"})
_SCHEMA_SECTIONS = frozenset({"env", "headers", "oauth"})
_OAUTH_FIELDS = frozenset(
    {
        "enabled",
        "token_url",
        "grant_type",
        "client_id",
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
        "uq_credential_grants_active_slot",
    }
)
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
class McpCredentialSlot:
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
    credential_slots: tuple[McpCredentialSlot, ...] = ()


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
class McpCredentialSlotView:
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
    credential_slots: tuple[McpCredentialSlotView, ...]
    credential_grants: tuple[CredentialGrantView, ...]
    supersedes_version_id: uuid.UUID | None
    payload_checksum: str
    submitted_at: datetime | None
    reviewed_at: datetime | None
    reviewed_by_user_id: str | None
    created_by_user_id: str
    created_at: datetime


class McpService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        governance_sink: SharedAssetGovernanceEventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()

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

    async def create_version(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        definition: McpDefinition,
        *,
        expected_asset_version: int,
    ) -> McpVersionView:
        definition = self._validate_definition(actor, definition)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: McpRepository) -> McpVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
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
                McpCredentialSlotRow(
                    mcp_server_version_id=version_id,
                    name=slot.name,
                    purpose=slot.purpose,
                    payload_schema={key: list(values) for key, values in slot.payload_schema.items()},
                    required=slot.required,
                )
                for slot in definition.credential_slots
            )
            record = await repository.add_version(
                asset,
                version,
                slots,
                request_id=actor.request_id,
            )
            asset.version += 1
            await repository.session.flush()
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
            if asset.status != "active" or record.row.workflow_status != WorkflowStatus.DRAFT.value or not record.slots:
                raise AssetConflict(actor.request_id)
            record.row.workflow_status = WorkflowStatus.PENDING_APPROVAL.value
            record.row.submitted_at = datetime.now(UTC)
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, asset_id, result.id, "mcp.submit_approval"),
        )

    async def approve(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        credential_versions: Mapping[str, uuid.UUID],
        *,
        expected_asset_version: int,
    ) -> McpVersionView:
        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)
        credential_versions = self._validate_credential_bindings(actor, credential_versions)

        async def operation(repository: McpRepository) -> McpVersionView:
            credentials = CredentialRepository(repository.session)
            await self._lock_project_first(repository, actor)
            asset = await self._get_asset(
                repository,
                actor,
                asset_id,
                for_update=True,
                lock_project=False,
            )
            self._require_expected_version(actor, asset, expected_asset_version)
            record = await self._get_version(repository, actor, asset_id, version_id, for_update=True)
            if asset.status != "active" or not record.slots:
                raise AssetConflict(actor.request_id)
            expected_status = {WorkflowStatus.DRAFT.value, WorkflowStatus.PENDING_APPROVAL.value} if isinstance(actor, SystemAssetGovernanceContext) else {WorkflowStatus.PENDING_APPROVAL.value}
            if record.row.workflow_status not in expected_status:
                raise AssetConflict(actor.request_id)
            slots_by_name = {slot.name: slot for slot in record.slots}
            if set(credential_versions).difference(slots_by_name) or any(slot.required and slot.name not in credential_versions for slot in record.slots):
                raise AssetValidationFailed(actor.request_id)
            try:
                locked_versions = await self._lock_credential_versions(
                    credentials,
                    actor,
                    tuple(credential_versions.values()),
                )
            except AssetNotFound:
                raise AssetValidationFailed(actor.request_id) from None
            bindings: list[tuple[McpCredentialSlotRow, CredentialVersionRow]] = []
            for slot in record.slots:
                credential_version_id = credential_versions.get(slot.name)
                if credential_version_id is None:
                    continue
                locked = locked_versions.get(credential_version_id)
                if locked is None:
                    raise AssetValidationFailed(actor.request_id)
                self._validate_slot_credential(actor, asset, slot, locked)
                bindings.append((slot, locked.version))
            grants = await repository.create_grants(
                record.row,
                bindings,
                user_id=actor.user_id,
                request_id=actor.request_id,
            )
            record.row.workflow_status = WorkflowStatus.PUBLISHED.value
            record.row.reviewed_at = datetime.now(UTC)
            record.row.reviewed_by_user_id = str(actor.user_id)
            asset.current_published_version_id = record.row.id
            asset.version += 1
            await repository.session.flush()
            return self._version_view(McpVersionRecord(record.row, record.slots, grants))

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, asset_id, result.id, "mcp.approve"),
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
            if asset.status != "active" or record.row.workflow_status != WorkflowStatus.DRAFT.value or record.slots:
                raise AssetConflict(actor.request_id)
            if self._checksum(self._definition_from_record(record)) != record.row.payload_checksum:
                raise AssetValidationFailed(actor.request_id)
            record.row.workflow_status = WorkflowStatus.PUBLISHED.value
            asset.current_published_version_id = record.row.id
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, asset_id, result.id, "mcp.publish"),
        )

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

    async def suspend(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> McpAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)
        return await self._change_status(
            actor,
            asset_id,
            expected_asset_version,
            "suspended",
            "mcp.suspend",
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

    async def grant_is_usable(self, actor: _Actor, grant_id: uuid.UUID) -> bool:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: McpRepository) -> bool:
            if isinstance(actor, ProjectContext):
                state = await repository.project_grant_state(actor, grant_id)
            elif actor.project_id is not None:
                state = await repository.override_grant_state(actor, grant_id)
            else:
                state = await repository.system_grant_state(actor, grant_id)
            return self._grant_state_usable(state)

        return await self._execute(actor, operation)

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
    async def _lock_credential_version(
        repository: CredentialRepository,
        actor: _Actor,
        credential_version_id: uuid.UUID,
    ) -> LockedCredentialVersion:
        if isinstance(actor, ProjectContext):
            return await repository.lock_project_credential_version(actor, credential_version_id)
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.lock_override_credential_version(actor, credential_version_id)
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.lock_system_credential_version(actor, credential_version_id)
        raise AssetForbidden("unknown")

    @staticmethod
    async def _lock_credential_versions(
        repository: CredentialRepository,
        actor: _Actor,
        credential_version_ids: tuple[uuid.UUID, ...],
    ) -> dict[uuid.UUID, LockedCredentialVersion]:
        if isinstance(actor, ProjectContext):
            return await repository.lock_project_credential_versions(
                actor,
                credential_version_ids,
            )
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.lock_override_credential_versions(
                actor,
                credential_version_ids,
            )
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.lock_system_credential_versions(
                actor,
                credential_version_ids,
            )
        raise AssetForbidden("unknown")

    @staticmethod
    def _validate_slot_credential(
        actor: _Actor,
        asset: McpServerRow,
        slot: McpCredentialSlotRow,
        locked: LockedCredentialVersion,
    ) -> None:
        credential = locked.credential
        version = locked.version
        if credential.scope != asset.scope or credential.project_id != asset.project_id or credential.status != "active" or version.status != "active":
            raise AssetValidationFailed(actor.request_id)
        expected_schema = {key: tuple(values) for key, values in version.payload_schema.items()}
        slot_schema = {key: tuple(values) for key, values in slot.payload_schema.items()}
        if slot_schema != expected_schema:
            raise AssetValidationFailed(actor.request_id)

    @staticmethod
    def _validate_credential_bindings(
        actor: _Actor,
        credential_versions: object,
    ) -> Mapping[str, uuid.UUID]:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(credential_versions, Mapping):
            raise AssetValidationFailed(request_id)
        normalized: dict[str, uuid.UUID] = {}
        for slot_name, credential_version_id in credential_versions.items():
            if not isinstance(slot_name, str) or _SLOT_PATTERN.fullmatch(slot_name) is None or not isinstance(credential_version_id, uuid.UUID):
                raise AssetValidationFailed(request_id)
            normalized[slot_name] = credential_version_id
        return MappingProxyType(normalized)

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
    def _validate_definition(cls, actor: _Actor, definition: McpDefinition) -> McpDefinition:
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
            slots = tuple(cls._validate_slot(slot) for slot in definition.credential_slots)
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
            if transport == "stdio":
                if not command or url is not None:
                    raise ValueError
            elif not url or command is not None or args:
                raise ValueError
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
                credential_slots=slots,
            )
        except (AttributeError, RecursionError, TypeError, ValueError):
            raise AssetValidationFailed(request_id) from None

    @classmethod
    def _validate_slot(cls, slot: McpCredentialSlot) -> McpCredentialSlot:
        if not isinstance(slot, McpCredentialSlot):
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
        return McpCredentialSlot(name, purpose, MappingProxyType(normalized), slot.required)

    @classmethod
    def _copy_json_mapping(cls, value: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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
            return
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
    def _grant_state_usable(state: GrantState) -> bool:
        return (
            state.grant.status == "active"
            and state.mcp_status != "suspended"
            and state.mcp_workflow_status == WorkflowStatus.PUBLISHED.value
            and state.credential_status == "active"
            and state.credential_version_status in {"active", "retired"}
        )

    @staticmethod
    def _checksum(definition: McpDefinition) -> str:
        canonical = {
            "args": list(definition.args),
            "command": definition.command,
            "credential_slots": [
                {
                    "name": slot.name,
                    "payload_schema": {key: list(values) for key, values in slot.payload_schema.items()},
                    "purpose": slot.purpose,
                    "required": slot.required,
                }
                for slot in definition.credential_slots
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
            credential_slots=tuple(
                McpCredentialSlot(
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
            credential_slots=tuple(
                McpCredentialSlotView(
                    id=slot.id,
                    name=slot.name,
                    purpose=slot.purpose,
                    payload_schema=MappingProxyType({key: tuple(values) for key, values in slot.payload_schema.items()}),
                    required=slot.required,
                )
                for slot in record.slots
            ),
            credential_grants=tuple(cls._grant_view(grant) for grant in record.grants),
            supersedes_version_id=record.row.supersedes_version_id,
            payload_checksum=record.row.payload_checksum,
            submitted_at=record.row.submitted_at,
            reviewed_at=record.row.reviewed_at,
            reviewed_by_user_id=record.row.reviewed_by_user_id,
            created_by_user_id=record.row.created_by_user_id,
            created_at=record.row.created_at,
        )

    @staticmethod
    def _grant_view(row) -> CredentialGrantView:
        return CredentialGrantView(
            id=row.id,
            mcp_server_version_id=row.mcp_server_version_id,
            credential_slot_id=row.credential_slot_id,
            credential_version_id=row.credential_version_id,
            status=row.status,
            version=row.version,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
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
