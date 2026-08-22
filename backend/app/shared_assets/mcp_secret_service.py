from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeVar

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.mcp_discovery_repository import McpToolDiscoveryAttemptRepository
from app.shared_assets.mcp_secret_store import (
    McpSecretStore,
    mcp_secret_closure_digest,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectSystemMcpBindingRow,
)

_T = TypeVar("_T")
_DISCOVERY_DOMAIN = b"actweave:mcp-tool-discovery:v1\0"


@dataclass(frozen=True, slots=True)
class McpSecretSlotStatus:
    id: uuid.UUID
    name: str
    purpose: str
    payload_schema: Mapping[str, tuple[str, ...]]
    required: bool
    configured: bool
    revision: int


@dataclass(frozen=True, slots=True)
class McpSecretSetView:
    mcp_server_id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    revision: int
    readiness: Literal["ready", "unready"]
    slots: tuple[McpSecretSlotStatus, ...]


class McpSecretService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        governance_sink: SharedAssetGovernanceEventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()

    async def get(
        self,
        actor: ProjectContext,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
    ) -> McpSecretSetView:
        self._require(actor, Capability.SHARED_ASSETS_READ)

        async def operation(session: AsyncSession) -> McpSecretSetView:
            await self._lock_project(session, actor, read=True)
            asset, version, slots = await self._target(
                session,
                actor,
                mcp_server_id,
                mcp_server_version_id,
                read=True,
            )
            return await self._view(session, actor, asset, version, slots)

        return await self._execute(actor, operation)

    async def replace(
        self,
        actor: ProjectContext,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        slot_name: str,
        payload: Mapping[str, Mapping[str, str]],
    ) -> McpSecretSetView:
        self._require(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)

        async def operation(session: AsyncSession) -> McpSecretSetView:
            await self._lock_project(session, actor)
            asset, version, slots = await self._target(
                session,
                actor,
                mcp_server_id,
                mcp_server_version_id,
            )
            self._require_replaceable(actor, asset, version)
            store = McpSecretStore(session)
            existing = {
                row.slot_id: row.current_generation_id is not None
                for row in await store.list_states(
                    project_id=actor.project_id,
                    mcp_server_id=asset.id,
                    mcp_server_version_id=version.id,
                    for_update=True,
                )
            }
            state = await store.replace(
                project_id=actor.project_id,
                mcp_server_id=asset.id,
                mcp_server_version_id=version.id,
                slots=slots,
                slot_name=slot_name,
                payload=payload,
                actor_user_id=str(actor.user_id),
                request_id=actor.request_id,
            )
            result = await self._view(
                session,
                actor,
                asset,
                version,
                slots,
                for_update=True,
            )
            if result.readiness == "ready":
                materials = await store.load_materials(
                    project_id=actor.project_id,
                    mcp_server_id=asset.id,
                    mcp_server_version_id=version.id,
                    slots=slots,
                    require_required=True,
                    for_update=True,
                    request_id=actor.request_id,
                )
                await self._enqueue_discovery(
                    session,
                    actor,
                    asset,
                    version,
                    mcp_secret_closure_digest(materials),
                )
            slot = next(item for item in result.slots if item.name == slot_name)
            await self._append_event(
                session,
                actor,
                result,
                slot,
                state.current_generation_id,
                "mcp.secret.replace" if existing.get(slot.id) else "mcp.secret.configure",
                "replaced" if existing.get(slot.id) else "created",
            )
            return result

        return await self._execute(actor, operation)

    async def clear(
        self,
        actor: ProjectContext,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        slot_name: str,
        *,
        confirmed: bool,
    ) -> McpSecretSetView:
        self._require(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)
        if confirmed is not True:
            raise AssetValidationFailed(actor.request_id)

        async def operation(session: AsyncSession) -> McpSecretSetView:
            await self._lock_project(session, actor)
            asset, version, slots = await self._target(
                session,
                actor,
                mcp_server_id,
                mcp_server_version_id,
            )
            store = McpSecretStore(session)
            previous = {
                row.slot_id: row.current_generation_id
                for row in await store.list_states(
                    project_id=actor.project_id,
                    mcp_server_id=asset.id,
                    mcp_server_version_id=version.id,
                    for_update=True,
                )
            }
            await store.clear(
                project_id=actor.project_id,
                mcp_server_id=asset.id,
                mcp_server_version_id=version.id,
                slots=slots,
                slot_name=slot_name,
                actor_user_id=str(actor.user_id),
                request_id=actor.request_id,
            )
            result = await self._view(
                session,
                actor,
                asset,
                version,
                slots,
                for_update=True,
            )
            slot = next(item for item in result.slots if item.name == slot_name)
            await self._append_event(
                session,
                actor,
                result,
                slot,
                previous.get(slot.id),
                "mcp.secret.clear",
                "cleared",
            )
            return result

        return await self._execute(actor, operation)

    async def _view(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        asset: McpServerRow,
        version: McpServerVersionRow,
        slots,
        *,
        for_update: bool = False,
    ) -> McpSecretSetView:
        states = await McpSecretStore(session).list_states(
            project_id=actor.project_id,
            mcp_server_id=asset.id,
            mcp_server_version_id=version.id,
            for_update=for_update,
        )
        by_slot = {row.slot_id: row for row in states}
        if set(by_slot) - {slot.id for slot in slots}:
            raise AssetValidationFailed(actor.request_id)
        views = tuple(
            McpSecretSlotStatus(
                id=slot.id,
                name=slot.name,
                purpose=slot.purpose,
                payload_schema={key: tuple(values) for key, values in slot.payload_schema.items()},
                required=slot.required,
                configured=(slot.id in by_slot and by_slot[slot.id].current_generation_id is not None),
                revision=0 if slot.id not in by_slot else int(by_slot[slot.id].revision),
            )
            for slot in slots
        )
        ready = all(not item.required or item.configured for item in views)
        return McpSecretSetView(
            mcp_server_id=asset.id,
            mcp_server_version_id=version.id,
            revision=sum(int(row.revision) for row in states),
            readiness="ready" if ready else "unready",
            slots=views,
        )

    async def _target(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        *,
        read: bool = False,
    ):
        if not isinstance(mcp_server_id, uuid.UUID) or not isinstance(mcp_server_version_id, uuid.UUID):
            raise AssetValidationFailed(actor.request_id)
        statement = (
            select(McpServerRow, McpServerVersionRow)
            .join(
                McpServerVersionRow,
                McpServerVersionRow.mcp_server_id == McpServerRow.id,
            )
            .where(
                McpServerRow.id == mcp_server_id,
                McpServerVersionRow.id == mcp_server_version_id,
                McpServerVersionRow.workflow_status == "published",
                or_(
                    and_(
                        McpServerRow.scope == "project",
                        McpServerRow.project_id == actor.project_id,
                        McpServerRow.status.in_(("active", "suspended")),
                    ),
                    and_(
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        McpServerRow.status == "active",
                        McpServerRow.current_published_version_id == McpServerVersionRow.id,
                    ),
                ),
            )
            .with_for_update(read=read, of=(McpServerRow, McpServerVersionRow))
        )
        pair = (await session.execute(statement)).one_or_none()
        if pair is None:
            raise AssetNotFound(actor.request_id)
        asset, version = pair
        if asset.scope == "system":
            await self._require_system_binding(
                session,
                actor,
                asset,
                version,
                read=read,
            )
        slots = tuple(
            (
                await session.execute(
                    select(McpSecretSlotRow)
                    .where(
                        McpSecretSlotRow.mcp_server_version_id == version.id,
                    )
                    .order_by(McpSecretSlotRow.name, McpSecretSlotRow.id)
                    .with_for_update(read=read, of=McpSecretSlotRow)
                )
            )
            .scalars()
            .all()
        )
        return asset, version, slots

    @staticmethod
    async def _require_system_binding(
        session: AsyncSession,
        actor: ProjectContext,
        asset: McpServerRow,
        version: McpServerVersionRow,
        *,
        read: bool,
    ) -> None:
        statement = (
            select(ProjectSystemMcpBindingRow.project_id)
            .where(
                ProjectSystemMcpBindingRow.project_id == actor.project_id,
                ProjectSystemMcpBindingRow.system_mcp_server_id == asset.id,
                ProjectSystemMcpBindingRow.mcp_server_version_id == version.id,
                ProjectSystemMcpBindingRow.enabled.is_(True),
            )
            .with_for_update(read=read, of=ProjectSystemMcpBindingRow)
        )
        if await session.scalar(statement) is None:
            raise AssetNotFound(actor.request_id)

    @staticmethod
    def _require_replaceable(
        actor: ProjectContext,
        asset: McpServerRow,
        version: McpServerVersionRow,
    ) -> None:
        if asset.scope == "system" and version.id != asset.current_published_version_id:
            raise AssetConflict(actor.request_id)

    @staticmethod
    async def _lock_project(
        session: AsyncSession,
        actor: ProjectContext,
        *,
        read: bool = False,
    ) -> None:
        found = await session.scalar(
            select(ProjectRow.id)
            .join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id)
            .where(
                ProjectRow.id == actor.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.id == actor.membership_id,
                ProjectMembershipRow.user_id == str(actor.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == actor.membership_version,
            )
            .with_for_update(read=read, of=(ProjectRow, ProjectMembershipRow))
        )
        if found is None:
            raise AssetNotFound(actor.request_id)

    @staticmethod
    async def _enqueue_discovery(
        session: AsyncSession,
        actor: ProjectContext,
        asset: McpServerRow,
        version: McpServerVersionRow,
        secret_digest: str,
    ) -> None:
        digest = hashlib.sha256(_DISCOVERY_DOMAIN)
        digest.update(actor.project_id.bytes)
        digest.update(asset.id.bytes)
        digest.update(version.id.bytes)
        digest.update(version.payload_checksum.encode("ascii"))
        digest.update(secret_digest.encode("ascii"))
        digest.update(b"auto")
        await McpToolDiscoveryAttemptRepository(session).enqueue(
            project_id=actor.project_id,
            requested_by_user_id=str(actor.user_id),
            mcp_server_id=asset.id,
            mcp_server_version_id=version.id,
            payload_checksum=version.payload_checksum,
            secret_digest=secret_digest,
            trigger="auto",
            idempotency_key=digest.hexdigest(),
        )

    async def _append_event(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        view: McpSecretSetView,
        slot: McpSecretSlotStatus,
        generation_id: uuid.UUID | None,
        action: str,
        reason: str,
    ) -> None:
        await self._governance_sink.append_project(
            session,
            actor=actor.user_id,
            project_id=actor.project_id,
            action=action,
            asset_kind="mcp",
            asset_id=view.mcp_server_id,
            version_id=view.mcp_server_version_id,
            request_id=actor.request_id,
            secret_metadata={
                "version_id": view.mcp_server_version_id,
                "slot_id": slot.id,
                "secret_name": slot.name,
                "generation_id": generation_id,
                "revision": slot.revision,
                "result": "cleared" if action.endswith(".clear") else "configured",
                "reason": reason,
                "readiness": view.readiness,
            },
        )

    async def _execute(
        self,
        actor: ProjectContext,
        operation: Callable[[AsyncSession], Awaitable[_T]],
    ) -> _T:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await operation(session)
        except SharedAssetError:
            raise
        except (IntegrityError, DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(actor.request_id) from None

    @staticmethod
    def _require(actor: ProjectContext, capability: Capability) -> None:
        if not isinstance(actor, ProjectContext) or capability not in actor.capabilities:
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))


__all__ = [
    "McpSecretService",
    "McpSecretSetView",
    "McpSecretSlotStatus",
]
