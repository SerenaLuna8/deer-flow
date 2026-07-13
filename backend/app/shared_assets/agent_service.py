from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_repository import AgentRepository, AgentVersionRecord
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.models import AgentPayload, AssetScope, WorkflowStatus
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow

_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "uq_agents_project_slug",
        "uq_agents_system_slug",
        "uq_agent_versions_asset_number",
    }
)
_Actor = ProjectContext | SystemAssetGovernanceContext
_T = TypeVar("_T")


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


@dataclass(frozen=True)
class CreateAgent:
    slug: str
    display_name: str


@dataclass(frozen=True)
class AgentAssetView:
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
class AgentVersionView:
    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    workflow_status: WorkflowStatus
    description: str
    soul: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skill_version_ids: tuple[uuid.UUID, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]
    supersedes_version_id: uuid.UUID | None
    payload_checksum: str
    created_by_user_id: str
    created_at: datetime


class AgentService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        governance_sink: SharedAssetGovernanceEventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()

    async def create_asset(self, actor: _Actor, command: CreateAgent) -> AgentAssetView:
        command = self._validate_create(actor, command)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentAssetView:
            if isinstance(actor, ProjectContext):
                row = await repository.create_project_asset(actor, command)
            elif actor.project_id is not None:
                row = await repository.create_override_asset(actor, command)
            else:
                row = await repository.create_system_asset(actor, command)
            return self._asset_view(row)

        result = await self._execute(actor, operation)
        self._record_governance(actor, result.id, None, "agent.create")
        return result

    async def create_version(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        payload: AgentPayload,
        *,
        expected_asset_version: int,
    ) -> AgentVersionView:
        payload = self._validate_payload(actor, payload)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
            await self._validate_dependency_closure(repository, actor, payload.skill_version_ids, payload.mcp_version_ids)
            if isinstance(actor, ProjectContext):
                version_number = await repository.next_project_version_number(actor, asset)
            elif actor.project_id is not None:
                version_number = await repository.next_override_version_number(actor, asset)
            else:
                version_number = await repository.next_system_version_number(actor, asset)
            row = AgentVersionRow(
                agent_id=asset.id,
                version_number=version_number,
                workflow_status=WorkflowStatus.DRAFT.value,
                description=payload.description,
                soul=payload.soul,
                model_ref=payload.model_ref,
                tool_groups=list(payload.tool_groups),
                supersedes_version_id=asset.current_published_version_id,
                payload_checksum=self._payload_checksum(payload),
                created_by_user_id=str(actor.user_id),
            )
            if isinstance(actor, ProjectContext):
                record = await repository.create_project_version(
                    actor,
                    asset.id,
                    row,
                    payload.skill_version_ids,
                    payload.mcp_version_ids,
                )
            elif actor.project_id is not None:
                record = await repository.create_override_version(
                    actor,
                    asset.id,
                    row,
                    payload.skill_version_ids,
                    payload.mcp_version_ids,
                )
            else:
                record = await repository.create_system_version(
                    actor,
                    asset.id,
                    row,
                    payload.skill_version_ids,
                    payload.mcp_version_ids,
                )
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        result = await self._execute(actor, operation)
        self._record_governance(actor, asset_id, result.id, "agent.version.create")
        return result

    async def publish(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentVersionView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
            record = await self._get_version(repository, actor, asset_id, version_id, for_update=True)
            if record.row.workflow_status != WorkflowStatus.DRAFT.value:
                raise AssetConflict(actor.request_id)
            await self._validate_dependency_closure(
                repository,
                actor,
                record.skill_version_ids,
                record.mcp_version_ids,
            )
            current_payload = AgentPayload(
                description=record.row.description,
                soul=record.row.soul,
                model_ref=record.row.model_ref,
                tool_groups=tuple(record.row.tool_groups),
                skill_version_ids=record.skill_version_ids,
                mcp_version_ids=record.mcp_version_ids,
            )
            if self._payload_checksum(current_payload) != record.row.payload_checksum:
                raise AssetValidationFailed(actor.request_id)
            record.row.workflow_status = WorkflowStatus.PUBLISHED.value
            asset.current_published_version_id = record.row.id
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        result = await self._execute(actor, operation)
        self._record_governance(actor, asset_id, version_id, "agent.publish")
        return result

    async def archive(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        result = await self._change_status(
            actor,
            asset_id,
            expected_asset_version=expected_asset_version,
            status="archived",
        )
        self._record_governance(actor, asset_id, None, "agent.archive")
        return result

    async def suspend(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)
        result = await self._change_status(
            actor,
            asset_id,
            expected_asset_version=expected_asset_version,
            status="suspended",
        )
        self._record_governance(actor, asset_id, None, "agent.suspend")
        return result

    async def get(self, actor: _Actor, asset_id: uuid.UUID) -> AgentAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: AgentRepository) -> AgentAssetView:
            return self._asset_view(await self._get_asset(repository, actor, asset_id))

        return await self._execute(actor, operation)

    async def list_visible(self, actor: _Actor) -> tuple[AgentAssetView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: AgentRepository) -> tuple[AgentAssetView, ...]:
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
    ) -> tuple[AgentVersionView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: AgentRepository) -> tuple[AgentVersionView, ...]:
            if isinstance(actor, ProjectContext):
                records = await repository.get_project_version_history(actor, asset_id)
            elif actor.project_id is not None:
                records = await repository.get_override_version_history(actor, asset_id)
            else:
                records = await repository.get_system_version_history(actor, asset_id)
            return tuple(self._version_view(record) for record in records)

        return await self._execute(actor, operation)

    async def _change_status(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
        status: str,
    ) -> AgentAssetView:
        async def operation(repository: AgentRepository) -> AgentAssetView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status == status:
                raise AssetConflict(actor.request_id)
            asset.status = status
            asset.version += 1
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(actor, operation)

    async def _execute(
        self,
        actor: _Actor,
        operation: Callable[[AgentRepository], Awaitable[_T]],
    ) -> _T:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await operation(AgentRepository(session))
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(actor.request_id) from None
            raise AssetStorageUnavailable(actor.request_id) from None
        except DBAPIError:
            raise AssetStorageUnavailable(actor.request_id) from None

    @staticmethod
    async def _get_asset(
        repository: AgentRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentRow:
        if isinstance(actor, ProjectContext):
            return await repository.get_project_asset(actor, asset_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.get_override_asset(actor, asset_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_asset(actor, asset_id, for_update=for_update)
        raise AssetForbidden("unknown")

    @staticmethod
    async def _get_version(
        repository: AgentRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersionRecord:
        if isinstance(actor, ProjectContext):
            return await repository.get_project_version(
                actor,
                asset_id,
                version_id,
                for_update=for_update,
            )
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.get_override_version(
                actor,
                asset_id,
                version_id,
                for_update=for_update,
            )
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_version(
                actor,
                asset_id,
                version_id,
                for_update=for_update,
            )
        raise AssetForbidden("unknown")

    @staticmethod
    def _validate_create(actor: _Actor, command: CreateAgent) -> CreateAgent:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(command, CreateAgent):
            raise AssetValidationFailed(request_id)
        slug = command.slug.strip()
        display_name = command.display_name.strip()
        if _SLUG_PATTERN.fullmatch(slug) is None or not display_name or len(display_name) > 120:
            raise AssetValidationFailed(request_id)
        return CreateAgent(slug=slug, display_name=display_name)

    @staticmethod
    def _validate_payload(actor: _Actor, payload: AgentPayload) -> AgentPayload:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(payload, AgentPayload):
            raise AssetValidationFailed(request_id)
        if not all(isinstance(value, str) for value in (payload.description, payload.soul, payload.model_ref)):
            raise AssetValidationFailed(request_id)
        try:
            normalized = AgentPayload(
                description=payload.description,
                soul=payload.soul,
                model_ref=payload.model_ref,
                tool_groups=tuple(payload.tool_groups),
                skill_version_ids=tuple(payload.skill_version_ids),
                mcp_version_ids=tuple(payload.mcp_version_ids),
            )
        except TypeError:
            raise AssetValidationFailed(request_id) from None
        if not normalized.soul.strip() or not normalized.model_ref.strip() or len(normalized.model_ref) > 255:
            raise AssetValidationFailed(request_id)
        if any(not isinstance(group, str) or not group.strip() for group in normalized.tool_groups):
            raise AssetValidationFailed(request_id)
        if len(set(normalized.tool_groups)) != len(normalized.tool_groups):
            raise AssetValidationFailed(request_id)
        for values in (normalized.skill_version_ids, normalized.mcp_version_ids):
            if any(not isinstance(value, uuid.UUID) for value in values) or len(set(values)) != len(values):
                raise AssetValidationFailed(request_id)
        return normalized

    @staticmethod
    def _require_capability(actor: _Actor, capability: Capability) -> None:
        if isinstance(actor, SystemAssetGovernanceContext):
            return
        if isinstance(actor, ProjectContext) and capability in actor.capabilities:
            return
        request_id = getattr(actor, "request_id", "unknown")
        raise AssetForbidden(request_id)

    @staticmethod
    def _require_expected_version(actor: _Actor, asset: AgentRow, expected: int) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool) or asset.version != expected:
            raise AssetConflict(actor.request_id)

    async def _validate_dependency_closure(
        self,
        repository: AgentRepository,
        actor: _Actor,
        skill_version_ids: Sequence[uuid.UUID],
        mcp_version_ids: Sequence[uuid.UUID],
    ) -> None:
        if isinstance(actor, ProjectContext):
            resolved_skill_ids = await repository.resolve_project_skill_versions(actor, skill_version_ids)
            resolved_mcp_ids = await repository.resolve_project_mcp_versions(actor, mcp_version_ids)
        elif isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            resolved_skill_ids = await repository.resolve_override_skill_versions(actor, skill_version_ids)
            resolved_mcp_ids = await repository.resolve_override_mcp_versions(actor, mcp_version_ids)
        elif isinstance(actor, SystemAssetGovernanceContext):
            resolved_skill_ids = await repository.resolve_system_skill_versions(actor, skill_version_ids)
            resolved_mcp_ids = await repository.resolve_system_mcp_versions(actor, mcp_version_ids)
        else:
            raise AssetForbidden("unknown")
        if set(resolved_skill_ids) != set(skill_version_ids) or set(resolved_mcp_ids) != set(mcp_version_ids):
            raise AssetValidationFailed(actor.request_id)

    @staticmethod
    def _payload_checksum(payload: AgentPayload) -> str:
        canonical = json.dumps(
            {
                "description": payload.description,
                "mcp_version_ids": [str(value) for value in payload.mcp_version_ids],
                "model_ref": payload.model_ref,
                "skill_version_ids": [str(value) for value in payload.skill_version_ids],
                "soul": payload.soul,
                "tool_groups": list(payload.tool_groups),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _asset_view(row: AgentRow) -> AgentAssetView:
        return AgentAssetView(
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

    @staticmethod
    def _version_view(record: AgentVersionRecord) -> AgentVersionView:
        row = record.row
        return AgentVersionView(
            id=row.id,
            agent_id=row.agent_id,
            version_number=row.version_number,
            workflow_status=WorkflowStatus(row.workflow_status),
            description=row.description,
            soul=row.soul,
            model_ref=row.model_ref,
            tool_groups=tuple(row.tool_groups),
            skill_version_ids=record.skill_version_ids,
            mcp_version_ids=record.mcp_version_ids,
            supersedes_version_id=row.supersedes_version_id,
            payload_checksum=row.payload_checksum,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
        )

    def _record_governance(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
    ) -> None:
        if not isinstance(actor, SystemAssetGovernanceContext):
            return
        self._governance_sink.write_override(
            actor=actor.user_id,
            project_id=actor.project_id,
            asset_id=asset_id,
            version_id=version_id,
            action=action,
            request_id=actor.request_id,
        )
