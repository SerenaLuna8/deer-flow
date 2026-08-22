from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.projects.capabilities import Capability
from app.projects.context import (
    ProjectContext,
    resolve_project_context_in_transaction,
)
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound
from app.shared_assets.binding_repository import BindingRepository
from app.shared_assets.errors import (
    AssetForbidden,
    AssetNotFound,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import (
    AssetKind,
    AssetSelection,
    ResolvedRunAssetClosure,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from deerflow.mcp_definition_policy import McpEndpointPolicy

MAX_AGENT_RUNTIME_ASSESSMENTS = 100

AgentRuntimeAssessmentStatus = Literal["ready", "blocked"]
AgentRuntimeAssessmentReason = Literal[
    "agent_unavailable",
    "runtime_dependency_unavailable",
    "model_unavailable",
]


@dataclass(frozen=True, slots=True)
class AgentRuntimeAssessment:
    agent_asset_id: uuid.UUID
    selected_version_id: uuid.UUID | None
    status: AgentRuntimeAssessmentStatus
    reason_code: AgentRuntimeAssessmentReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_asset_id, uuid.UUID):
            raise TypeError("Agent runtime assessment requires an Agent ID")
        if self.selected_version_id is not None and not isinstance(
            self.selected_version_id,
            uuid.UUID,
        ):
            raise TypeError("Agent runtime assessment version is invalid")
        if self.status == "ready":
            if self.selected_version_id is None or self.reason_code is not None:
                raise ValueError("ready Agent runtime assessment is invalid")
        elif self.status == "blocked":
            if self.reason_code not in {
                "agent_unavailable",
                "runtime_dependency_unavailable",
                "model_unavailable",
            }:
                raise ValueError("blocked Agent runtime assessment is invalid")
            if (self.reason_code == "agent_unavailable" and self.selected_version_id is not None) or (self.reason_code != "agent_unavailable" and self.selected_version_id is None):
                raise ValueError("blocked Agent runtime assessment version is invalid")
        else:
            raise ValueError("Agent runtime assessment status is invalid")


class AgentRuntimeResolver(Protocol):
    async def resolve_run_asset_closure_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        selection: AssetSelection,
    ) -> ResolvedRunAssetClosure: ...


class AgentRuntimeClosureValidator(Protocol):
    async def validate_run_asset_closure_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        closure: ResolvedRunAssetClosure,
    ) -> object: ...


class ActiveModelCatalog(Protocol):
    async def resolve_admissible_active_model(
        self,
        model_ref: str | None,
    ) -> object | None: ...


class ProjectReadLocker(Protocol):
    async def lock_project(
        self,
        context: ProjectContext,
        *,
        read: bool = False,
    ) -> None: ...


ProjectContextResolver = Callable[..., Awaitable[ProjectContext]]
ActiveModelCatalogFactory = Callable[[AsyncSession], ActiveModelCatalog]
ProjectReadLockerFactory = Callable[[AsyncSession], ProjectReadLocker]


class AgentRuntimeAssessmentService:
    """Read-only, server-authoritative Agent admission preflight.

    The result is advisory: Run admission repeats the same asset-closure and
    model checks in its own write transaction.  Storage uncertainty fails the
    complete batch instead of fabricating a blocked business result.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        endpoint_policy: McpEndpointPolicy | None = None,
        resolver: AgentRuntimeResolver | None = None,
        closure_validator: AgentRuntimeClosureValidator | None = None,
        model_catalog_factory: ActiveModelCatalogFactory = SystemModelRepository,
        project_read_locker_factory: ProjectReadLockerFactory = BindingRepository,
        context_resolver: ProjectContextResolver = (resolve_project_context_in_transaction),
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session factory is required")
        if not callable(model_catalog_factory) or not callable(project_read_locker_factory) or not callable(context_resolver):
            raise TypeError("Agent runtime assessment authority is invalid")
        self._session_factory = session_factory
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._closure_validator = closure_validator or RunSnapshotRepository(
            session_factory,  # type: ignore[arg-type]
            endpoint_policy=endpoint_policy,
        )
        self._model_catalog_factory = model_catalog_factory
        self._project_read_locker_factory = project_read_locker_factory
        self._context_resolver = context_resolver

    async def assess(
        self,
        actor: ProjectContext,
        agent_ids: tuple[uuid.UUID, ...],
    ) -> tuple[AgentRuntimeAssessment, ...]:
        request_id = getattr(actor, "request_id", "unknown")
        if type(actor) is not ProjectContext:
            raise AssetForbidden(
                request_id if isinstance(request_id, str) else "unknown",
            )
        if Capability.SHARED_ASSETS_READ not in actor.capabilities:
            raise AssetForbidden(actor.request_id)
        if type(agent_ids) is not tuple or not 1 <= len(agent_ids) <= MAX_AGENT_RUNTIME_ASSESSMENTS or any(not isinstance(agent_id, uuid.UUID) for agent_id in agent_ids) or len(set(agent_ids)) != len(agent_ids):
            raise AssetValidationFailed(actor.request_id)

        try:
            async with self._session_factory() as session, session.begin():
                current = await self._context_resolver(
                    session,
                    actor.user_id,
                    actor.project_id,
                    actor.request_id,
                    lock=False,
                )
                if type(current) is not ProjectContext or current != actor:
                    raise AssetNotFound(actor.request_id)
                if Capability.SHARED_ASSETS_READ not in current.capabilities:
                    raise AssetForbidden(actor.request_id)
                await self._project_read_locker_factory(session).lock_project(
                    current,
                    read=True,
                )

                private_context = PrivateWorkContext.from_project(current)
                model_catalog = self._model_catalog_factory(session)
                model_availability: dict[str, bool] = {}
                assessments: list[AgentRuntimeAssessment] = []
                for agent_id in agent_ids:
                    assessments.append(
                        await self._assess_one(
                            session,
                            current,
                            private_context,
                            model_catalog,
                            model_availability,
                            agent_id,
                        )
                    )
                return tuple(assessments)
        except (AssetNotFound, AssetForbidden, AssetValidationFailed):
            raise
        except AssetStorageUnavailable:
            raise
        except ProjectNotFound:
            raise AssetNotFound(actor.request_id) from None
        except ProjectDatabaseUnavailable:
            raise AssetStorageUnavailable(actor.request_id) from None
        except PrivateWorkUnavailable:
            raise AssetStorageUnavailable(actor.request_id) from None
        except SystemModelRepositoryInvariant:
            raise AssetStorageUnavailable(actor.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(actor.request_id) from None

    async def _assess_one(
        self,
        session: AsyncSession,
        context: ProjectContext,
        private_context: PrivateWorkContext,
        model_catalog: ActiveModelCatalog,
        model_availability: dict[str, bool],
        agent_id: uuid.UUID,
    ) -> AgentRuntimeAssessment:
        try:
            closure = await self._resolver.resolve_run_asset_closure_in_session(
                session,
                context,
                AssetSelection(AssetKind.AGENT, agent_id),
            )
        except (AssetNotFound, AssetResolutionUnavailable, AssetValidationFailed):
            return AgentRuntimeAssessment(
                agent_asset_id=agent_id,
                selected_version_id=None,
                status="blocked",
                reason_code="agent_unavailable",
            )
        if type(closure) is not ResolvedRunAssetClosure:
            raise AssetStorageUnavailable(context.request_id)

        selected_version_id = closure.lead_agent.version_id
        try:
            await self._closure_validator.validate_run_asset_closure_in_session(
                session,
                private_context,
                closure,
            )
        except (RunSnapshotAssetStale, AssetResolutionUnavailable):
            return AgentRuntimeAssessment(
                agent_asset_id=agent_id,
                selected_version_id=selected_version_id,
                status="blocked",
                reason_code="runtime_dependency_unavailable",
            )

        agents = (closure.lead_agent, *closure.delegated_agents)
        for resolved_agent in agents:
            model_ref = resolved_agent.payload.model_ref
            available = model_availability.get(model_ref)
            if available is None:
                available = (
                    await model_catalog.resolve_admissible_active_model(
                        model_ref,
                    )
                    is not None
                )
                model_availability[model_ref] = available
            if not available:
                return AgentRuntimeAssessment(
                    agent_asset_id=agent_id,
                    selected_version_id=selected_version_id,
                    status="blocked",
                    reason_code="model_unavailable",
                )

        return AgentRuntimeAssessment(
            agent_asset_id=agent_id,
            selected_version_id=selected_version_id,
            status="ready",
            reason_code=None,
        )


__all__ = [
    "AgentRuntimeAssessment",
    "AgentRuntimeAssessmentReason",
    "AgentRuntimeAssessmentService",
    "AgentRuntimeAssessmentStatus",
    "MAX_AGENT_RUNTIME_ASSESSMENTS",
]
