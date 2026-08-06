"""Worker-only execution of durable retention cases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.retention_jobs import (
    former_owner_retention_key,
    project_retention_key,
)
from app.private_work.retention_purge import (
    RetentionCandidate,
    RetentionNotEligible,
    RetentionPurgeRepository,
    retention_purge_id,
)
from app.quotas.integration import ProjectQuotaEnforcer
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow

RepositoryBuilder = Callable[[AsyncSession], JobRepository]
_ACTIVE_JOB_STATUSES = ("queued", "leased", "running", "retry_wait")


class RetentionPurgeJobHandler:
    """Revalidate governance authority and atomically purge + settle."""

    def __init__(
        self,
        sessions: async_sessionmaker,
        *,
        audit: TrustedOperationAuditSink,
        quota: ProjectQuotaEnforcer,
        job_repository_builder: RepositoryBuilder = JobRepository,
        repository: RetentionPurgeRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(audit) is not TrustedOperationAuditSink:
            raise TypeError("retention Worker handler requires trusted audit authority")
        if type(quota) is not ProjectQuotaEnforcer:
            raise TypeError("retention Worker handler requires quota authority")
        self._sessions = sessions
        self._audit = audit
        self._quota = quota
        self._job_repository_builder = job_repository_builder
        self._repository = repository or RetentionPurgeRepository()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _candidate(
        self,
        session: AsyncSession,
        row: JobRow,
        *,
        now: datetime,
    ) -> RetentionCandidate:
        if row.owner_user_id is None:
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == row.project_id))
            if (
                project is None
                or project.deletion_effective_at is None
                or row.idempotency_key
                != project_retention_key(
                    project.id,
                    project.deletion_effective_at,
                )
            ):
                raise RetentionNotEligible
            return RetentionCandidate.project(
                project_id=project.id,
                deletion_effective_at=project.deletion_effective_at,
                idempotency_key=row.idempotency_key,
                request_id="retention-worker",
            )

        membership = await session.scalar(
            select(ProjectMembershipRow).where(
                ProjectMembershipRow.project_id == row.project_id,
                ProjectMembershipRow.user_id == row.owner_user_id,
            )
        )
        if membership is None or membership.retention_until is None:
            raise RetentionNotEligible
        common = {
            "project_id": row.project_id,
            "owner_user_id": row.owner_user_id,
            "membership_id": membership.id,
            "activation_generation": membership.activation_generation,
            "retention_until": membership.retention_until,
        }
        regular_key = former_owner_retention_key(
            **common,
            early_delete=False,
        )
        early_key = former_owner_retention_key(
            **common,
            early_delete=True,
        )
        if row.idempotency_key == regular_key:
            early_delete = False
            eligibility_at = membership.retention_until
        elif row.idempotency_key == early_key:
            early_delete = True
            eligibility_at = row.available_at
        else:
            # Rejoin/leave generation races fail closed: an older Job cannot
            # authorize deletion for the current activation generation.
            raise RetentionNotEligible
        return RetentionCandidate.former_owner(
            **common,
            idempotency_key=row.idempotency_key,
            request_id="retention-worker",
            early_delete=early_delete,
            eligibility_at=eligibility_at,
        )

    async def _cancel_siblings(
        self,
        session: AsyncSession,
        row: JobRow,
        *,
        now: datetime,
    ) -> None:
        statement = update(JobRow).where(
            JobRow.id != row.id,
            JobRow.job_type == "retention_purge",
            JobRow.project_id == row.project_id,
            JobRow.status.in_(_ACTIVE_JOB_STATUSES),
        )
        if row.owner_user_id is not None:
            statement = statement.where(
                JobRow.owner_user_id == row.owner_user_id,
            )
        await session.execute(
            statement.values(
                cancel_requested_at=now,
                cancel_reason="retention_scope_completed",
                updated_at=now,
            )
        )

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobSettlement:
        del authority
        if claim.job_type != "retention_purge":
            return JobSettlement(
                JobOutcome.failed("INVALID_RETENTION_JOB"),
                self._lease_lost_commit(claim),
            )

        async def commit() -> None:
            now = self._clock()
            async with self._sessions() as session, session.begin():
                row = await session.scalar(
                    select(JobRow).where(
                        JobRow.id == claim.job_id,
                        JobRow.job_type == "retention_purge",
                        JobRow.project_id == claim.scope.project_id,
                    )
                )
                if row is None or row.owner_user_id != claim.scope.owner_user_id:
                    raise LeaseLost(claim.job_id)
                jobs = self._job_repository_builder(session)
                if row.cancel_requested_at is not None:
                    if not await jobs.settle_cancelled(
                        claim.job_id,
                        lease_token=claim.lease_token,
                        now=now,
                    ):
                        raise LeaseLost(claim.job_id)
                    return
                try:
                    candidate = await self._candidate(session, row, now=now)
                    await self._repository.verify_still_eligible(
                        session,
                        candidate,
                        now=now,
                    )
                except RetentionNotEligible:
                    if not await jobs.settle_cancelled(
                        claim.job_id,
                        lease_token=claim.lease_token,
                        now=now,
                    ):
                        raise LeaseLost(claim.job_id)
                    return
                purged_count = await self._repository.physically_purge(
                    session,
                    candidate,
                    quota=self._quota,
                )
                await self._audit.purge_completed(
                    session,
                    purge_id=retention_purge_id(candidate.idempotency_key),
                    project_id=candidate.project_id,
                    resource_kind=candidate.resource_kind,
                    purged_count=purged_count,
                    request_id=candidate.request_id,
                )
                await self._cancel_siblings(session, row, now=now)
                if not await jobs.settle_success(
                    claim.job_id,
                    lease_token=claim.lease_token,
                    now=now,
                ):
                    raise LeaseLost(claim.job_id)

        return JobSettlement(JobOutcome.succeeded(), commit)

    @staticmethod
    def _lease_lost_commit(claim: JobClaim):
        async def commit() -> None:
            raise LeaseLost(claim.job_id)

        return commit


__all__ = ["RetentionPurgeJobHandler"]
