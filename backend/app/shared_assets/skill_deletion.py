"""Project Skill archival and delayed immutable-package purge."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditOutcome,
    AuditProcessContext,
    AuditTarget,
    AuditTargetKind,
)
from app.audit.service import AuditService
from app.projects.context import ProjectContext
from app.quotas.models import QuotaError
from app.shared_assets.errors import (
    AssetConflict,
    AssetNotFound,
    AssetStorageUnavailable,
)
from app.shared_assets.skill_repository import SkillRepository
from deerflow.persistence.private_work.model import (
    RunAssetVersionRow,
    RunSkillVersionRefRow,
)
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.shared_assets import SkillRow, SkillVersionRow

logger = logging.getLogger(__name__)

_DEFAULT_PURGE_BATCH_SIZE = 100
_DEFAULT_PURGE_INTERVAL_SECONDS = 15 * 60.0


_ARCHIVED_SKILL_PURGE_NAMESPACE = uuid.UUID(
    "b4f44208-f6c5-488f-aa8a-f315a3cd24de",
)


@dataclass(frozen=True, slots=True)
class SkillDeleteResult:
    affected_agent_count: int

    def __post_init__(self) -> None:
        if type(self.affected_agent_count) is not int or self.affected_agent_count < 0:
            raise ValueError("affected Agent count must be non-negative")


@dataclass(frozen=True, slots=True)
class ArchivedSkillPurgeReport:
    projects_scanned: int
    skills_examined: int
    skills_purged: int
    versions_purged: int
    released_bytes: int

    def __post_init__(self) -> None:
        values = (
            self.projects_scanned,
            self.skills_examined,
            self.skills_purged,
            self.versions_purged,
            self.released_bytes,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("archived Skill purge facts must be non-negative")

    def plus(self, other: ArchivedSkillPurgeReport) -> ArchivedSkillPurgeReport:
        return ArchivedSkillPurgeReport(
            projects_scanned=self.projects_scanned + other.projects_scanned,
            skills_examined=self.skills_examined + other.skills_examined,
            skills_purged=self.skills_purged + other.skills_purged,
            versions_purged=self.versions_purged + other.versions_purged,
            released_bytes=self.released_bytes + other.released_bytes,
        )


class ArchivedSkillPurgeQuotaPort(Protocol):
    async def release_skill_version_if_reserved(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        version_id: uuid.UUID,
        size: int,
    ) -> bool: ...

    async def reconcile_project_storage(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
    ) -> None: ...


class ArchivedSkillPurgeAuditPort(Protocol):
    async def archived_skill_purged(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        version_count: int,
        request_id: str,
    ) -> None: ...


class ArchivedSkillSweepPort(Protocol):
    async def sweep(
        self,
        *,
        limit: int,
        request_id: str,
    ) -> ArchivedSkillPurgeReport: ...


class DurableArchivedSkillPurgeAuditSink:
    """Process-bound, content-free audit adapter for physical package purge."""

    def __init__(
        self,
        service: AuditService,
        *,
        process_context: AuditProcessContext,
    ) -> None:
        if type(service) is not AuditService:
            raise TypeError("archived Skill purge AuditService is required")
        self._service = service
        self._process_context = service.require_process_context(process_context)

    async def archived_skill_purged(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        version_count: int,
        request_id: str,
    ) -> None:
        if not isinstance(project_id, uuid.UUID) or not isinstance(skill_id, uuid.UUID) or type(version_count) is not int or version_count < 0:
            raise TypeError("archived Skill purge audit facts are invalid")
        purge_id = uuid.uuid5(_ARCHIVED_SKILL_PURGE_NAMESPACE, str(skill_id))
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
            AuditAction.PURGE_COMPLETED,
            AuditTarget(
                AuditTargetKind.PURGE,
                purge_id,
                project_id,
            ),
            AuditOutcome.SUCCESS,
            {
                "resource_kind": "archived_skill",
                "purged_count": version_count,
            },
            request_id=request_id,
        )


class AgentSkillDefinitionRemovalPort(Protocol):
    async def remove_project_skill_from_definitions_in_session(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        skill_id: uuid.UUID,
    ) -> tuple[object, ...]: ...


class SkillDeletionCoordinator:
    """One atomic Project-governed Skill deletion decision."""

    def __init__(self, agents: AgentSkillDefinitionRemovalPort) -> None:
        self._agents = agents

    async def delete_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        skill_id: uuid.UUID,
        expected_revision: int,
    ) -> SkillDeleteResult:
        if not isinstance(session, AsyncSession) or not session.in_transaction() or not isinstance(context, ProjectContext) or not isinstance(skill_id, uuid.UUID) or type(expected_revision) is not int or expected_revision < 1:
            raise AssetConflict(getattr(context, "request_id", "unknown"))

        repository = SkillRepository(session)
        await repository.lock_project_delete_scope(context)
        asset = await repository.get_project_asset(
            context,
            skill_id,
            for_update=True,
        )
        if asset.revision != expected_revision:
            raise AssetConflict(context.request_id)

        affected_agents = await self._agents.remove_project_skill_from_definitions_in_session(
            session,
            context,
            skill_id,
        )
        if type(affected_agents) is not tuple:
            raise AssetStorageUnavailable(context.request_id)

        # The user explicitly chose immediate ciphertext destruction. Retained
        # Runs keep their version files but fail closed if they later need this
        # destroyed Generation.
        await repository.destroy_project_asset_secrets(context, asset)
        await repository.archive_project_asset(context, asset)
        return SkillDeleteResult(affected_agent_count=len(affected_agents))


class ArchivedSkillPurger:
    """Trusted cleanup of archived Skill packages no retained Run still pins."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        quota: ArchivedSkillPurgeQuotaPort,
        audit: ArchivedSkillPurgeAuditPort,
    ) -> None:
        if not callable(sessions):
            raise TypeError("archived Skill purge session factory is invalid")
        self._sessions = sessions
        self._quota = quota
        self._audit = audit

    async def purge_project(
        self,
        project_id: uuid.UUID,
        *,
        limit: int = 100,
        request_id: str = "archived-skill-purge",
    ) -> ArchivedSkillPurgeReport:
        async with self._sessions() as session, session.begin():
            return await self.purge_project_in_session(
                session,
                project_id,
                limit=limit,
                request_id=request_id,
            )

    async def purge_project_in_session(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        limit: int = 100,
        request_id: str = "archived-skill-purge",
    ) -> ArchivedSkillPurgeReport:
        if not isinstance(session, AsyncSession) or not session.in_transaction() or not isinstance(project_id, uuid.UUID) or not 1 <= limit <= 1000 or not isinstance(request_id, str) or not request_id:
            raise ValueError("archived Skill purge request is invalid")

        repository = SkillRepository(session)
        await repository.lock_project_purge_scope(project_id)
        assets = await repository.list_archived_project_assets_for_purge(
            project_id,
            limit=limit,
        )
        skills_purged = 0
        versions_purged = 0
        released_bytes = 0
        for asset in assets:
            versions = await repository.plan_archived_project_asset_purge(
                project_id,
                asset,
            )
            if versions is None:
                continue
            version_count = len(versions)
            size = sum(version.size_bytes for version in versions)
            try:
                for version in versions:
                    await self._quota.release_skill_version_if_reserved(
                        session,
                        project_id,
                        version_id=version.version_id,
                        size=version.size_bytes,
                    )
                await repository.purge_archived_project_asset_versions(
                    project_id,
                    asset,
                    versions,
                )
                await self._audit.archived_skill_purged(
                    session,
                    project_id=project_id,
                    skill_id=asset.id,
                    version_count=version_count,
                    request_id=request_id,
                )
            except QuotaError:
                raise AssetStorageUnavailable(request_id) from None
            skills_purged += 1
            versions_purged += version_count
            released_bytes += size
        if skills_purged:
            try:
                await self._quota.reconcile_project_storage(session, project_id)
            except QuotaError:
                raise AssetStorageUnavailable(request_id) from None
        return ArchivedSkillPurgeReport(
            projects_scanned=1,
            skills_examined=len(assets),
            skills_purged=skills_purged,
            versions_purged=versions_purged,
            released_bytes=released_bytes,
        )

    async def sweep(
        self,
        *,
        limit: int = 100,
        request_id: str = "archived-skill-purge-sweep",
    ) -> ArchivedSkillPurgeReport:
        """Low-frequency repair seam for missed project-scoped invocations."""

        if not 1 <= limit <= 1000:
            raise ValueError("archived Skill purge sweep limit is invalid")
        async with self._sessions() as session:
            project_ids = tuple(
                (
                    await session.execute(
                        select(SkillRow.project_id)
                        .join(ProjectRow, ProjectRow.id == SkillRow.project_id)
                        .where(
                            SkillRow.scope == "project",
                            SkillRow.project_id.is_not(None),
                            SkillRow.status == "archived",
                            or_(
                                ProjectRow.status == "active",
                                and_(
                                    ProjectRow.status == "pending_deletion",
                                    ProjectRow.deletion_effective_at.is_not(None),
                                    ProjectRow.deletion_effective_at <= func.now(),
                                ),
                            ),
                            exists().where(
                                SkillVersionRow.skill_id == SkillRow.id,
                            ),
                            ~exists().where(
                                RunSkillVersionRefRow.project_id == SkillRow.project_id,
                                RunSkillVersionRefRow.asset_scope == "project",
                                RunSkillVersionRefRow.skill_project_id == SkillRow.project_id,
                                RunSkillVersionRefRow.skill_id == SkillRow.id,
                            ),
                            ~exists().where(
                                RunAssetVersionRow.project_id == SkillRow.project_id,
                                RunAssetVersionRow.asset_kind == "skill",
                                RunAssetVersionRow.asset_scope == "project",
                                RunAssetVersionRow.asset_id == SkillRow.id,
                                RunAssetVersionRow.snapshot_schema_version.in_((2, 3)),
                            ),
                        )
                        .distinct()
                        .order_by(SkillRow.project_id)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        report = ArchivedSkillPurgeReport(0, 0, 0, 0, 0)
        remaining = limit
        for project_id in project_ids:
            if not isinstance(project_id, uuid.UUID) or remaining <= 0:
                continue
            try:
                current = await self.purge_project(
                    project_id,
                    limit=remaining,
                    request_id=request_id,
                )
            except AssetNotFound:
                continue
            report = report.plus(current)
            remaining -= current.skills_examined
        return report


class ArchivedSkillPurgeReconciler:
    """Gateway-owned low-frequency repair for deferred Skill package purge."""

    def __init__(
        self,
        purger: ArchivedSkillSweepPort,
        *,
        batch_size: int = _DEFAULT_PURGE_BATCH_SIZE,
        interval_seconds: float = _DEFAULT_PURGE_INTERVAL_SECONDS,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("archived Skill purge batch size is invalid")
        if interval_seconds <= 0:
            raise ValueError("archived Skill purge interval is invalid")
        self._purger = purger
        self._batch_size = batch_size
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        if self._closed or self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run_forever(),
            name="archived-skill-purge-reconciler",
        )

    async def aclose(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def run_once(self) -> ArchivedSkillPurgeReport:
        return await self._purger.sweep(
            limit=self._batch_size,
            request_id="archived-skill-purge-reconcile",
        )

    async def _run_forever(self) -> None:
        while not self._closed:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A later pass retries durable archive rows. Never log Project,
                # Skill, Run, or account coordinates from this process loop.
                logger.warning("Archived Skill purge reconciliation pass deferred")
            await asyncio.sleep(self._interval_seconds)


@asynccontextmanager
async def archived_skill_purge_reconciler_runtime(
    purger: ArchivedSkillSweepPort,
) -> AsyncIterator[ArchivedSkillPurgeReconciler]:
    """Run periodic purge repair for exactly one Gateway lifetime."""

    reconciler = ArchivedSkillPurgeReconciler(purger)
    await reconciler.start()
    try:
        yield reconciler
    finally:
        await reconciler.aclose()


__all__ = [
    "ArchivedSkillPurgeAuditPort",
    "ArchivedSkillPurgeReconciler",
    "ArchivedSkillPurgeQuotaPort",
    "ArchivedSkillPurgeReport",
    "ArchivedSkillPurger",
    "ArchivedSkillSweepPort",
    "AgentSkillDefinitionRemovalPort",
    "DurableArchivedSkillPurgeAuditSink",
    "SkillDeleteResult",
    "SkillDeletionCoordinator",
    "archived_skill_purge_reconciler_runtime",
]
