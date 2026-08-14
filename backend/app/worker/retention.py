"""Worker-only execution of durable retention cases."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.retention_jobs import (
    former_owner_retention_key,
    project_retention_key,
)
from app.private_work.retention_purge import (
    RetentionCandidate,
    RetentionExecutionApprovalActive,
    RetentionExecutionApprovalAuditPort,
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
        approval_audit: RetentionExecutionApprovalAuditPort,
        quota: ProjectQuotaEnforcer,
        job_repository_builder: RepositoryBuilder = JobRepository,
        repository: RetentionPurgeRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_initial_seconds: int = 2,
        retry_max_seconds: int = 300,
    ) -> None:
        if type(audit) is not TrustedOperationAuditSink:
            raise TypeError("retention Worker handler requires trusted audit authority")
        if type(quota) is not ProjectQuotaEnforcer:
            raise TypeError("retention Worker handler requires quota authority")
        if type(retry_initial_seconds) is not int or type(retry_max_seconds) is not int or retry_initial_seconds < 1 or retry_max_seconds < retry_initial_seconds:
            raise ValueError("retention retry policy is invalid")
        self._sessions = sessions
        self._audit = audit
        self._approval_audit = approval_audit
        self._quota = quota
        self._job_repository_builder = job_repository_builder
        self._repository = repository or RetentionPurgeRepository()
        # An explicit clock is a test-only forward override. PostgreSQL remains
        # the lower bound for lease and retention authority, so a slow host
        # clock can never extend an expired destructive-work lease.
        self._clock = clock
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds

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

    @staticmethod
    async def _lock_scope_prefix(
        session: AsyncSession,
        claim: JobClaim,
    ) -> None:
        """Lock Project -> Membership shells before the retention Job."""

        project = await session.scalar(select(ProjectRow.id).where(ProjectRow.id == claim.scope.project_id).with_for_update(of=ProjectRow))
        if project is None:
            raise LeaseLost(claim.job_id)
        membership_statement = (
            select(ProjectMembershipRow.id)
            .where(
                ProjectMembershipRow.project_id == claim.scope.project_id,
            )
            .order_by(
                ProjectMembershipRow.project_id,
                ProjectMembershipRow.user_id,
            )
            .with_for_update(of=ProjectMembershipRow)
        )
        if claim.scope.owner_user_id is not None:
            membership_statement = membership_statement.where(
                ProjectMembershipRow.user_id == claim.scope.owner_user_id,
            )
        memberships = tuple((await session.scalars(membership_statement)).all())
        if claim.scope.owner_user_id is not None and len(memberships) != 1:
            raise LeaseLost(claim.job_id)

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

    async def _defer_for_execution_approval(
        self,
        jobs: JobRepository,
        row: JobRow,
        claim: JobClaim,
        *,
        now: datetime,
        retry_after: datetime | None,
    ) -> None:
        """Defer on bounded external authority without spending failure budget.

        A retention case can wait on a separately leased command/Run. Each
        With a proven absolute deadline, the claim already incremented
        ``attempt_count`` before this handler saw the blocker, so extend
        ``max_attempts`` by exactly one before normal retry settlement. A
        blocker without a deadline uses the ordinary finite failure budget.
        """

        external_authority_deferral = retry_after is not None
        remaining_failure_attempts = row.max_attempts - row.attempt_count + 1
        if external_authority_deferral:
            row.max_attempts += 1
        result = await jobs.retry_or_dead_result(
            claim.job_id,
            lease_token=claim.lease_token,
            public_error_code="RETENTION_EXECUTION_APPROVAL_ACTIVE",
            retry_initial_seconds=self._retry_initial_seconds,
            retry_max_seconds=self._retry_max_seconds,
            now=now,
        )
        if not result.changed:
            raise LeaseLost(claim.job_id)
        if not external_authority_deferral:
            if row.status not in {"retry_wait", "dead"}:
                raise RuntimeError("retention approval retry did not settle")
            return
        if row.status != "retry_wait":
            raise RuntimeError("retention approval deferral became terminal")
        if row.max_attempts - row.attempt_count != remaining_failure_attempts:
            raise RuntimeError("retention approval deferral spent retry budget")
        if retry_after is not None:
            deferred_until = retry_after.astimezone(UTC) + timedelta(seconds=1)
            row.available_at = max(row.available_at, deferred_until)
            row.updated_at = now

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
            async with self._sessions() as session, session.begin():
                await self._lock_scope_prefix(session, claim)
                row = await session.scalar(
                    select(JobRow)
                    .where(
                        JobRow.id == claim.job_id,
                        JobRow.job_type == "retention_purge",
                        JobRow.project_id == claim.scope.project_id,
                    )
                    .with_for_update(of=JobRow)
                    .execution_options(populate_existing=True)
                )
                if row is None or row.owner_user_id != claim.scope.owner_user_id:
                    raise LeaseLost(claim.job_id)
                # Heartbeats stop before this settlement callback starts.  A
                # scope/Job lock wait can therefore outlive the last lease;
                # sample authority only after every prefix and Job lock is
                # held, before any destructive purge work begins.
                database_now = await session.scalar(
                    select(func.clock_timestamp()),
                )
                if not isinstance(database_now, datetime) or database_now.tzinfo is None:
                    raise LeaseLost(claim.job_id)
                now = database_now.astimezone(UTC)
                if self._clock is not None:
                    injected_now = self._clock()
                    if not isinstance(injected_now, datetime) or injected_now.tzinfo is None:
                        raise LeaseLost(claim.job_id)
                    now = max(now, injected_now.astimezone(UTC))
                lease_token_hash = hashlib.sha256(claim.lease_token.encode("utf-8")).hexdigest()
                if row.status not in {"leased", "running"} or row.lease_token_hash != lease_token_hash or row.lease_expires_at is None or row.lease_expires_at <= now:
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
                try:
                    purged_count = await self._repository.physically_purge(
                        session,
                        candidate,
                        quota=self._quota,
                        approval_audit=self._approval_audit,
                    )
                except RetentionExecutionApprovalActive as error:
                    await self._defer_for_execution_approval(
                        jobs,
                        row,
                        claim,
                        now=now,
                        retry_after=error.retry_after,
                    )
                    return
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
