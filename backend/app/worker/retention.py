"""Worker-only execution of durable retention cases."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.account_private_lifecycle import (
    AccountPrivateLifecycle,
    AccountPrivateScopeChanged,
    LockedAccountPrivateScope,
)
from app.private_work.retention_jobs import (
    account_retention_key,
    former_owner_retention_key,
    project_retention_key,
)
from app.private_work.retention_purge import (
    RetentionCandidate,
    RetentionExecutionActive,
    RetentionExecutionApprovalActive,
    RetentionExecutionApprovalAuditPort,
    RetentionNotEligible,
    RetentionPurgeRepository,
    retention_purge_id,
)
from app.private_work.run_skill_tree_orphan_reaper import (
    RunSkillTreeOrphanReaper,
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
KnowledgePurge = Callable[[UUID], Awaitable[bool]]
_ACTIVE_JOB_STATUSES = ("queued", "leased", "running", "retry_wait")


class KnowledgePurgeIncomplete(RuntimeError):
    """Knowledge cleanup must complete before the project purge proceeds."""

    def __init__(self, project_id: UUID) -> None:
        super().__init__(f"knowledge purge incomplete for project {project_id}")


class RetentionPurgeJobHandler:
    """Revalidate governance authority and atomically purge + settle."""

    # Class-level default keeps the Knowledge gate optional: a handler built
    # without ``__init__`` (partial doubles in host tests) has no hook either.
    _knowledge_purge: KnowledgePurge | None = None

    def __init__(
        self,
        sessions: async_sessionmaker,
        *,
        audit: TrustedOperationAuditSink,
        approval_audit: RetentionExecutionApprovalAuditPort,
        quota: ProjectQuotaEnforcer,
        mount_owner_reconciler: RunSkillTreeOrphanReaper,
        job_repository_builder: RepositoryBuilder = JobRepository,
        repository: RetentionPurgeRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_initial_seconds: int = 2,
        retry_max_seconds: int = 300,
        knowledge_purge: KnowledgePurge | None = None,
    ) -> None:
        if type(audit) is not TrustedOperationAuditSink:
            raise TypeError("retention Worker handler requires trusted audit authority")
        if type(quota) is not ProjectQuotaEnforcer:
            raise TypeError("retention Worker handler requires quota authority")
        if type(retry_initial_seconds) is not int or type(retry_max_seconds) is not int or retry_initial_seconds < 1 or retry_max_seconds < retry_initial_seconds:
            raise ValueError("retention retry policy is invalid")
        if type(mount_owner_reconciler) is not RunSkillTreeOrphanReaper:
            raise TypeError("retention mount-owner reconciler is invalid")
        if repository is not None and type(repository) is not RetentionPurgeRepository:
            raise TypeError("retention purge repository is invalid")
        self._sessions = sessions
        self._audit = audit
        self._approval_audit = approval_audit
        self._quota = quota
        self._job_repository_builder = job_repository_builder
        self._repository = RetentionPurgeRepository() if repository is None else repository
        self._mount_owner_reconciler = mount_owner_reconciler
        # An explicit clock is a test-only forward override. PostgreSQL remains
        # the lower bound for lease and retention authority, so a slow host
        # clock can never extend an expired destructive-work lease.
        self._clock = clock
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds
        # Optional Knowledge Module hook: project purges must clear Knowledge
        # objects and rows before the final governance transaction runs.
        self._knowledge_purge = knowledge_purge

    async def _candidate(
        self,
        session: AsyncSession,
        row: JobRow,
        *,
        now: datetime,
        locked_account_scope: LockedAccountPrivateScope | None = None,
    ) -> RetentionCandidate:
        effective_at = row.retention_effective_at
        generation = row.owner_private_generation
        if row.retention_resource_kind not in {"project", "former_owner", "account"} or type(generation) is not int or generation < 1 or not isinstance(effective_at, datetime) or effective_at.tzinfo is None:
            raise RetentionNotEligible
        effective_at = effective_at.astimezone(UTC)

        if row.retention_resource_kind == "project":
            if row.owner_user_id is not None or row.retention_membership_id is not None:
                raise RetentionNotEligible
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == row.project_id))
            if (
                project is None
                or project.deletion_effective_at is None
                or project.membership_version != generation
                or project.deletion_effective_at.astimezone(UTC) != effective_at
                or row.idempotency_key
                != project_retention_key(
                    project.id,
                    project.deletion_effective_at,
                )
            ):
                raise RetentionNotEligible
            return RetentionCandidate.project(
                project_id=project.id,
                project_generation=generation,
                deletion_effective_at=project.deletion_effective_at,
                idempotency_key=row.idempotency_key,
                request_id="retention-worker",
            )

        if row.retention_resource_kind == "account":
            if (
                row.owner_user_id is None
                or row.retention_membership_id is not None
                or locked_account_scope is None
                or locked_account_scope.owner_user_id != row.owner_user_id
                or row.project_id not in locked_account_scope.project_ids
                or row.idempotency_key
                != account_retention_key(
                    owner_user_id=row.owner_user_id,
                    generation=generation,
                    effective_at=effective_at,
                )
            ):
                raise RetentionNotEligible
            return RetentionCandidate.account(
                owner_user_id=row.owner_user_id,
                project_ids=locked_account_scope.project_ids,
                account_private_generation=generation,
                retention_until=effective_at,
                idempotency_key=row.idempotency_key,
                request_id="retention-worker",
            )

        if row.owner_user_id is None or row.retention_membership_id is None:
            raise RetentionNotEligible
        membership = await session.scalar(
            select(ProjectMembershipRow).where(
                ProjectMembershipRow.id == row.retention_membership_id,
                ProjectMembershipRow.project_id == row.project_id,
                ProjectMembershipRow.user_id == row.owner_user_id,
            )
        )
        if membership is None or membership.retention_until is None or membership.activation_generation != generation:
            raise RetentionNotEligible
        common = {
            "project_id": row.project_id,
            "owner_user_id": row.owner_user_id,
            "membership_id": row.retention_membership_id,
            "activation_generation": generation,
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
        if row.idempotency_key == regular_key and membership.retention_until.astimezone(UTC) == effective_at:
            early_delete = False
            eligibility_at = effective_at
        elif row.idempotency_key == early_key:
            early_delete = True
            eligibility_at = effective_at
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
    ) -> LockedAccountPrivateScope | None:
        """Lock Project -> Membership shells before the retention Job."""

        preview = (
            await session.execute(
                select(
                    JobRow.project_id,
                    JobRow.owner_user_id,
                    JobRow.retention_resource_kind,
                ).where(
                    JobRow.id == claim.job_id,
                    JobRow.job_type == "retention_purge",
                )
            )
        ).one_or_none()
        if preview is None or preview.project_id != claim.scope.project_id or preview.owner_user_id != claim.scope.owner_user_id:
            raise LeaseLost(claim.job_id)
        if preview.retention_resource_kind == "account":
            if preview.owner_user_id is None:
                raise LeaseLost(claim.job_id)
            try:
                locked_scope = await AccountPrivateLifecycle().lock_stable_scope_for_purge(
                    session,
                    preview.owner_user_id,
                )
            except AccountPrivateScopeChanged:
                raise LeaseLost(claim.job_id) from None
            if preview.project_id not in locked_scope.project_ids:
                raise LeaseLost(claim.job_id)
            return locked_scope

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
        return None

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
        public_error_code: str = "RETENTION_EXECUTION_APPROVAL_ACTIVE",
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
            public_error_code=public_error_code,
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

    async def _knowledge_purge_admitted(self, claim: JobClaim) -> bool:
        """Re-check project-purge eligibility before irreversible Knowledge work.

        The Knowledge purge deletes MinIO objects outside the governance
        transaction, so unlike the row purge it cannot be rolled back when
        ``commit()`` later finds the claim cancelled or not eligible. This
        gate mirrors ``_candidate``'s project checks and fails closed on any
        sign the purge will not proceed: cancellation, restore, generation
        drift, or a changed deadline.

        The project row is read FOR SHARE so the check serializes against a
        concurrent restore's FOR UPDATE; the lock is released with this short
        transaction before the purge runs. The remaining window is closed on
        the restore side, which refuses while a purge job is leased/running
        (``lock_recoverable_admin_project``) — together the two checks make a
        restored project and an executed Knowledge purge mutually exclusive.
        """

        async with self._sessions() as session:
            job = (
                await session.execute(
                    select(
                        JobRow.retention_resource_kind,
                        JobRow.cancel_requested_at,
                        JobRow.retention_effective_at,
                        JobRow.owner_private_generation,
                        JobRow.idempotency_key,
                    ).where(JobRow.id == claim.job_id)
                )
            ).one_or_none()
            if job is None or job.retention_resource_kind != "project" or job.cancel_requested_at is not None:
                return False
            effective_at = job.retention_effective_at
            generation = job.owner_private_generation
            if type(generation) is not int or generation < 1 or not isinstance(effective_at, datetime) or effective_at.tzinfo is None:
                return False
            project = (
                await session.execute(
                    select(
                        ProjectRow.status,
                        ProjectRow.deletion_effective_at,
                        ProjectRow.membership_version,
                    )
                    .where(ProjectRow.id == claim.scope.project_id)
                    .with_for_update(read=True, of=ProjectRow)
                )
            ).one_or_none()
            if project is None or project.status != "pending_deletion" or project.deletion_effective_at is None or project.membership_version != generation or project.deletion_effective_at.astimezone(UTC) != effective_at.astimezone(UTC):
                return False
            return job.idempotency_key == project_retention_key(claim.scope.project_id, project.deletion_effective_at)

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

        # Provider enumeration/destroy/readback can be slow and irreversible,
        # so consume it before the governance transaction.  The repository
        # still performs the authoritative durable-root absence check after
        # locking the exact Job -> Run -> Attempt suffix.
        await self._mount_owner_reconciler.reconcile_once()

        # Knowledge cleanup (MinIO objects plus knowledge_* rows) is slow
        # external I/O, so it also runs before the governance transaction. An
        # incomplete purge raises, which settles this claim through the
        # ordinary Worker retry budget — the project purge never completes
        # while Knowledge resources remain.
        if self._knowledge_purge is not None and await self._knowledge_purge_admitted(claim):
            if not await self._knowledge_purge(claim.scope.project_id):
                raise KnowledgePurgeIncomplete(claim.scope.project_id)

        async def commit() -> None:
            async with self._sessions() as session, session.begin():
                locked_account_scope = await self._lock_scope_prefix(
                    session,
                    claim,
                )
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
                    candidate = await self._candidate(
                        session,
                        row,
                        now=now,
                        locked_account_scope=locked_account_scope,
                    )
                    await self._repository.verify_still_eligible(
                        session,
                        candidate,
                        now=now,
                        locked_account_scope=locked_account_scope,
                        coordinator_job_id=row.id,
                    )
                except RetentionExecutionActive as error:
                    await self._defer_for_execution_approval(
                        jobs,
                        row,
                        claim,
                        now=now,
                        retry_after=error.retry_after,
                        public_error_code="RETENTION_EXECUTION_ACTIVE",
                    )
                    return
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
                except (
                    RetentionExecutionActive,
                    RetentionExecutionApprovalActive,
                ) as error:
                    await self._defer_for_execution_approval(
                        jobs,
                        row,
                        claim,
                        now=now,
                        retry_after=error.retry_after,
                        public_error_code=("RETENTION_EXECUTION_ACTIVE" if isinstance(error, RetentionExecutionActive) else "RETENTION_EXECUTION_APPROVAL_ACTIVE"),
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


__all__ = ["KnowledgePurgeIncomplete", "RetentionPurgeJobHandler"]
