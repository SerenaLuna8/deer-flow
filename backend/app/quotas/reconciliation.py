from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.projects.models import ProjectRole
from app.quotas.models import (
    QUOTA_DIMENSIONS,
    QuotaDifference,
    QuotaDimension,
    QuotaForbidden,
    QuotaPolicyInvalid,
    QuotaReconciliationAuthority,
    QuotaReconciliationReport,
    _is_issued_quota_reconciliation_authority,
)
from app.quotas.service import QuotaService
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.quotas.model import ProjectUsageLedgerRow
from deerflow.persistence.quotas.sql import QuotaRepository
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import (
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)


class QuotaReconciler:
    """Compare counters with domain authority and append compensations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        service: QuotaService,
    ) -> None:
        self._sessions = session_factory
        self._service = service

    @staticmethod
    def _authority(value: object) -> QuotaReconciliationAuthority:
        if not _is_issued_quota_reconciliation_authority(value):
            raise QuotaForbidden("trusted quota reconciliation authority is required")
        return value

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        selected = value or datetime.now(UTC)
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise QuotaPolicyInvalid("quota reconciliation time must be aware")
        return selected.astimezone(UTC)

    async def _expected(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        dimension: QuotaDimension,
        bucket: str,
    ) -> int:
        if dimension == "members":
            value = await session.scalar(
                select(func.count())
                .select_from(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.role != ProjectRole.CHANNEL_GUEST.value,
                )
            )
        elif dimension == "storage_bytes":
            private_file_bytes = await session.scalar(
                select(func.coalesce(func.sum(PrivateFileRow.size), 0)).where(
                    PrivateFileRow.project_id == project_id,
                    PrivateFileRow.status == "ready",
                )
            )
            project_skill_bytes = await session.scalar(
                select(func.coalesce(func.sum(SkillVersionFileRow.size_bytes), 0))
                .select_from(SkillVersionFileRow)
                .join(
                    SkillVersionRow,
                    SkillVersionRow.id == SkillVersionFileRow.skill_version_id,
                )
                .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
                .where(
                    SkillRow.scope == "project",
                    SkillRow.project_id == project_id,
                )
            )
            value = int(private_file_bytes or 0) + int(project_skill_bytes or 0)
        elif dimension == "concurrent_runs":
            value = await session.scalar(
                select(func.count())
                .select_from(RunRow)
                .where(
                    RunRow.project_id == project_id,
                    RunRow.status.in_(("pending", "running")),
                )
            )
        else:
            value = await session.scalar(
                select(func.coalesce(func.sum(ProjectUsageLedgerRow.delta), 0)).where(
                    ProjectUsageLedgerRow.project_id == project_id,
                    ProjectUsageLedgerRow.dimension == dimension,
                    ProjectUsageLedgerRow.bucket == bucket,
                    ProjectUsageLedgerRow.source_kind.in_(
                        ("consume", "consume_threshold"),
                    ),
                )
            )
        return int(value or 0)

    async def preview(
        self,
        authority: QuotaReconciliationAuthority,
        *,
        now: datetime | None = None,
    ) -> QuotaReconciliationReport:
        selected_project = self._authority(authority).project_id
        checked_at = self._now(now)
        differences: list[QuotaDifference] = []
        async with self._sessions() as session:
            if not await session.scalar(select(ProjectRow.id).where(ProjectRow.id == selected_project)):
                raise QuotaPolicyInvalid("quota reconciliation project is missing")
            repository = QuotaRepository(session)
            for dimension in QUOTA_DIMENSIONS:
                bucket = self._service.bucket_for(dimension, now=checked_at)
                expected = await self._expected(
                    session,
                    selected_project,
                    dimension,
                    bucket,
                )
                counter = await repository.counter(
                    selected_project,
                    dimension,
                    bucket,
                )
                current = 0 if counter is None else counter.used + counter.reserved
                axis_valid = counter is None or ((dimension == "mcp_calls_daily" and counter.reserved == 0) or (dimension != "mcp_calls_daily" and counter.used == 0))
                if current != expected or not axis_valid:
                    differences.append(
                        QuotaDifference(
                            dimension=dimension,
                            bucket=bucket,
                            current=current,
                            expected=expected,
                        )
                    )
        return QuotaReconciliationReport(
            project_id=str(selected_project),
            differences=tuple(differences),
            applied=False,
        )

    async def execute(
        self,
        authority: QuotaReconciliationAuthority,
        *,
        now: datetime | None = None,
    ) -> QuotaReconciliationReport:
        selected_project = self._authority(authority).project_id
        checked_at = self._now(now)
        differences: list[QuotaDifference] = []
        async with self._sessions() as session, session.begin():
            project = (await session.execute(select(ProjectRow).where(ProjectRow.id == selected_project).with_for_update(of=ProjectRow))).scalar_one_or_none()
            if project is None:
                raise QuotaPolicyInvalid("quota reconciliation project is missing")
            repository = QuotaRepository(session)
            for dimension in QUOTA_DIMENSIONS:
                bucket = self._service.bucket_for(dimension, now=checked_at)
                counter = await repository.lock_counter(
                    selected_project,
                    dimension,
                    bucket,
                )
                expected = await self._expected(
                    session,
                    selected_project,
                    dimension,
                    bucket,
                )
                repaired = await self._service._reconcile_locked(
                    session,
                    counter,
                    expected=expected,
                    now=checked_at,
                )
                if repaired is not None:
                    current, authoritative = repaired
                    differences.append(
                        QuotaDifference(
                            dimension=dimension,
                            bucket=bucket,
                            current=current,
                            expected=authoritative,
                        )
                    )
        return QuotaReconciliationReport(
            project_id=str(selected_project),
            differences=tuple(differences),
            applied=bool(differences),
        )


__all__ = ["QuotaReconciler"]
