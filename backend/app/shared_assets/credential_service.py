from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_repository import CredentialRepository
from app.shared_assets.crypto import (
    CredentialEncryptFailed,
    CredentialPayloadInvalid,
    encrypt_credential_payload,
)
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from app.shared_assets.models import AssetScope
from deerflow.persistence.shared_assets import CredentialEnvelopeRow, CredentialRow, CredentialVersionRow

_NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?\Z")
_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,31}\Z")
_PAYLOAD_SECTIONS = frozenset({"env", "headers", "oauth", "query"})
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "uq_credentials_project_name",
        "uq_credentials_system_name",
        "uq_credential_versions_asset_number",
    }
)
_Actor = ProjectContext | SystemAssetGovernanceContext
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


@dataclass(frozen=True)
class CreateCredential:
    name: str
    display_name: str
    credential_type: str


@dataclass(frozen=True)
class CredentialView:
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


@dataclass(frozen=True)
class CredentialVersionView:
    id: uuid.UUID
    credential_id: uuid.UUID
    version_number: int
    status: str
    payload_schema_version: int
    payload_schema: Mapping[str, tuple[str, ...]]
    supersedes_version_id: uuid.UUID | None
    created_by_user_id: str
    created_at: datetime


@dataclass(frozen=True)
class CredentialGrantView:
    id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    credential_slot_id: uuid.UUID
    credential_version_id: uuid.UUID
    status: str
    version: int
    created_by_user_id: str
    created_at: datetime


@dataclass(frozen=True)
class CredentialGrantMigrationView:
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    migrated_count: int


@dataclass(frozen=True)
class CredentialRotationStatus:
    eligible_total: int
    current: int
    pending: int
    status: str


class CredentialService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        keyring: CredentialKeyring | None = None,
        governance_sink: SharedAssetGovernanceEventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._keyring = keyring
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()

    async def create(
        self,
        actor: _Actor,
        command: CreateCredential,
        payload: object,
    ) -> CredentialView:
        command = self._validate_create(actor, command)
        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)
        payload_schema = self._payload_schema(actor, payload)
        scope, project_id = self._scope(actor)
        version_id = uuid.uuid4()
        envelope = self._encrypt(actor, payload, scope, project_id, version_id)

        async def operation(repository: CredentialRepository) -> CredentialView:
            row = CredentialRow(
                scope=scope.value,
                project_id=project_id,
                name=command.name,
                display_name=command.display_name,
                credential_type=command.credential_type,
                created_by_user_id=str(actor.user_id),
            )
            if isinstance(actor, ProjectContext):
                await repository.create_project_credential(actor, row)
            elif project_id is not None:
                await repository.create_override_credential(actor, row)
            else:
                await repository.create_system_credential(actor, row)
            version = CredentialVersionRow(
                id=version_id,
                credential_id=row.id,
                version_number=1,
                status="active",
                payload_schema_version=1,
                payload_schema={key: list(values) for key, values in payload_schema.items()},
                created_by_user_id=str(actor.user_id),
            )
            envelope_row = CredentialEnvelopeRow(
                credential_version_id=version_id,
                envelope_generation=1,
                key_id=envelope.key_id,
                nonce=envelope.nonce,
                ciphertext=envelope.ciphertext,
                is_active=True,
                created_by_user_id=str(actor.user_id),
                activated_at=datetime.now(UTC),
            )
            await repository.add_version(row, version, envelope_row, request_id=actor.request_id)
            row.current_version_id = version.id
            await repository.session.flush()
            return self._credential_view(row)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                result.id,
                result.current_version_id,
                "credential.create",
            ),
        )

    async def replace(
        self,
        actor: _Actor,
        credential_id: uuid.UUID,
        payload: object,
        *,
        expected_credential_version: int,
    ) -> CredentialVersionView:
        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)
        payload_schema = self._payload_schema(actor, payload)
        scope, project_id = self._scope(actor)
        version_id = uuid.uuid4()
        envelope = self._encrypt(actor, payload, scope, project_id, version_id)

        async def operation(repository: CredentialRepository) -> CredentialVersionView:
            credential = await self._get_credential(repository, actor, credential_id, for_update=True)
            self._require_expected_version(actor, credential, expected_credential_version)
            if credential.status != "active":
                raise AssetConflict(actor.request_id)
            previous = await repository.lock_current_version(credential, request_id=actor.request_id)
            if previous.status != "active":
                raise AssetConflict(actor.request_id)
            number = await repository.next_version_number(credential)
            previous.status = "retired"
            previous.retired_at = datetime.now(UTC)
            version = CredentialVersionRow(
                id=version_id,
                credential_id=credential.id,
                version_number=number,
                status="active",
                payload_schema_version=1,
                payload_schema={key: list(values) for key, values in payload_schema.items()},
                supersedes_version_id=previous.id,
                created_by_user_id=str(actor.user_id),
            )
            envelope_row = CredentialEnvelopeRow(
                credential_version_id=version.id,
                envelope_generation=1,
                key_id=envelope.key_id,
                nonce=envelope.nonce,
                ciphertext=envelope.ciphertext,
                is_active=True,
                created_by_user_id=str(actor.user_id),
                activated_at=datetime.now(UTC),
            )
            await repository.add_version(
                credential,
                version,
                envelope_row,
                request_id=actor.request_id,
            )
            credential.current_version_id = version.id
            credential.version += 1
            await repository.session.flush()
            return self._version_view(version)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, credential_id, result.id, "credential.replace"),
        )

    async def revoke(
        self,
        actor: _Actor,
        credential_id: uuid.UUID,
        *,
        expected_credential_version: int,
    ) -> CredentialView:
        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)

        async def operation(repository: CredentialRepository) -> CredentialView:
            credential = await self._get_credential(repository, actor, credential_id, for_update=True)
            self._require_expected_version(actor, credential, expected_credential_version)
            if credential.status == "revoked":
                raise AssetConflict(actor.request_id)
            await self._revoke_runtime_references(repository, actor, credential)
            credential.version += 1
            await repository.session.flush()
            return self._credential_view(credential)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                credential_id,
                result.current_version_id,
                "credential.revoke",
            ),
        )

    async def delete(
        self,
        actor: _Actor,
        credential_id: uuid.UUID,
        *,
        expected_credential_version: int,
    ) -> None:
        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)

        async def operation(repository: CredentialRepository) -> uuid.UUID | None:
            credential = await self._get_credential(
                repository,
                actor,
                credential_id,
                for_update=True,
            )
            self._require_expected_version(
                actor,
                credential,
                expected_credential_version,
            )
            if credential.status == "active":
                await self._revoke_runtime_references(
                    repository,
                    actor,
                    credential,
                )
            elif credential.status != "revoked":
                raise AssetConflict(actor.request_id)
            current_version_id = credential.current_version_id
            await repository.mark_deleted(
                credential,
                request_id=actor.request_id,
            )
            return current_version_id

        await self._execute(
            actor,
            operation,
            governance=lambda session, version_id: self._record_governance(
                session,
                actor,
                credential_id,
                version_id,
                "credential.delete",
            ),
        )

    async def migrate_grants(
        self,
        actor: _Actor,
        credential_id: uuid.UUID,
        *,
        expected_credential_version: int,
    ) -> CredentialGrantMigrationView:
        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)

        async def operation(repository: CredentialRepository) -> CredentialGrantMigrationView:
            credential = await self._get_credential(repository, actor, credential_id, for_update=True)
            self._require_expected_version(actor, credential, expected_credential_version)
            if credential.status != "active":
                raise AssetConflict(actor.request_id)
            current = await repository.lock_current_version(credential, request_id=actor.request_id)
            if current.status != "active":
                raise AssetConflict(actor.request_id)
            active_grants = await repository.lock_active_grants(credential)
            stale_grants = tuple(item for item in active_grants if item.grant.credential_version_id != current.id)
            active_skill_bindings = await repository.lock_active_skill_bindings(credential)
            stale_skill_bindings = tuple(item for item in active_skill_bindings if item.binding.credential_id == credential.id and item.binding.credential_version_id != current.id)
            target_schema = {key: tuple(values) for key, values in current.payload_schema.items()}
            for item in stale_grants:
                if item.mcp_server.scope != credential.scope or item.mcp_server.project_id != credential.project_id:
                    raise AssetValidationFailed(actor.request_id)
                slot_schema = {key: tuple(values) for key, values in item.slot.payload_schema.items()}
                if slot_schema != target_schema:
                    raise AssetValidationFailed(actor.request_id)
            target_env = target_schema.get("env", ())
            for item in stale_skill_bindings:
                if credential.scope != "project" or credential.project_id != item.binding.project_id or item.binding.secret_name not in target_env:
                    raise AssetValidationFailed(actor.request_id)
            migrated_at = datetime.now(UTC)
            await repository.migrate_grants(
                stale_grants,
                current,
                user_id=actor.user_id,
                migrated_at=migrated_at,
            )
            await repository.migrate_skill_bindings(
                active_skill_bindings,
                current,
                credential_id=credential.id,
                user_id=actor.user_id,
                migrated_at=migrated_at,
            )
            return CredentialGrantMigrationView(
                credential_id=credential.id,
                credential_version_id=current.id,
                migrated_count=len(stale_grants) + len(stale_skill_bindings),
            )

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                result.credential_id,
                result.credential_version_id,
                "credential.grants.migrate",
            ),
        )

    async def get(self, actor: _Actor, credential_id: uuid.UUID) -> CredentialView:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: CredentialRepository) -> CredentialView:
            return self._credential_view(await self._get_credential(repository, actor, credential_id))

        return await self._execute(actor, operation)

    async def list_visible(self, actor: _Actor) -> tuple[CredentialView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: CredentialRepository) -> tuple[CredentialView, ...]:
            if isinstance(actor, ProjectContext):
                rows = await repository.list_project_visible(actor)
            elif actor.project_id is not None:
                rows = await repository.list_override_visible(actor)
            else:
                rows = await repository.list_system_visible(actor)
            return tuple(self._credential_view(row) for row in rows)

        return await self._execute(actor, operation)

    async def get_version_history(
        self,
        actor: _Actor,
        credential_id: uuid.UUID,
    ) -> tuple[CredentialVersionView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: CredentialRepository) -> tuple[CredentialVersionView, ...]:
            if isinstance(actor, ProjectContext):
                rows = await repository.get_project_version_history(actor, credential_id)
            elif actor.project_id is not None:
                rows = await repository.get_override_version_history(actor, credential_id)
            else:
                rows = await repository.get_system_version_history(actor, credential_id)
            return tuple(self._version_view(row) for row in rows)

        return await self._execute(actor, operation)

    async def rotation_status(
        self,
        actor: SystemAssetGovernanceContext,
    ) -> CredentialRotationStatus:
        if not isinstance(actor, SystemAssetGovernanceContext) or actor.project_id is not None:
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        try:
            keyring = self._keyring or CredentialKeyring.from_environment()
        except CredentialKeyringInvalid:
            raise AssetStorageUnavailable(actor.request_id) from None

        async def operation(repository: CredentialRepository) -> tuple[int, int]:
            return await repository.rotation_status(
                actor,
                active_key_id=keyring.active_key_id,
            )

        eligible_total, current = await self._execute(actor, operation)
        pending = eligible_total - current
        return CredentialRotationStatus(
            eligible_total=eligible_total,
            current=current,
            pending=pending,
            status="pending" if pending else "current",
        )

    async def _execute(
        self,
        actor: _Actor,
        operation: Callable[[CredentialRepository], Awaitable[_T]],
        governance: Callable[[AsyncSession, _T], Awaitable[None]] | None = None,
    ) -> _T:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await operation(CredentialRepository(session))
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
    async def _get_credential(
        repository: CredentialRepository,
        actor: _Actor,
        credential_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> CredentialRow:
        if isinstance(actor, ProjectContext):
            return await repository.get_project_credential(actor, credential_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.get_override_credential(actor, credential_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_credential(actor, credential_id, for_update=for_update)
        raise AssetForbidden("unknown")

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
    def _validate_create(actor: _Actor, command: CreateCredential) -> CreateCredential:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(command, CreateCredential):
            raise AssetValidationFailed(request_id)
        name = command.name.strip()
        display_name = command.display_name.strip()
        credential_type = command.credential_type.strip()
        if _NAME_PATTERN.fullmatch(name) is None or not display_name or len(display_name) > 120 or _TYPE_PATTERN.fullmatch(credential_type) is None:
            raise AssetValidationFailed(request_id)
        return CreateCredential(name, display_name, credential_type)

    @staticmethod
    def _payload_schema(actor: _Actor, payload: object) -> Mapping[str, tuple[str, ...]]:
        request_id = getattr(actor, "request_id", "unknown")
        try:
            if not isinstance(payload, Mapping) or not payload:
                raise ValueError
            if not set(payload).issubset(_PAYLOAD_SECTIONS):
                raise ValueError
            schema: dict[str, tuple[str, ...]] = {}
            for section, values in payload.items():
                if section not in _PAYLOAD_SECTIONS or not isinstance(values, Mapping) or not values:
                    raise ValueError
                names = tuple(sorted(values))
                if any(not isinstance(name, str) or not name or len(name) > 255 for name in names):
                    raise ValueError
                # Credential fields are write-only text values in the public
                # API. In particular, Skill env bindings are injected into a
                # subprocess environment and therefore cannot safely accept
                # JSON scalars such as null, booleans, or numbers. Reject them
                # at creation/replacement instead of allowing configuration to
                # succeed and failing only when a Worker materializes a Run.
                if any(not isinstance(values[name], str) or not values[name] for name in names):
                    raise ValueError
                schema[section] = names
            return MappingProxyType(schema)
        except (RecursionError, TypeError, ValueError):
            raise AssetValidationFailed(request_id) from None

    def _encrypt(
        self,
        actor: _Actor,
        payload: object,
        scope: AssetScope,
        project_id: uuid.UUID | None,
        version_id: uuid.UUID,
    ):
        try:
            keyring = self._keyring or CredentialKeyring.from_environment()
            return encrypt_credential_payload(payload, scope, project_id, version_id, keyring)
        except CredentialPayloadInvalid:
            raise AssetValidationFailed(actor.request_id) from None
        except (CredentialEncryptFailed, CredentialKeyringInvalid):
            raise AssetStorageUnavailable(actor.request_id) from None

    @staticmethod
    def _require_capability(actor: _Actor, capability: Capability) -> None:
        if isinstance(actor, SystemAssetGovernanceContext):
            return
        if isinstance(actor, ProjectContext) and capability in actor.capabilities:
            return
        raise AssetForbidden(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _require_expected_version(actor: _Actor, row: CredentialRow, expected: int) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool) or row.version != expected:
            raise AssetConflict(actor.request_id)

    @staticmethod
    async def _revoke_runtime_references(
        repository: CredentialRepository,
        actor: _Actor,
        credential: CredentialRow,
    ) -> None:
        now = datetime.now(UTC)
        versions = await repository.lock_all_versions(credential)
        active_grants = await repository.lock_active_grants(credential)
        active_skill_bindings = await repository.lock_active_skill_bindings(credential)
        for version in versions:
            if version.status == "revoked":
                continue
            version.status = "revoked"
            version.revoked_at = now
            version.revoked_by_user_id = str(actor.user_id)
        await repository.revoke_grants(
            tuple(item.grant for item in active_grants),
            user_id=actor.user_id,
            revoked_at=now,
        )
        await repository.revoke_skill_bindings(
            active_skill_bindings,
            credential_id=credential.id,
            user_id=actor.user_id,
            revoked_at=now,
        )
        credential.status = "revoked"
        credential.revoked_at = now
        credential.revoked_by_user_id = str(actor.user_id)

    @staticmethod
    def _credential_view(row: CredentialRow) -> CredentialView:
        return CredentialView(
            id=row.id,
            scope=AssetScope(row.scope),
            project_id=row.project_id,
            name=row.name,
            display_name=row.display_name,
            credential_type=row.credential_type,
            status=row.status,
            current_version_id=row.current_version_id,
            version=row.version,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _version_view(row: CredentialVersionRow) -> CredentialVersionView:
        schema = MappingProxyType({key: tuple(values) for key, values in row.payload_schema.items()})
        return CredentialVersionView(
            id=row.id,
            credential_id=row.credential_id,
            version_number=row.version_number,
            status=row.status,
            payload_schema_version=row.payload_schema_version,
            payload_schema=schema,
            supersedes_version_id=row.supersedes_version_id,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
        )

    async def _record_governance(
        self,
        session: AsyncSession,
        actor: _Actor,
        credential_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
    ) -> None:
        if isinstance(actor, ProjectContext):
            await self._governance_sink.append_project(
                session,
                actor=actor.user_id,
                project_id=actor.project_id,
                asset_id=credential_id,
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
            asset_id=credential_id,
            version_id=version_id,
            action=action,
            request_id=actor.request_id,
            asset_kind="mcp",
        )
