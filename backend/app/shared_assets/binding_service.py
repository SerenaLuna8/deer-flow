from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.binding_repository import BindingRepository
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.models import AssetKind, AssetSelection

_Actor = ProjectContext | SystemAssetGovernanceContext
_T = TypeVar("_T")
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "project_system_agent_bindings_pkey",
        "project_system_skill_bindings_pkey",
        "project_system_mcp_bindings_pkey",
    }
)


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
class SystemAssetBinding:
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


class BindingService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        governance_sink: SharedAssetGovernanceEventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()

    async def enable(
        self,
        actor: _Actor,
        selection: AssetSelection,
        *,
        expected_binding_version: int | None = None,
    ) -> SystemAssetBinding:
        selection = self._validate_selection(actor, selection, require_version=True)
        self._require_manage_bindings(actor)

        async def operation(repository: BindingRepository) -> SystemAssetBinding:
            await repository.lock_project(actor)
            existing = await repository.get_binding(
                actor,
                selection.kind,
                selection.asset_id,
                for_update=True,
                required=False,
            )
            if existing is not None:
                if existing.enabled or expected_binding_version is None or existing.version != self._validate_expected(actor, expected_binding_version):
                    raise AssetConflict(actor.request_id)
                await repository.lock_target(actor, selection)
                await repository.validate_target_dependencies(actor, selection)
                version_column = {
                    AssetKind.AGENT: "agent_version_id",
                    AssetKind.SKILL: "skill_version_id",
                    AssetKind.MCP: "mcp_server_version_id",
                }[selection.kind]
                setattr(existing, version_column, selection.version_id)
                existing.enabled = True
                existing.version += 1
                existing.updated_by_user_id = str(actor.user_id)
                await repository.session.flush()
                return self._view(selection.kind, existing)
            if expected_binding_version is not None:
                raise AssetConflict(actor.request_id)
            await repository.lock_target(actor, selection)
            await repository.validate_target_dependencies(actor, selection)
            return self._view(selection.kind, await repository.add_binding(actor, selection))

        result = await self._execute(actor, operation)
        self._record(actor, selection, "binding.enable")
        return result

    async def upgrade(
        self,
        actor: _Actor,
        selection: AssetSelection,
        *,
        expected_binding_version: int,
    ) -> SystemAssetBinding:
        return await self._move(
            actor,
            selection,
            expected_binding_version=expected_binding_version,
            action="binding.upgrade",
        )

    async def rollback(
        self,
        actor: _Actor,
        selection: AssetSelection,
        *,
        expected_binding_version: int,
    ) -> SystemAssetBinding:
        return await self._move(
            actor,
            selection,
            expected_binding_version=expected_binding_version,
            action="binding.rollback",
        )

    async def disable(
        self,
        actor: _Actor,
        selection: AssetSelection,
        *,
        expected_binding_version: int,
    ) -> SystemAssetBinding:
        selection = self._validate_selection(actor, selection, require_version=False)
        self._require_manage_bindings(actor)
        expected = self._validate_expected(actor, expected_binding_version)

        async def operation(repository: BindingRepository) -> SystemAssetBinding:
            await repository.lock_project(actor)
            row = await repository.get_binding(
                actor,
                selection.kind,
                selection.asset_id,
                for_update=True,
            )
            if row.version != expected or not row.enabled:
                raise AssetConflict(actor.request_id)
            row.enabled = False
            row.version += 1
            row.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return self._view(selection.kind, row)

        result = await self._execute(actor, operation)
        self._record(actor, selection, "binding.disable")
        return result

    async def _move(
        self,
        actor: _Actor,
        selection: AssetSelection,
        *,
        expected_binding_version: int,
        action: str,
    ) -> SystemAssetBinding:
        selection = self._validate_selection(actor, selection, require_version=True)
        self._require_manage_bindings(actor)
        expected = self._validate_expected(actor, expected_binding_version)

        async def operation(repository: BindingRepository) -> SystemAssetBinding:
            await repository.lock_project(actor)
            row = await repository.get_binding(
                actor,
                selection.kind,
                selection.asset_id,
                for_update=True,
            )
            if row.version != expected or not row.enabled:
                raise AssetConflict(actor.request_id)
            target = await repository.lock_target(actor, selection)
            await repository.validate_target_dependencies(actor, selection)
            version_column = {
                AssetKind.AGENT: "agent_version_id",
                AssetKind.SKILL: "skill_version_id",
                AssetKind.MCP: "mcp_server_version_id",
            }[selection.kind]
            current_version_id = getattr(row, version_column)
            if current_version_id == selection.version_id:
                raise AssetConflict(actor.request_id)
            current = await repository.lock_system_version(
                actor,
                selection.kind,
                selection.asset_id,
                current_version_id,
                read=True,
            )
            if (action == "binding.upgrade" and target.version.version_number <= current.version_number) or (action == "binding.rollback" and target.version.version_number >= current.version_number):
                raise AssetConflict(actor.request_id)
            setattr(row, version_column, selection.version_id)
            row.version += 1
            row.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return self._view(selection.kind, row)

        result = await self._execute(actor, operation)
        self._record(actor, selection, action)
        return result

    async def _execute(
        self,
        actor: _Actor,
        operation: Callable[[BindingRepository], Awaitable[_T]],
    ) -> _T:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await operation(BindingRepository(session))
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(getattr(actor, "request_id", "unknown")) from None
            raise AssetStorageUnavailable(getattr(actor, "request_id", "unknown")) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(getattr(actor, "request_id", "unknown")) from None

    @staticmethod
    def _require_manage_bindings(actor: _Actor) -> None:
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return
        if isinstance(actor, ProjectContext) and Capability.SHARED_ASSETS_MANAGE_BINDINGS in actor.capabilities:
            return
        raise AssetForbidden(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _validate_selection(
        actor: _Actor,
        selection: AssetSelection,
        *,
        require_version: bool,
    ) -> AssetSelection:
        request_id = getattr(actor, "request_id", "unknown")
        if (
            not isinstance(selection, AssetSelection)
            or not isinstance(selection.kind, AssetKind)
            or not isinstance(selection.asset_id, uuid.UUID)
            or (require_version and not isinstance(selection.version_id, uuid.UUID))
            or (selection.version_id is not None and not isinstance(selection.version_id, uuid.UUID))
        ):
            raise AssetValidationFailed(request_id)
        return selection

    @staticmethod
    def _validate_expected(actor: _Actor, expected: int) -> int:
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise AssetConflict(getattr(actor, "request_id", "unknown"))
        return expected

    @staticmethod
    def _view(kind: AssetKind, row) -> SystemAssetBinding:
        asset_id = getattr(
            row,
            {
                AssetKind.AGENT: "system_agent_id",
                AssetKind.SKILL: "system_skill_id",
                AssetKind.MCP: "system_mcp_server_id",
            }[kind],
        )
        version_id = getattr(
            row,
            {
                AssetKind.AGENT: "agent_version_id",
                AssetKind.SKILL: "skill_version_id",
                AssetKind.MCP: "mcp_server_version_id",
            }[kind],
        )
        return SystemAssetBinding(
            project_id=row.project_id,
            kind=kind,
            asset_id=asset_id,
            version_id=version_id,
            enabled=row.enabled,
            version=row.version,
            created_by_user_id=row.created_by_user_id,
            updated_by_user_id=row.updated_by_user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _record(self, actor: _Actor, selection: AssetSelection, action: str) -> None:
        if not isinstance(actor, SystemAssetGovernanceContext):
            return
        self._governance_sink.write_override(
            actor=actor.user_id,
            project_id=actor.project_id,
            asset_id=selection.asset_id,
            version_id=selection.version_id,
            action=action,
            request_id=actor.request_id,
        )
