from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.default_agent_repository import (
    ProjectDefaultAgentRepository,
)
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver

_MAX_BIGINT = 9_223_372_036_854_775_807


@dataclass(frozen=True)
class ProjectDefaultAgentSelection:
    project_id: uuid.UUID
    agent_asset_id: uuid.UUID | None
    revision: int


@dataclass(frozen=True)
class _MutationResult:
    selection: ProjectDefaultAgentSelection
    audit_asset_id: uuid.UUID | None
    audit_action: str | None


class ProjectDefaultAgentService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        governance_sink: SharedAssetGovernanceEventSink | None = None,
        resolver: ProjectAssetResolver | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()
        self._resolver = resolver or ProjectAssetResolver(session_factory)

    async def get(
        self,
        actor: ProjectContext,
    ) -> ProjectDefaultAgentSelection:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = ProjectDefaultAgentRepository(session)
                    await repository.lock_project(actor, read=True)
                    return await self.get_in_session(session, actor)
        except SharedAssetError:
            raise
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(actor.request_id) from None

    async def get_in_session(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        *,
        lock: bool = False,
    ) -> ProjectDefaultAgentSelection:
        """Read the selection without opening another transaction.

        With ``lock=True`` the caller must already hold the project and
        membership locks. This is the path used by atomic Thread creation.
        """

        self._require_capability(actor, Capability.SHARED_ASSETS_READ)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise AssetValidationFailed(actor.request_id)
        row = await ProjectDefaultAgentRepository(session).get_in_session(
            actor,
            for_update=lock,
        )
        if row is None:
            return ProjectDefaultAgentSelection(actor.project_id, None, 0)
        return ProjectDefaultAgentSelection(
            actor.project_id,
            row.agent_asset_id,
            row.revision,
        )

    async def resolve_configured_agent_in_session(
        self,
        session: AsyncSession,
        actor: ProjectContext,
    ) -> ResolvedAgentSnapshot | None:
        """Lock and resolve the configured project Agent in the caller tx."""

        selection = await self.get_in_session(session, actor, lock=True)
        if selection.agent_asset_id is None:
            return None
        resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
            session,
            actor,
            AssetSelection(AssetKind.AGENT, selection.agent_asset_id),
        )
        if not isinstance(resolved, ResolvedAgentSnapshot) or resolved.scope is not AssetScope.PROJECT:
            raise AssetResolutionUnavailable(actor.request_id)
        return resolved

    async def replace(
        self,
        actor: ProjectContext,
        agent_asset_id: uuid.UUID | None,
        *,
        expected_revision: int,
    ) -> ProjectDefaultAgentSelection:
        self._require_capability(
            actor,
            Capability.SHARED_ASSETS_MANAGE_BINDINGS,
        )
        self._validate_agent_id(actor, agent_asset_id)
        self._validate_expected_revision(actor, expected_revision)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = ProjectDefaultAgentRepository(session)
                    await repository.lock_project(actor)
                    current = await repository.get_in_session(
                        actor,
                        for_update=True,
                    )
                    current_revision = 0 if current is None else current.revision
                    if current_revision != expected_revision:
                        raise AssetConflict(actor.request_id)
                    if current_revision >= _MAX_BIGINT:
                        raise AssetConflict(actor.request_id)

                    previous_agent_id = None if current is None else current.agent_asset_id
                    if agent_asset_id is not None:
                        target = await repository.lock_project_agent(
                            actor,
                            agent_asset_id,
                        )
                        if target.status != "active":
                            raise AssetConflict(actor.request_id)
                        try:
                            resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
                                session,
                                actor,
                                AssetSelection(AssetKind.AGENT, agent_asset_id),
                            )
                        except AssetResolutionUnavailable:
                            raise AssetConflict(actor.request_id) from None
                        if not isinstance(resolved, ResolvedAgentSnapshot) or resolved.scope is not AssetScope.PROJECT:
                            raise AssetConflict(actor.request_id)

                    row = (
                        await repository.create(actor, agent_asset_id)
                        if current is None
                        else await repository.replace(
                            actor,
                            current,
                            agent_asset_id,
                        )
                    )
                    selection = ProjectDefaultAgentSelection(
                        actor.project_id,
                        row.agent_asset_id,
                        row.revision,
                    )
                    if agent_asset_id is not None:
                        result = _MutationResult(
                            selection,
                            agent_asset_id,
                            "agent.default.set",
                        )
                    elif previous_agent_id is not None:
                        result = _MutationResult(
                            selection,
                            previous_agent_id,
                            "agent.default.clear",
                        )
                    else:
                        result = _MutationResult(selection, None, None)
                    await self._record_governance(session, actor, result)
                    return selection
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if self._constraint_name(exc) in {
                "project_default_agents_pkey",
                "fk_project_default_agents_project_agent",
            }:
                raise AssetConflict(actor.request_id) from None
            raise AssetStorageUnavailable(actor.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(actor.request_id) from None

    async def _record_governance(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        result: _MutationResult,
    ) -> None:
        if result.audit_asset_id is None or result.audit_action is None:
            return
        await self._governance_sink.append_project(
            session,
            actor=actor.user_id,
            project_id=actor.project_id,
            asset_id=result.audit_asset_id,
            version_id=None,
            action=result.audit_action,
            request_id=actor.request_id,
            asset_kind="agent",
        )

    @staticmethod
    def _require_capability(
        actor: ProjectContext,
        capability: Capability,
    ) -> None:
        if isinstance(actor, ProjectContext) and capability in actor.capabilities:
            return
        raise AssetForbidden(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _validate_agent_id(
        actor: ProjectContext,
        agent_asset_id: uuid.UUID | None,
    ) -> None:
        if agent_asset_id is not None and not isinstance(agent_asset_id, uuid.UUID):
            raise AssetValidationFailed(actor.request_id)

    @staticmethod
    def _validate_expected_revision(
        actor: ProjectContext,
        expected_revision: int,
    ) -> None:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0 or expected_revision > _MAX_BIGINT:
            raise AssetConflict(actor.request_id)

    @staticmethod
    def _constraint_name(exc: BaseException) -> str | None:
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            value = getattr(current, "constraint_name", None)
            if isinstance(value, str):
                return value
            current = getattr(current, "orig", None) or getattr(
                current,
                "__cause__",
                None,
            )
        return None
