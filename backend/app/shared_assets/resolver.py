from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.binding_repository import BindingRepository
from app.shared_assets.catalog_state_repository import CatalogStateRepository
from app.shared_assets.credential_closure import (
    McpCredentialClosureInvalid,
    lock_mcp_credential_closure,
)
from app.shared_assets.crypto import (
    CredentialDecryptFailed,
    EncryptedEnvelope,
    decrypt_credential_payload,
)
from app.shared_assets.errors import (
    AssetForbidden,
    AssetNotFound,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from app.shared_assets.mcp_repository import McpVersionRecord
from app.shared_assets.mcp_service import McpService
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedAssetSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
)
from app.shared_assets.skill_repository import SkillVersionRecord
from app.shared_assets.skill_service import SkillService
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)


@dataclass(frozen=True)
class MaterializedMcpSecrets:
    """Short-lived MCP plaintext; the secret mapping is intentionally repr-hidden."""

    mcp_version_id: uuid.UUID
    by_slot: Mapping[str, Mapping[str, object]] = field(repr=False)


@dataclass(frozen=True)
class _ResolvedRecord:
    scope: AssetScope
    asset: AgentRow | SkillRow | McpServerRow
    version: AgentVersionRow | SkillVersionRow | McpServerVersionRow


@dataclass(frozen=True)
class _GrantMaterial:
    slot: McpCredentialSlotRow
    grant: CredentialGrantRow
    credential: CredentialRow
    version: CredentialVersionRow
    envelope: CredentialEnvelopeRow


_ASSET_TYPES = {
    AssetKind.AGENT: (AgentRow, AgentVersionRow, "agent_id"),
    AssetKind.SKILL: (SkillRow, SkillVersionRow, "skill_id"),
    AssetKind.MCP: (McpServerRow, McpServerVersionRow, "mcp_server_id"),
}
_BINDING_TYPES = {
    AssetKind.AGENT: (
        ProjectSystemAgentBindingRow,
        "system_agent_id",
        "agent_version_id",
    ),
    AssetKind.SKILL: (
        ProjectSystemSkillBindingRow,
        "system_skill_id",
        "skill_version_id",
    ),
    AssetKind.MCP: (
        ProjectSystemMcpBindingRow,
        "system_mcp_server_id",
        "mcp_server_version_id",
    ),
}


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


class ProjectAssetResolver:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        keyring: CredentialKeyring | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._keyring = keyring

    async def resolve_project_asset_snapshot(
        self,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> ResolvedAssetSnapshot:
        self._validate_resolve_input(context, selection)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = BindingRepository(session)
                    await repository.lock_project(context, read=True)
                    record = await self._resolve_record(session, repository, context, selection)
                    snapshot = await self._snapshot(
                        session,
                        context,
                        selection.kind,
                        record,
                        0,
                    )
                    generation = await CatalogStateRepository(session).read_generation()
                    return replace(snapshot, catalog_generation=generation)
        except (AssetForbidden, AssetValidationFailed):
            raise
        except AssetResolutionUnavailable:
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(context.request_id) from None

    async def materialize_mcp_secrets(
        self,
        context: ProjectContext,
        resolved: ResolvedMcpSnapshot,
    ) -> MaterializedMcpSecrets:
        request_id = getattr(context, "request_id", "unknown")
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(request_id)
        if Capability.SHARED_ASSETS_READ not in context.capabilities:
            raise AssetForbidden(request_id)
        if (
            type(resolved) is not ResolvedMcpSnapshot
            or resolved.kind is not AssetKind.MCP
            or not isinstance(resolved.scope, AssetScope)
            or not isinstance(resolved.asset_id, uuid.UUID)
            or not isinstance(resolved.version_id, uuid.UUID)
            or not isinstance(resolved.checksum, str)
            or not isinstance(resolved.definition, Mapping)
            or any(not isinstance(grant_id, uuid.UUID) for grant_id in resolved.credential_grant_ids)
            or len(set(resolved.credential_grant_ids)) != len(resolved.credential_grant_ids)
        ):
            raise AssetValidationFailed(request_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await self._materialize(
                        session,
                        context,
                        resolved,
                        request_id,
                    )
        except (AssetValidationFailed, AssetResolutionUnavailable):
            raise
        except AssetNotFound:
            raise AssetResolutionUnavailable(request_id) from None
        except (CredentialDecryptFailed, CredentialKeyringInvalid):
            raise AssetStorageUnavailable(request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(request_id) from None

    @staticmethod
    def _validate_resolve_input(
        context: ProjectContext,
        selection: AssetSelection,
    ) -> None:
        request_id = getattr(context, "request_id", "unknown")
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(request_id)
        if Capability.SHARED_ASSETS_READ not in context.capabilities:
            raise AssetForbidden(request_id)
        if not isinstance(selection, AssetSelection) or not isinstance(selection.kind, AssetKind) or not isinstance(selection.asset_id, uuid.UUID) or (selection.version_id is not None and not isinstance(selection.version_id, uuid.UUID)):
            raise AssetValidationFailed(request_id)

    async def _resolve_record(
        self,
        session: AsyncSession,
        repository: BindingRepository,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> _ResolvedRecord:
        asset_type, version_type, parent_column = _ASSET_TYPES[selection.kind]
        project_statement = (
            select(asset_type)
            .where(
                asset_type.id == selection.asset_id,
                asset_type.scope == "project",
                asset_type.project_id == context.project_id,
            )
            .with_for_update(read=True, of=asset_type)
        )
        asset = (await session.execute(project_statement)).scalar_one_or_none()
        if asset is not None:
            version_id = asset.current_published_version_id
            if version_id is None or (selection.version_id is not None and selection.version_id != version_id):
                raise AssetResolutionUnavailable(context.request_id)
            version = await self._lock_version(
                session,
                version_type,
                parent_column,
                asset.id,
                version_id,
                context.request_id,
            )
            self._assert_asset_state(asset, version, context.request_id)
            return _ResolvedRecord(AssetScope.PROJECT, asset, version)

        binding = await repository.get_binding(
            context,
            selection.kind,
            selection.asset_id,
            for_update=True,
            read=True,
            required=False,
        )
        if binding is None or not binding.enabled:
            raise AssetResolutionUnavailable(context.request_id)
        _binding_type, _asset_column, version_column = _BINDING_TYPES[selection.kind]
        pinned_version_id = getattr(binding, version_column)
        if selection.version_id is not None and selection.version_id != pinned_version_id:
            raise AssetResolutionUnavailable(context.request_id)
        try:
            target = await repository.lock_target(
                context,
                AssetSelection(selection.kind, selection.asset_id, pinned_version_id),
                allow_archived=True,
                read=True,
            )
        except SharedAssetError:
            raise AssetResolutionUnavailable(context.request_id) from None
        self._assert_asset_state(target.asset, target.version, context.request_id)
        return _ResolvedRecord(AssetScope.SYSTEM, target.asset, target.version)

    @staticmethod
    async def _lock_version(
        session: AsyncSession,
        version_type,
        parent_column: str,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        request_id: str,
    ):
        statement = (
            select(version_type)
            .where(
                version_type.id == version_id,
                getattr(version_type, parent_column) == asset_id,
            )
            .with_for_update(read=True, of=version_type)
        )
        version = (await session.execute(statement)).scalar_one_or_none()
        if version is None:
            raise AssetResolutionUnavailable(request_id)
        return version

    @staticmethod
    def _assert_asset_state(asset, version, request_id: str) -> None:
        if asset.status == "suspended" or version.workflow_status != "published":
            raise AssetResolutionUnavailable(request_id)

    async def _snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        kind: AssetKind,
        record: _ResolvedRecord,
        generation: int,
    ) -> ResolvedAssetSnapshot:
        if kind is AssetKind.AGENT:
            return await self._agent_snapshot(session, context, record, generation)
        if kind is AssetKind.SKILL:
            return await self._skill_snapshot(session, context, record, generation)
        return await self._mcp_snapshot(session, context, record, generation)

    async def _agent_snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        record: _ResolvedRecord,
        generation: int,
    ) -> ResolvedAgentSnapshot:
        version = record.version
        if not isinstance(version, AgentVersionRow):
            raise AssetResolutionUnavailable(context.request_id)
        skill_ids = tuple(
            (
                await session.execute(
                    select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == version.id).order_by(AgentVersionSkillRefRow.sort_order).with_for_update(read=True, of=AgentVersionSkillRefRow)
                )
            )
            .scalars()
            .all()
        )
        mcp_ids = tuple(
            (
                await session.execute(
                    select(AgentVersionMcpRefRow.mcp_server_version_id).where(AgentVersionMcpRefRow.agent_version_id == version.id).order_by(AgentVersionMcpRefRow.sort_order).with_for_update(read=True, of=AgentVersionMcpRefRow)
                )
            )
            .scalars()
            .all()
        )
        await self._assert_exact_dependencies(
            session,
            context,
            AssetKind.SKILL,
            skill_ids,
        )
        await self._assert_exact_dependencies(
            session,
            context,
            AssetKind.MCP,
            mcp_ids,
        )
        for mcp_version_id in mcp_ids:
            mcp_record = await self._mcp_record_for_version(
                session,
                context,
                mcp_version_id,
            )
            await self._usable_grant_ids(session, mcp_record, context.request_id)
        dependencies = tuple((*skill_ids, *mcp_ids))
        return ResolvedAgentSnapshot(
            kind=AssetKind.AGENT,
            scope=record.scope,
            asset_id=record.asset.id,
            version_id=version.id,
            checksum=version.payload_checksum,
            catalog_generation=generation,
            dependency_version_ids=dependencies,
            payload=AgentPayload(
                description=version.description,
                soul=version.soul,
                model_ref=version.model_ref,
                tool_groups=tuple(version.tool_groups),
                skill_version_ids=skill_ids,
                mcp_version_ids=mcp_ids,
            ),
        )

    async def _skill_snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        record: _ResolvedRecord,
        generation: int,
    ) -> ResolvedSkillSnapshot:
        version = record.version
        if not isinstance(version, SkillVersionRow):
            raise AssetResolutionUnavailable(context.request_id)
        rows = tuple((await session.execute(select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == version.id).order_by(SkillVersionFileRow.path).with_for_update(read=True, of=SkillVersionFileRow))).scalars().all())
        skill_record = SkillVersionRecord(version, rows)
        try:
            files = await asyncio.to_thread(
                SkillService._verified_archive_files,
                skill_record,
                context.request_id,
            )
        except AssetValidationFailed:
            raise AssetResolutionUnavailable(context.request_id) from None
        requirements: list[str] = []
        for item in version.secret_requirements:
            if isinstance(item, str):
                requirements.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
                requirements.append(str(item["name"]))
            else:
                raise AssetResolutionUnavailable(context.request_id)
        return ResolvedSkillSnapshot(
            kind=AssetKind.SKILL,
            scope=record.scope,
            asset_id=record.asset.id,
            version_id=version.id,
            checksum=version.payload_checksum,
            catalog_generation=generation,
            dependency_version_ids=(),
            files=files,
            secret_requirements=tuple(requirements),
        )

    async def _mcp_snapshot(
        self,
        session: AsyncSession,
        context: ProjectContext,
        record: _ResolvedRecord,
        generation: int,
    ) -> ResolvedMcpSnapshot:
        if not isinstance(record.version, McpServerVersionRow):
            raise AssetResolutionUnavailable(context.request_id)
        slots = tuple(
            (
                await session.execute(
                    select(McpCredentialSlotRow).where(McpCredentialSlotRow.mcp_server_version_id == record.version.id).order_by(McpCredentialSlotRow.name, McpCredentialSlotRow.id).with_for_update(read=True, of=McpCredentialSlotRow)
                )
            )
            .scalars()
            .all()
        )
        mcp_record = McpVersionRecord(record.version, slots, ())
        grant_ids = await self._usable_grant_ids(session, record, context.request_id)
        safe_definition = self._safe_mcp_definition(
            mcp_record,
            context.request_id,
        )
        return ResolvedMcpSnapshot(
            kind=AssetKind.MCP,
            scope=record.scope,
            asset_id=record.asset.id,
            version_id=record.version.id,
            checksum=record.version.payload_checksum,
            catalog_generation=generation,
            dependency_version_ids=(),
            definition=safe_definition,
            credential_grant_ids=grant_ids,
        )

    @staticmethod
    def _safe_mcp_definition(
        record: McpVersionRecord,
        request_id: str,
    ) -> Mapping[str, object]:
        definition = McpService._definition_from_record(record)
        safe_definition = _freeze(
            {
                "description": definition.description,
                "transport": definition.transport,
                "command": definition.command,
                "args": definition.args,
                "url": definition.url,
                "env": definition.env,
                "headers": definition.headers,
                "oauth": definition.oauth,
                "routing": definition.routing,
                "tool_overrides": definition.tool_overrides,
                "timeout_seconds": definition.timeout_seconds,
                "credential_slots": tuple(
                    {
                        "name": slot.name,
                        "purpose": slot.purpose,
                        "payload_schema": slot.payload_schema,
                        "required": slot.required,
                    }
                    for slot in definition.credential_slots
                ),
            }
        )
        if not isinstance(safe_definition, Mapping):
            raise AssetResolutionUnavailable(request_id)
        return safe_definition

    async def _assert_exact_dependencies(
        self,
        session: AsyncSession,
        context: ProjectContext,
        kind: AssetKind,
        version_ids: Sequence[uuid.UUID],
    ) -> None:
        for version_id in sorted(
            {uuid.UUID(str(value)) for value in version_ids},
            key=lambda value: value.int,
        ):
            await self._dependency_record(session, context, kind, version_id)

    async def _dependency_record(
        self,
        session: AsyncSession,
        context: ProjectContext,
        kind: AssetKind,
        version_id: uuid.UUID,
    ) -> _ResolvedRecord:
        asset_type, version_type, parent_column = _ASSET_TYPES[kind]
        parent_id = (await session.execute(select(getattr(version_type, parent_column)).where(version_type.id == version_id))).scalar_one_or_none()
        project_statement = (
            select(asset_type)
            .where(
                asset_type.id == parent_id,
                asset_type.scope == "project",
                asset_type.project_id == context.project_id,
                asset_type.status != "suspended",
            )
            .with_for_update(read=True, of=asset_type)
        )
        project_asset = (await session.execute(project_statement)).scalar_one_or_none()
        if project_asset is not None:
            project_version = await self._lock_version(
                session,
                version_type,
                parent_column,
                uuid.UUID(str(project_asset.id)),
                version_id,
                context.request_id,
            )
            self._assert_asset_state(
                project_asset,
                project_version,
                context.request_id,
            )
            return _ResolvedRecord(
                AssetScope.PROJECT,
                project_asset,
                project_version,
            )
        binding_type, asset_column, binding_version_column = _BINDING_TYPES[kind]
        binding_statement = (
            select(binding_type)
            .where(
                binding_type.project_id == context.project_id,
                getattr(binding_type, binding_version_column) == version_id,
                binding_type.enabled.is_(True),
            )
            .with_for_update(read=True, of=binding_type)
        )
        binding = (await session.execute(binding_statement)).scalar_one_or_none()
        if binding is None:
            raise AssetResolutionUnavailable(context.request_id)
        asset_id = uuid.UUID(str(getattr(binding, asset_column)))
        asset_statement = (
            select(asset_type)
            .where(
                asset_type.id == asset_id,
                asset_type.scope == "system",
                asset_type.project_id.is_(None),
                asset_type.status != "suspended",
            )
            .with_for_update(read=True, of=asset_type)
        )
        system_asset = (await session.execute(asset_statement)).scalar_one_or_none()
        if system_asset is None:
            raise AssetResolutionUnavailable(context.request_id)
        system_version = await self._lock_version(
            session,
            version_type,
            parent_column,
            asset_id,
            version_id,
            context.request_id,
        )
        self._assert_asset_state(
            system_asset,
            system_version,
            context.request_id,
        )
        return _ResolvedRecord(AssetScope.SYSTEM, system_asset, system_version)

    async def _mcp_record_for_version(
        self,
        session: AsyncSession,
        context: ProjectContext,
        version_id: uuid.UUID,
    ) -> _ResolvedRecord:
        return await self._dependency_record(
            session,
            context,
            AssetKind.MCP,
            version_id,
        )

    async def _usable_grant_ids(
        self,
        session: AsyncSession,
        record: _ResolvedRecord,
        request_id: str,
    ) -> tuple[uuid.UUID, ...]:
        if not isinstance(record.version, McpServerVersionRow):
            raise AssetResolutionUnavailable(request_id)
        project_id = uuid.UUID(str(record.asset.project_id)) if record.scope is AssetScope.PROJECT and record.asset.project_id is not None else None
        try:
            closure = await lock_mcp_credential_closure(
                session,
                uuid.UUID(str(record.version.id)),
                scope=record.scope,
                project_id=project_id,
            )
        except McpCredentialClosureInvalid:
            raise AssetResolutionUnavailable(request_id) from None
        return closure.grant_ids

    async def _materialize(
        self,
        session: AsyncSession,
        context: ProjectContext,
        resolved: ResolvedMcpSnapshot,
        request_id: str,
    ) -> MaterializedMcpSecrets:
        repository = BindingRepository(session)
        await repository.lock_project(context, read=True)
        scope = resolved.scope
        project_id = uuid.UUID(str(context.project_id)) if scope is AssetScope.PROJECT else None
        if scope is AssetScope.SYSTEM:
            binding = await repository.get_binding(
                context,
                AssetKind.MCP,
                resolved.asset_id,
                for_update=True,
                read=True,
                required=False,
            )
            if binding is None or not binding.enabled or binding.mcp_server_version_id != resolved.version_id:
                raise AssetResolutionUnavailable(request_id)
        asset_filters = [
            McpServerRow.id == resolved.asset_id,
            McpServerRow.scope == scope.value,
        ]
        if scope is AssetScope.PROJECT:
            asset_filters.append(McpServerRow.project_id == context.project_id)
        else:
            asset_filters.append(McpServerRow.project_id.is_(None))
        asset_statement = select(McpServerRow).where(*asset_filters).with_for_update(read=True, of=McpServerRow)
        asset = (await session.execute(asset_statement)).scalar_one_or_none()
        version_statement = (
            select(McpServerVersionRow)
            .where(
                McpServerVersionRow.id == resolved.version_id,
                McpServerVersionRow.mcp_server_id == resolved.asset_id,
            )
            .with_for_update(read=True, of=McpServerVersionRow)
        )
        version = (await session.execute(version_statement)).scalar_one_or_none()
        if asset is None or version is None or asset.status == "suspended" or version.workflow_status != "published" or version.payload_checksum != resolved.checksum:
            raise AssetResolutionUnavailable(request_id)

        slots = tuple(
            (
                await session.execute(
                    select(McpCredentialSlotRow).where(McpCredentialSlotRow.mcp_server_version_id == version.id).order_by(McpCredentialSlotRow.name, McpCredentialSlotRow.id).with_for_update(read=True, of=McpCredentialSlotRow)
                )
            )
            .scalars()
            .all()
        )
        locked_definition = self._safe_mcp_definition(
            McpVersionRecord(version, slots, ()),
            request_id,
        )
        if locked_definition != resolved.definition:
            raise AssetResolutionUnavailable(request_id)
        slots_by_id = {slot.id: slot for slot in slots}
        references = (
            await session.execute(
                select(
                    CredentialGrantRow.id,
                    CredentialGrantRow.credential_slot_id,
                    CredentialGrantRow.credential_version_id,
                    CredentialVersionRow.credential_id,
                )
                .join(
                    CredentialVersionRow,
                    CredentialVersionRow.id == CredentialGrantRow.credential_version_id,
                )
                .where(
                    CredentialGrantRow.mcp_server_version_id == version.id,
                    CredentialGrantRow.status == "active",
                )
            )
        ).all()
        reference_by_grant = {row.id: row for row in references}
        if set(reference_by_grant) != set(resolved.credential_grant_ids):
            raise AssetResolutionUnavailable(request_id)
        credential_ids = sorted(
            {row.credential_id for row in references},
            key=lambda value: value.int,
        )
        credentials: dict[uuid.UUID, CredentialRow] = {}
        for credential_id in credential_ids:
            statement = select(CredentialRow).where(CredentialRow.id == credential_id).with_for_update(read=True, of=CredentialRow)
            credential = (await session.execute(statement)).scalar_one_or_none()
            if credential is None:
                raise AssetResolutionUnavailable(request_id)
            credentials[credential_id] = credential
        versions: dict[uuid.UUID, CredentialVersionRow] = {}
        for version_id in sorted(
            {row.credential_version_id for row in references},
            key=lambda value: value.int,
        ):
            statement = select(CredentialVersionRow).where(CredentialVersionRow.id == version_id).with_for_update(read=True, of=CredentialVersionRow)
            credential_version = (await session.execute(statement)).scalar_one_or_none()
            if credential_version is None:
                raise AssetResolutionUnavailable(request_id)
            versions[version_id] = credential_version
        envelopes: dict[uuid.UUID, CredentialEnvelopeRow] = {}
        for version_id in sorted(versions, key=lambda value: value.int):
            statement = (
                select(CredentialEnvelopeRow)
                .where(
                    CredentialEnvelopeRow.credential_version_id == version_id,
                    CredentialEnvelopeRow.is_active.is_(True),
                )
                .with_for_update(read=True, of=CredentialEnvelopeRow)
            )
            envelope = (await session.execute(statement)).scalar_one_or_none()
            if envelope is None:
                raise AssetResolutionUnavailable(request_id)
            envelopes[version_id] = envelope
        grants: dict[uuid.UUID, CredentialGrantRow] = {}
        for grant_id in sorted(reference_by_grant, key=lambda value: value.int):
            statement = (
                select(CredentialGrantRow)
                .where(
                    CredentialGrantRow.id == grant_id,
                    CredentialGrantRow.mcp_server_version_id == version.id,
                )
                .with_for_update(read=True, of=CredentialGrantRow)
            )
            grant = (await session.execute(statement)).scalar_one_or_none()
            if grant is None:
                raise AssetResolutionUnavailable(request_id)
            grants[grant_id] = grant

        materials: list[_GrantMaterial] = []
        granted_slot_ids: set[uuid.UUID] = set()
        for grant_id in resolved.credential_grant_ids:
            reference = reference_by_grant[grant_id]
            slot = slots_by_id.get(reference.credential_slot_id)
            grant = grants[grant_id]
            credential_version = versions[reference.credential_version_id]
            credential = credentials[credential_version.credential_id]
            envelope = envelopes[credential_version.id]
            scope_matches = credential.scope == scope.value and ((scope is AssetScope.SYSTEM and credential.project_id is None) or (scope is AssetScope.PROJECT and credential.project_id == project_id))
            expected_schema = {key: sorted(values) for key, values in slot.payload_schema.items()} if slot is not None else None
            actual_schema = {key: sorted(values) for key, values in credential_version.payload_schema.items()}
            if slot is None or grant.status != "active" or credential.status != "active" or credential_version.status not in {"active", "retired"} or not scope_matches or expected_schema != actual_schema:
                raise AssetResolutionUnavailable(request_id)
            granted_slot_ids.add(slot.id)
            materials.append(_GrantMaterial(slot, grant, credential, credential_version, envelope))
        if any(slot.required and slot.id not in granted_slot_ids for slot in slots):
            raise AssetResolutionUnavailable(request_id)

        try:
            keyring = self._keyring or CredentialKeyring.from_environment()
        except CredentialKeyringInvalid:
            raise
        by_slot: dict[str, Mapping[str, object]] = {}
        for material in materials:
            encrypted = EncryptedEnvelope(
                key_id=material.envelope.key_id,
                nonce=bytes(material.envelope.nonce),
                ciphertext=bytes(material.envelope.ciphertext),
            )
            payload = await asyncio.to_thread(
                decrypt_credential_payload,
                encrypted,
                scope,
                project_id,
                uuid.UUID(str(material.version.id)),
                keyring,
            )
            frozen_payload = _freeze(payload)
            if not isinstance(frozen_payload, Mapping):
                raise AssetResolutionUnavailable(request_id)
            by_slot[material.slot.name] = frozen_payload
        return MaterializedMcpSecrets(
            mcp_version_id=version.id,
            by_slot=MappingProxyType(by_slot),
        )


async def resolve_project_asset_snapshot(
    context: ProjectContext,
    selection: AssetSelection,
    *,
    session_factory: Callable[[], AsyncSession],
) -> ResolvedAssetSnapshot:
    """Functional adapter for call sites that do not keep a resolver instance."""

    return await ProjectAssetResolver(session_factory).resolve_project_asset_snapshot(
        context,
        selection,
    )


async def materialize_mcp_secrets(
    context: ProjectContext,
    resolved: ResolvedMcpSnapshot,
    *,
    session_factory: Callable[[], AsyncSession],
    keyring: CredentialKeyring | None = None,
) -> MaterializedMcpSecrets:
    """Functional internal adapter; plaintext exists only in the returned object."""

    return await ProjectAssetResolver(
        session_factory,
        keyring=keyring,
    ).materialize_mcp_secrets(context, resolved)
