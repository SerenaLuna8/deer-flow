"""Transactional retention purge for expired private-work data."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.account_private_lifecycle import (
    AccountPrivateLifecycle,
    AccountPrivateScopeChanged,
    LockedAccountPrivateScope,
)
from app.private_work.execution_approval_audit import (
    HostExecutionApprovalAuditPort,
)
from app.private_work.execution_approval_lifecycle import (
    ExecutionApprovalPrivateLifecycleConflict,
    LockedExecutionApprovalRows,
    cancel_locked_execution_approval_continuation,
    lock_execution_approval_private_rows,
    reconcile_locked_execution_approval,
    reject_sealed_staged_approval_terminalization,
)
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    transition_output_delivery_obligation_for_approval_terminal,
)
from app.private_work.retention_authority import (
    RetentionPurgeAuthority,
    RetentionPurgeAuthorityConflict,
)
from app.private_work.run_skill_tree_materializer import (
    read_materialization_owner_metadata,
)
from app.private_work.run_skill_tree_orphan_reaper import (
    scan_materialization_owner_ids,
)
from app.quotas.integration import ProjectQuotaEnforcer
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.models import AgentPayload
from deerflow.config.paths import get_paths
from deerflow.persistence.execution_approvals import (
    EXECUTION_APPROVAL_ACTIVE_STATUSES,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
    RunAssetVersionRow,
    RunMcpSecretSnapshotRow,
    RunSkillSecretSnapshotRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import (
    AgentDesignSessionRow,
    SkillDesignSessionRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user.model import UserRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.utils.asyncio import joined_to_thread

_PURGE_NAMESPACE = uuid.UUID("1960a83e-df43-4f8c-85f4-b7193c08a9d0")
_PROVIDER_MOUNT_OWNER_STATES = frozenset({"acquiring", "mounted", "release_pending"})
_PURGED_AGENT_PAYLOAD_CHECKSUM = agent_payload_checksum(
    AgentPayload(
        description="",
        soul="",
        model_ref="default",
        tool_groups=(),
        skill_refs=(),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
)


class RetentionNotEligible(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RETENTION_NOT_ELIGIBLE")


class RetentionExecutionApprovalActive(RuntimeError):
    """A live host-execution continuation still owns private scope data."""

    def __init__(self, *, retry_after: datetime | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("RETENTION_EXECUTION_APPROVAL_ACTIVE")


class RetentionExecutionActive(RuntimeError):
    """A scoped Job has not converged after its durable purge fence."""

    def __init__(self, *, retry_after: datetime | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("RETENTION_EXECUTION_ACTIVE")


class RetentionExecutionApprovalAuditPort(
    HostExecutionApprovalAuditPort,
    Protocol,
):
    """Worker audit authority needed to converge approval-owned Runs."""

    async def run_terminal(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None: ...


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retention timestamp must be timezone-aware")
    return value.astimezone(UTC)


def retention_purge_id(idempotency_key: str) -> uuid.UUID:
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("retention idempotency key is required")
    return uuid.uuid5(_PURGE_NAMESPACE, idempotency_key)


def _provider_lifecycle_owner_job_ids(
    materialization_root: Path,
) -> frozenset[uuid.UUID]:
    owner_ids, invalid_entries = scan_materialization_owner_ids(
        materialization_root,
    )
    if invalid_entries:
        raise ValueError("materialization owner inventory is incomplete")
    job_ids: set[uuid.UUID] = set()
    for owner_id in owner_ids:
        metadata = read_materialization_owner_metadata(
            materialization_root,
            owner_id,
        )
        if metadata.state in _PROVIDER_MOUNT_OWNER_STATES:
            job_ids.add(metadata.job_id)
    return frozenset(job_ids)


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    resource_kind: str
    project_id: uuid.UUID | None
    owner_user_id: str | None
    membership_id: uuid.UUID | None
    activation_generation: int | None
    account_private_generation: int | None
    project_ids: tuple[uuid.UUID, ...]
    eligibility_at: datetime
    idempotency_key: str
    request_id: str
    early_delete: bool = False

    def __post_init__(self) -> None:
        if self.resource_kind not in {
            "project",
            "account",
            "former_owner",
        }:
            raise ValueError("invalid retention resource kind")
        if not isinstance(self.idempotency_key, str) or not 1 <= len(self.idempotency_key) <= 256:
            raise ValueError("invalid retention idempotency key")
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128:
            raise ValueError("invalid retention request id")
        object.__setattr__(self, "eligibility_at", _aware(self.eligibility_at))
        if type(self.early_delete) is not bool:
            raise TypeError("early_delete must be a boolean")

    @classmethod
    def project(
        cls,
        *,
        project_id: uuid.UUID,
        project_generation: int,
        deletion_effective_at: datetime,
        idempotency_key: str,
        request_id: str,
    ) -> RetentionCandidate:
        if type(project_generation) is not int or project_generation < 1:
            raise ValueError("project retention generation must be positive")
        return cls(
            resource_kind="project",
            project_id=uuid.UUID(str(project_id)),
            owner_user_id=None,
            membership_id=None,
            activation_generation=project_generation,
            account_private_generation=None,
            project_ids=(),
            eligibility_at=deletion_effective_at,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    @classmethod
    def account(
        cls,
        *,
        owner_user_id: str,
        project_ids: tuple[uuid.UUID, ...],
        account_private_generation: int,
        retention_until: datetime,
        idempotency_key: str,
        request_id: str,
    ) -> RetentionCandidate:
        projects = tuple(sorted({uuid.UUID(str(value)) for value in project_ids}, key=str))
        if not projects:
            raise ValueError("account purge requires retained project scopes")
        if type(account_private_generation) is not int or account_private_generation < 1:
            raise ValueError("account purge requires a positive lifecycle generation")
        return cls(
            resource_kind="account",
            project_id=None,
            owner_user_id=str(uuid.UUID(str(owner_user_id))),
            membership_id=None,
            activation_generation=None,
            account_private_generation=account_private_generation,
            project_ids=projects,
            eligibility_at=retention_until,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    @classmethod
    def former_owner(
        cls,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        membership_id: uuid.UUID,
        activation_generation: int,
        retention_until: datetime,
        idempotency_key: str,
        request_id: str,
        early_delete: bool = False,
        eligibility_at: datetime | None = None,
    ) -> RetentionCandidate:
        if not isinstance(activation_generation, int) or activation_generation < 1:
            raise ValueError("activation_generation must be positive")
        return cls(
            resource_kind="former_owner",
            project_id=uuid.UUID(str(project_id)),
            owner_user_id=str(uuid.UUID(str(owner_user_id))),
            membership_id=uuid.UUID(str(membership_id)),
            activation_generation=activation_generation,
            account_private_generation=None,
            project_ids=(),
            eligibility_at=eligibility_at or retention_until,
            idempotency_key=idempotency_key,
            request_id=request_id,
            early_delete=early_delete,
        )


@dataclass(frozen=True, slots=True)
class RetentionPurgeResult:
    purge_id: uuid.UUID
    resource_kind: str
    purged_count: int
    purged_at: datetime


class RetentionPurgeRepository:
    """Session-bound validation and deletion without transaction ownership."""

    def __init__(self) -> None:
        root = get_paths().run_skill_materialization_root()
        if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts or root.name != "run-skill-materializations":
            raise ValueError("invalid retention materialization root")
        self._materialization_root = root

    async def verify_still_eligible(
        self,
        session: AsyncSession,
        candidate: RetentionCandidate,
        *,
        now: datetime,
        locked_account_scope: LockedAccountPrivateScope | None = None,
        coordinator_job_id: uuid.UUID | None = None,
    ) -> tuple[tuple[uuid.UUID, str | None], ...]:
        """Lock Phase-B scope and install its exact transaction-local Run set.

        The returned scopes are for application deletion topology.  The
        immediate PostgreSQL closure trigger separately consumes the exact
        per-Run authority installed before this method returns; commit or
        rollback clears it.
        """

        now = _aware(now)
        if now < candidate.eligibility_at:
            raise RetentionNotEligible
        if candidate.resource_kind == "project":
            assert candidate.project_id is not None
            assert candidate.activation_generation is not None
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == candidate.project_id).with_for_update())
            if (
                project is None
                or project.status != "pending_deletion"
                or project.membership_version != candidate.activation_generation
                or project.deletion_effective_at is None
                or _aware(project.deletion_effective_at) != candidate.eligibility_at
                or _aware(project.deletion_effective_at) > now
            ):
                raise RetentionNotEligible
            memberships = (
                (await session.execute(select(ProjectMembershipRow).where(ProjectMembershipRow.project_id == candidate.project_id).order_by(ProjectMembershipRow.project_id, ProjectMembershipRow.user_id).with_for_update())).scalars().all()
            )
            scopes = tuple((candidate.project_id, membership.user_id) for membership in memberships)
            locked_runs = await self._require_execution_quiescent(
                session,
                scopes=((candidate.project_id, None),),
                coordinator_job_id=coordinator_job_id,
                now=now,
            )
            try:
                await RetentionPurgeAuthority.issue_verified_scope(
                    session,
                    purge_id=retention_purge_id(candidate.idempotency_key),
                    resource_kind=candidate.resource_kind,
                    project_id=candidate.project_id,
                    owner_user_id=None,
                    locked_runs=locked_runs,
                )
            except RetentionPurgeAuthorityConflict:
                raise RetentionExecutionActive from None
            return scopes

        if candidate.resource_kind == "former_owner":
            assert candidate.project_id is not None
            assert candidate.owner_user_id is not None
            assert candidate.membership_id is not None
            assert candidate.activation_generation is not None
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == candidate.project_id).with_for_update())
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.id == candidate.membership_id,
                    ProjectMembershipRow.project_id == candidate.project_id,
                    ProjectMembershipRow.user_id == candidate.owner_user_id,
                )
                .with_for_update()
            )
            if project is None or membership is None or membership.status not in {"left", "removed"} or membership.retention_until is None or membership.activation_generation != candidate.activation_generation:
                raise RetentionNotEligible
            retention_until = _aware(membership.retention_until)
            if candidate.early_delete:
                if candidate.eligibility_at > now:
                    raise RetentionNotEligible
            else:
                project_deadline = None if project.deletion_effective_at is None else _aware(project.deletion_effective_at)
                project_allows_owner_deadline = project.status == "active" or (project.status == "pending_deletion" and project_deadline is not None and retention_until < project_deadline)
                if not project_allows_owner_deadline or retention_until != candidate.eligibility_at or retention_until > now:
                    # The earlier deadline owns deletion. Equal deadlines are
                    # project-owned so only one exact purge case completes.
                    raise RetentionNotEligible
            scopes = ((candidate.project_id, candidate.owner_user_id),)
            locked_runs = await self._require_execution_quiescent(
                session,
                scopes=scopes,
                coordinator_job_id=coordinator_job_id,
                now=now,
            )
            try:
                await RetentionPurgeAuthority.issue_verified_scope(
                    session,
                    purge_id=retention_purge_id(candidate.idempotency_key),
                    resource_kind=candidate.resource_kind,
                    project_id=candidate.project_id,
                    owner_user_id=candidate.owner_user_id,
                    locked_runs=locked_runs,
                )
            except RetentionPurgeAuthorityConflict:
                raise RetentionExecutionActive from None
            return scopes

        assert candidate.owner_user_id is not None
        assert candidate.account_private_generation is not None
        if locked_account_scope is None:
            try:
                locked_scope = await AccountPrivateLifecycle().lock_stable_scope_for_purge(
                    session,
                    candidate.owner_user_id,
                )
            except AccountPrivateScopeChanged:
                raise RetentionNotEligible from None
        else:
            locked_scope = locked_account_scope
        owner = locked_scope._user_row
        if (
            locked_scope.project_ids != candidate.project_ids
            or locked_scope.state != "pending_deletion"
            or locked_scope.generation != candidate.account_private_generation
            or getattr(owner, "private_retention_effective_at", None) != candidate.eligibility_at
        ):
            raise RetentionNotEligible
        scopes = tuple((project_id, candidate.owner_user_id) for project_id in locked_scope.project_ids)
        locked_runs = await self._require_execution_quiescent(
            session,
            scopes=scopes,
            coordinator_job_id=coordinator_job_id,
            now=now,
        )
        try:
            await RetentionPurgeAuthority.issue_verified_scope(
                session,
                purge_id=retention_purge_id(candidate.idempotency_key),
                resource_kind=candidate.resource_kind,
                project_id=None,
                owner_user_id=candidate.owner_user_id,
                project_ids=candidate.project_ids,
                locked_runs=locked_runs,
            )
        except RetentionPurgeAuthorityConflict:
            raise RetentionExecutionActive from None
        return scopes

    async def _require_execution_quiescent(
        self,
        session: AsyncSession,
        *,
        scopes: tuple[tuple[uuid.UUID, str | None], ...],
        coordinator_job_id: uuid.UUID | None,
        now: datetime,
    ) -> tuple[RunRow, ...]:
        if not scopes:
            return ()
        scope_predicates = tuple(JobRow.project_id == project_id if owner_user_id is None else (JobRow.project_id == project_id) & (JobRow.owner_user_id == owner_user_id) for project_id, owner_user_id in scopes)
        jobs = tuple(
            (
                await session.execute(
                    select(JobRow)
                    .where(
                        or_(*scope_predicates),
                        *(() if coordinator_job_id is None else (JobRow.id != coordinator_job_id,)),
                    )
                    .order_by(JobRow.project_id, JobRow.owner_user_id, JobRow.id)
                    .with_for_update(of=JobRow)
                )
            )
            .scalars()
            .all()
        )
        active_jobs = tuple(job for job in jobs if job.status in {"leased", "running"} or (job.status in {"queued", "retry_wait"} and job.available_at <= now))
        runs = tuple(
            (
                await session.execute(
                    select(RunRow)
                    .where(or_(*tuple(RunRow.project_id == project_id if owner_user_id is None else (RunRow.project_id == project_id) & (RunRow.owner_user_id == owner_user_id) for project_id, owner_user_id in scopes)))
                    .order_by(
                        RunRow.project_id,
                        RunRow.owner_user_id,
                        RunRow.thread_id,
                        RunRow.run_id,
                    )
                    .with_for_update(of=RunRow)
                )
            )
            .scalars()
            .all()
        )
        active_attempts = ()
        if jobs:
            active_attempts = tuple(
                (
                    await session.execute(
                        select(JobAttemptRow)
                        .where(
                            JobAttemptRow.job_id.in_(
                                tuple(job.id for job in jobs),
                            ),
                            JobAttemptRow.outcome.is_(None),
                        )
                        .order_by(
                            JobAttemptRow.job_id,
                            JobAttemptRow.attempt_number,
                        )
                        .with_for_update(of=JobAttemptRow)
                    )
                )
                .scalars()
                .all()
            )
        if active_jobs or active_attempts:
            retry_after = max(
                (deadline for deadline in (job.lease_expires_at for job in active_jobs) if deadline is not None and deadline > now),
                default=None,
            )
            raise RetentionExecutionActive(retry_after=retry_after)

        # A terminal Job/Attempt is not provider-side absence proof.  Provider
        # lifecycle roots may disappear only through the in-process finalizer's
        # matching release proof or the advisory-locked orphan reconciler's
        # matching owner-absence proof.  Re-read the shared durable root after
        # the exact Job -> Run -> Attempt locks are held; transaction-A/B mount
        # transitions need that same suffix, so a newly acquiring owner cannot
        # cross this final gate.
        exact_job_ids = frozenset(uuid.UUID(str(job.id)) for job in jobs)
        if exact_job_ids:
            try:
                provider_owner_job_ids = await joined_to_thread(
                    _provider_lifecycle_owner_job_ids,
                    self._materialization_root,
                )
            except Exception:
                raise RetentionExecutionActive from None
            if exact_job_ids.intersection(provider_owner_job_ids):
                raise RetentionExecutionActive
        return runs

    async def physically_purge(
        self,
        session: AsyncSession,
        candidate: RetentionCandidate,
        *,
        quota: ProjectQuotaEnforcer,
        approval_audit: RetentionExecutionApprovalAuditPort,
    ) -> int:
        if candidate.resource_kind == "project":
            assert candidate.project_id is not None
            await _purge_execution_approvals(
                session,
                project_id=candidate.project_id,
                owner_user_id=None,
                request_id=candidate.request_id,
                quota=quota,
                audit=approval_audit,
            )
            await release_private_storage_quota(
                session,
                project_id=candidate.project_id,
                owner_user_id=None,
                quota=quota,
                request_id=candidate.request_id,
            )
            await release_project_skill_storage_quota(
                session,
                project_id=candidate.project_id,
                quota=quota,
                request_id=candidate.request_id,
            )
            await purge_private_scope(
                session,
                project_id=candidate.project_id,
                owner_user_id=None,
                quota=quota,
                request_id=candidate.request_id,
                approval_audit=approval_audit,
            )
            await purge_project_shared_scope(
                session,
                project_id=candidate.project_id,
            )
            await quota.reconcile_project_storage(
                session,
                candidate.project_id,
            )
            return 1
        if candidate.resource_kind == "former_owner":
            assert candidate.project_id is not None
            assert candidate.owner_user_id is not None
            await _purge_execution_approvals(
                session,
                project_id=candidate.project_id,
                owner_user_id=candidate.owner_user_id,
                request_id=candidate.request_id,
                quota=quota,
                audit=approval_audit,
            )
            await release_private_storage_quota(
                session,
                project_id=candidate.project_id,
                owner_user_id=candidate.owner_user_id,
                quota=quota,
                request_id=candidate.request_id,
            )
            await purge_private_scope(
                session,
                project_id=candidate.project_id,
                owner_user_id=candidate.owner_user_id,
                quota=quota,
                request_id=candidate.request_id,
                approval_audit=approval_audit,
            )
            return 1
        assert candidate.owner_user_id is not None
        for project_id in candidate.project_ids:
            await _purge_execution_approvals(
                session,
                project_id=project_id,
                owner_user_id=candidate.owner_user_id,
                request_id=candidate.request_id,
                quota=quota,
                audit=approval_audit,
            )
        for project_id in candidate.project_ids:
            await release_private_storage_quota(
                session,
                project_id=project_id,
                owner_user_id=candidate.owner_user_id,
                quota=quota,
                request_id=candidate.request_id,
            )
            await purge_private_scope(
                session,
                project_id=project_id,
                owner_user_id=candidate.owner_user_id,
                quota=quota,
                request_id=candidate.request_id,
                approval_audit=approval_audit,
            )
        completed = await session.execute(
            update(UserRow)
            .where(
                UserRow.id == candidate.owner_user_id,
                UserRow.private_retention_state == "pending_deletion",
                UserRow.private_retention_generation == candidate.account_private_generation,
                UserRow.private_retention_effective_at == candidate.eligibility_at,
            )
            .values(
                private_retention_state="purged",
                private_retention_effective_at=None,
            )
        )
        if completed.rowcount != 1:
            raise RetentionNotEligible
        return len(candidate.project_ids)


async def release_private_storage_quota(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str | None,
    quota: ProjectQuotaEnforcer,
    request_id: str,
) -> None:
    """Release exact ready-file reservations before their retention purge."""

    statement = (
        select(
            PrivateFileRow.id,
            PrivateFileRow.owner_user_id,
            PrivateFileRow.size,
            ProjectMembershipRow.version.label("membership_version"),
        )
        .join(
            ProjectMembershipRow,
            (ProjectMembershipRow.project_id == PrivateFileRow.project_id) & (ProjectMembershipRow.user_id == PrivateFileRow.owner_user_id),
        )
        .where(
            PrivateFileRow.project_id == project_id,
            PrivateFileRow.status == "ready",
            *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
        )
        .order_by(PrivateFileRow.owner_user_id, PrivateFileRow.id)
        .with_for_update(of=PrivateFileRow)
    )
    rows = (await session.execute(statement)).all()
    for row in rows:
        await quota.release_file(
            session,
            PrivateResourceScope(
                project_id=str(project_id),
                owner_user_id=row.owner_user_id,
                membership_version=row.membership_version,
            ),
            file_id=row.id,
            size=row.size,
            request_id=request_id,
        )


async def release_project_skill_storage_quota(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    quota: ProjectQuotaEnforcer,
    request_id: str,
) -> None:
    """Release exact immutable Skill-version reservations before project purge."""

    rows = (
        await session.execute(
            select(
                SkillVersionFileRow.skill_version_id,
                SkillVersionFileRow.size_bytes,
            )
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
            .order_by(
                SkillVersionFileRow.skill_version_id,
                SkillVersionFileRow.path,
            )
            .with_for_update(of=SkillVersionFileRow)
        )
    ).all()
    version_sizes: dict[uuid.UUID, int] = {}
    for row in rows:
        version_sizes[row.skill_version_id] = version_sizes.get(row.skill_version_id, 0) + row.size_bytes
    for version_id, size in version_sizes.items():
        await quota.release_skill_version_if_reserved(
            session,
            project_id=project_id,
            version_id=version_id,
            size=size,
        )


async def _cancel_retention_approval(
    session: AsyncSession,
    row: ExecutionApprovalRequestRow,
    *,
    now: datetime,
    request_id: str,
    audit: RetentionExecutionApprovalAuditPort,
) -> None:
    if row.status not in EXECUTION_APPROVAL_ACTIVE_STATUSES:
        return
    try:
        await reject_sealed_staged_approval_terminalization(
            session,
            row,
        )
        await transition_output_delivery_obligation_for_approval_terminal(
            session,
            approval=row,
            approval_status="cancelled",
            now=now,
        )
    except (
        ExecutionApprovalPrivateLifecycleConflict,
        OutputDeliveryObligationConflict,
    ):
        raise RetentionExecutionApprovalActive() from None
    row.status = "cancelled"
    row.version += 1
    row.terminal_at = now
    row.updated_at = now
    await audit.host_execution_approval_terminal(
        session,
        project_id=row.project_id,
        source_run_id=row.source_run_id,
        status="cancelled",
        request_id=request_id,
        occurred_at=now,
    )


async def _cancel_unowned_approval_continuation(
    session: AsyncSession,
    locked: LockedExecutionApprovalRows,
    row: ExecutionApprovalRequestRow,
    *,
    now: datetime,
    request_id: str,
    quota: ProjectQuotaEnforcer,
    audit: RetentionExecutionApprovalAuditPort,
) -> tuple[bool, datetime | None]:
    """Cancel an unowned linked Run and report whether settlement must wait."""

    if row.continuation_job_id is None or row.continuation_run_id is None:
        return False, None
    job = locked.jobs.get(row.continuation_job_id)
    if job is None:
        raise RetentionExecutionApprovalActive()

    membership_version = await session.scalar(
        select(ProjectMembershipRow.version).where(
            ProjectMembershipRow.project_id == row.project_id,
            ProjectMembershipRow.user_id == row.owner_user_id,
        )
    )
    if membership_version is None:
        raise RetentionExecutionApprovalActive()
    scope = PrivateResourceScope(
        project_id=str(row.project_id),
        owner_user_id=row.owner_user_id,
        membership_version=membership_version,
    )
    try:
        cancel_result = cancel_locked_execution_approval_continuation(
            row,
            locked,
            now=now,
            reason="retention_scope_purged",
        )
    except ExecutionApprovalPrivateLifecycleConflict:
        raise RetentionExecutionApprovalActive() from None
    if cancel_result == "requested":
        # Each Worker lease has a concrete upper bound. A heartbeat may renew
        # it, but every retry re-locks and re-evaluates the new authority while
        # preserving the purge Job's real failure budget.
        return True, job.lease_expires_at

    await quota.release_concurrent_run(
        session,
        scope,
        run_id=row.continuation_run_id,
        request_id=request_id,
    )
    if cancel_result == "cancelled":
        await audit.run_terminal(
            session,
            scope,
            run_id=row.continuation_run_id,
            job_id=row.continuation_job_id,
            job_type=job.job_type,
            status="interrupted",
            public_error_code=None,
            request_id=request_id,
        )
    return False, None


async def _purge_execution_approvals(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str | None,
    request_id: str,
    quota: ProjectQuotaEnforcer | None,
    audit: RetentionExecutionApprovalAuditPort | None,
) -> None:
    """Converge, stop, and remove private approval payload before Run scrub."""

    try:
        locked = await lock_execution_approval_private_rows(
            session,
            project_id=project_id,
            owner_user_id=owner_user_id,
        )
    except ExecutionApprovalPrivateLifecycleConflict:
        raise RetentionExecutionApprovalActive() from None
    now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise RetentionExecutionApprovalActive()
    now = now.astimezone(UTC)
    rows = locked.rows
    if not rows:
        return
    if audit is None or quota is None:
        # Direct/test callers without Worker audit+quota authority may purge a
        # scope with no approvals, but may never converge or erase approvals.
        raise RetentionExecutionApprovalActive()

    retry_after: datetime | None = None
    blocked_without_deadline = False
    for row in rows:
        claimed_deadline = locked.claimed_absolute_deadlines.get(row.id)
        if row.status in EXECUTION_APPROVAL_ACTIVE_STATUSES:
            await reconcile_locked_execution_approval(
                session,
                row,
                now=now,
                audit=audit,
            )
        # A dead DB lease cannot prove that a recently launched Local process
        # has stopped. Preserve its frozen command timeout even if lazy
        # reconciliation just changed claimed -> unknown.
        if claimed_deadline is not None and now < claimed_deadline:
            retry_after = max((claimed_deadline, retry_after) if retry_after is not None else (claimed_deadline,))
            continue
        if row.status == "claimed":
            blocked_without_deadline = True
            continue

        if row.continuation_job_id is not None:
            wait_required, deadline = await _cancel_unowned_approval_continuation(
                session,
                locked,
                row,
                now=now,
                request_id=request_id,
                quota=quota,
                audit=audit,
            )
            if deadline is not None:
                retry_after = max(
                    (value for value in (retry_after, deadline) if value is not None),
                    default=retry_after,
                )
            if wait_required and deadline is None:
                blocked_without_deadline = True

        if row.status in {"staged", "pending", "approved"}:
            await _cancel_retention_approval(
                session,
                row,
                now=now,
                request_id=request_id,
                audit=audit,
            )

    if retry_after is not None or blocked_without_deadline:
        # Persist any safe cancellation/reconciliation in the surrounding
        # retention Job transaction, then retry after the owned process lease.
        raise RetentionExecutionApprovalActive(
            retry_after=None if blocked_without_deadline else retry_after,
        )

    await session.flush()
    approval_ids = tuple(row.id for row in rows)
    await session.execute(
        delete(ExecutionApprovalResultReceiptRow).where(
            ExecutionApprovalResultReceiptRow.project_id == project_id,
            ExecutionApprovalResultReceiptRow.approval_id.in_(approval_ids),
            *(() if owner_user_id is None else (ExecutionApprovalResultReceiptRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(ExecutionApprovalRequestRow).where(
            ExecutionApprovalRequestRow.project_id == project_id,
            ExecutionApprovalRequestRow.id.in_(approval_ids),
            *(() if owner_user_id is None else (ExecutionApprovalRequestRow.owner_user_id == owner_user_id,)),
        )
    )


async def purge_private_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str | None,
    quota: ProjectQuotaEnforcer | None = None,
    request_id: str = "retention-purge",
    approval_audit: RetentionExecutionApprovalAuditPort | None = None,
) -> None:
    """Delete/scrub private payload for one exact project or project+owner scope.

    Immutable jobs, audit rows, governance rows, and their minimal FK shells are
    retained.  Rows with no immutable references are physically removed.
    """

    purged_at = datetime.now(UTC)
    parameters: dict[str, object] = {
        "project_id": project_id,
        "purged_at": purged_at,
    }
    owner_clause = ""
    if owner_user_id is not None:
        owner_clause = " AND owner_user_id = :owner_user_id"
        parameters["owner_user_id"] = owner_user_id

    def owner_for(alias: str) -> str:
        return "" if owner_user_id is None else f" AND {alias}.owner_user_id = :owner_user_id"

    await _purge_execution_approvals(
        session,
        project_id=project_id,
        owner_user_id=owner_user_id,
        request_id=request_id,
        quota=quota,
        audit=approval_audit,
    )

    # Jobs are immutable governance shells, so retention does not delete them.
    # Request cancellation for every active Memory producer in the exact purge
    # scope before deleting its private payload.  An owned Worker observes the
    # request at heartbeat; an unowned Job is terminalized by normal claiming.
    await session.execute(
        update(JobRow)
        .where(
            JobRow.job_type.in_(("memory_dream", "memory_seal")),
            JobRow.project_id == project_id,
            JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
            *(() if owner_user_id is None else (JobRow.owner_user_id == owner_user_id,)),
        )
        .values(
            cancel_requested_at=func.coalesce(
                JobRow.cancel_requested_at,
                parameters["purged_at"],
            ),
            cancel_reason=func.coalesce(
                JobRow.cancel_reason,
                "retention_scope_purged",
            ),
            updated_at=parameters["purged_at"],
        )
    )

    # Agent Builder sessions contain private conversation and generated
    # blueprint bodies. Delete the exact project/owner scope before shared
    # Agents are purged; operations cascade from the session row and completed
    # sessions otherwise retain RESTRICT references to their created Agent.
    await session.execute(
        delete(AgentDesignSessionRow).where(
            AgentDesignSessionRow.project_id == project_id,
            *(() if owner_user_id is None else (AgentDesignSessionRow.owner_user_id == owner_user_id,)),
        )
    )
    # Skill Builder stores the same owner-private conversation class plus
    # temporary candidate BLOBs. Operations and files cascade with the session.
    # Completed sessions must be removed before their created Skill/version so
    # the retention transaction cannot be blocked by the intentional RESTRICT
    # foreign keys.
    await session.execute(
        delete(SkillDesignSessionRow).where(
            SkillDesignSessionRow.project_id == project_id,
            *(() if owner_user_id is None else (SkillDesignSessionRow.owner_user_id == owner_user_id,)),
        )
    )

    # Connection credentials/conversations cascade from exact connection rows.
    await session.execute(
        text(f"DELETE FROM channel_oauth_states WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )
    await session.execute(
        text(f"DELETE FROM channel_conversations WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )
    await session.execute(
        text(f"DELETE FROM channel_connections WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )

    await session.execute(
        delete(RunMemoryContextSnapshotRow).where(
            RunMemoryContextSnapshotRow.project_id == project_id,
            *(() if owner_user_id is None else (RunMemoryContextSnapshotRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(MemoryHistoryEntryRow).where(
            MemoryHistoryEntryRow.project_id == project_id,
            *(() if owner_user_id is None else (MemoryHistoryEntryRow.owner_user_id == owner_user_id,)),
        )
    )
    # The episode archive carries no cascading references and is deleted by
    # exact scope, like the history backlog above.
    await session.execute(
        delete(MemoryEpisodeRow).where(
            MemoryEpisodeRow.project_id == project_id,
            *(() if owner_user_id is None else (MemoryEpisodeRow.owner_user_id == owner_user_id,)),
        )
    )
    # Version and Dream-run rows cascade from the one current document row.
    await session.execute(
        delete(MemoryDocumentRow).where(
            MemoryDocumentRow.project_id == project_id,
            *(() if owner_user_id is None else (MemoryDocumentRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(RunMcpSecretSnapshotRow).where(
            RunMcpSecretSnapshotRow.project_id == project_id,
            *(() if owner_user_id is None else (RunMcpSecretSnapshotRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(RunSkillSecretSnapshotRow).where(
            RunSkillSecretSnapshotRow.project_id == project_id,
            *(() if owner_user_id is None else (RunSkillSecretSnapshotRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(RunAssetVersionRow).where(
            RunAssetVersionRow.project_id == project_id,
            *(() if owner_user_id is None else (RunAssetVersionRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(PrivateArtifactRow).where(
            PrivateArtifactRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateArtifactRow.owner_user_id == owner_user_id,)),
        )
    )
    file_ids = select(PrivateFileRow.id).where(
        PrivateFileRow.project_id == project_id,
        *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
    )
    await session.execute(delete(PrivateFileChunkRow).where(PrivateFileChunkRow.file_id.in_(file_ids)))
    await session.execute(
        update(PrivateFileRow)
        .where(
            PrivateFileRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
        )
        .values(source_file_id=None)
    )
    await session.execute(
        delete(PrivateFileRow).where(
            PrivateFileRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
        )
    )

    # Checkpoint tables are LangGraph-owned and intentionally addressed only by
    # the exact private Thread coordinates collected in this scope.
    thread_predicate = "project_id=:project_id" + owner_clause
    thread_ids = f"SELECT thread_id FROM threads_meta WHERE {thread_predicate}"
    for checkpoint_table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        if await session.scalar(text("SELECT to_regclass(:table_name)"), {"table_name": checkpoint_table}) is not None:
            await session.execute(
                text(f"DELETE FROM {checkpoint_table} WHERE thread_id IN ({thread_ids})"),
                parameters,
            )

    await session.execute(text(f"DELETE FROM run_events WHERE project_id=:project_id{owner_clause}"), parameters)
    await session.execute(text(f"DELETE FROM feedback WHERE project_id=:project_id{owner_clause}"), parameters)

    # Automation rows with immutable job references retain a scrubbed shell;
    # unreferenced rows are physically removed.
    await session.execute(
        text(
            f"""DELETE FROM scheduled_task_runs occurrence
                 WHERE occurrence.project_id=:project_id{owner_for("occurrence")}
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.automation_occurrence_id=occurrence.id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM scheduled_tasks task
                 WHERE task.project_id=:project_id{owner_for("task")}
                   AND NOT EXISTS (SELECT 1 FROM scheduled_task_runs occurrence WHERE occurrence.task_id=task.id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE scheduled_tasks task
                    SET title='purged', prompt='', schedule_spec='{{}}'::json,
                        status='cancelled', next_run_at=NULL, deleted_at=:purged_at,
                        updated_at=:purged_at
                  WHERE task.project_id=:project_id{owner_for("task")}"""
        ),
        parameters,
    )

    # Runs referenced by immutable jobs/audit are scrubbed, while unreferenced
    # Runs and then empty Thread shells are physically removed.
    # Skill Builder operation outcomes remain useful for idempotent replay, but
    # their optional Run link must not pin otherwise deletable private telemetry.
    await session.execute(
        text(
            f"""UPDATE skill_design_operations operation
                    SET run_id=NULL
                  WHERE operation.project_id=:project_id{owner_for("operation")}
                    AND operation.run_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM jobs
                         WHERE jobs.run_id=operation.run_id
                           AND jobs.project_id=operation.project_id
                    )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM runs run
                 WHERE run.project_id=:project_id{owner_for("run")}
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.run_id=run.run_id AND jobs.project_id=run.project_id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE runs run
                    SET assistant_id=NULL, metadata_json='{{}}'::json, kwargs_json='{{}}'::json,
                        error=NULL, first_human_message=NULL, last_ai_message=NULL
                  WHERE run.project_id=:project_id{owner_for("run")}"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM threads_meta thread
                 WHERE thread.project_id=:project_id{owner_for("thread")}
                   AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.thread_id=thread.thread_id)
                   AND NOT EXISTS (SELECT 1 FROM scheduled_tasks WHERE scheduled_tasks.thread_id=thread.thread_id)
                   AND NOT EXISTS (SELECT 1 FROM channel_conversations WHERE channel_conversations.thread_id=thread.thread_id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE threads_meta thread
                    SET assistant_id=NULL, display_name=NULL, metadata_json='{{}}'::json,
                        frozen_at=:purged_at, deleted_at=:purged_at,
                        checkpoint_delete_status='complete', updated_at=:purged_at
                  WHERE thread.project_id=:project_id{owner_for("thread")}"""
        ),
        parameters,
    )


async def _delete_project_version_leaves(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    asset_table: str,
    version_table: str,
    asset_id_column: str,
) -> None:
    """Delete an exact project's immutable version chain from leaves to root."""

    if (asset_table, version_table, asset_id_column) not in {
        ("skills", "skill_versions", "skill_id"),
        ("mcp_servers", "mcp_server_versions", "mcp_server_id"),
    }:
        raise ValueError("unsupported project asset version chain")
    while True:
        deleted = (
            (
                await session.execute(
                    text(
                        f"""DELETE FROM {version_table} AS version
                         USING {asset_table} AS asset
                         WHERE version.{asset_id_column}=asset.id
                           AND asset.scope='project'
                           AND asset.project_id=:project_id
                           AND NOT EXISTS (
                               SELECT 1 FROM {version_table} AS child
                               WHERE child.supersedes_version_id=version.id
                           )
                         RETURNING version.id"""
                    ),
                    {"project_id": project_id},
                )
            )
            .scalars()
            .all()
        )
        if not deleted:
            return


async def purge_project_channel_guest_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> None:
    """Remove one project's group bindings and unreferenced guest principals.

    Project retention normally enters this boundary after ``purge_private_scope``.
    The explicit connection cleanup also makes the shared-scope purge safe when a
    recovery/operator path resumes after the private phase already committed or
    was skipped.  Human memberships and every other project are out of scope.

    Immutable job, Run, or audit shells may still reference a guest membership or
    user.  Those governance references win: nested savepoints turn the final
    membership/user removal into a safe orphan cleanup rather than weakening or
    deleting retained records.
    """

    project_uuid = uuid.UUID(str(project_id))
    guest_memberships = (
        await session.execute(
            select(
                ProjectMembershipRow.id.label("membership_id"),
                ProjectMembershipRow.user_id,
            )
            .where(
                ProjectMembershipRow.project_id == project_uuid,
                ProjectMembershipRow.role == "channel_guest",
            )
            .order_by(ProjectMembershipRow.user_id, ProjectMembershipRow.id)
            .with_for_update(of=ProjectMembershipRow)
        )
    ).all()
    guest_user_ids = tuple(sorted({row.user_id for row in guest_memberships}))
    parameters: dict[str, object] = {"project_id": project_uuid}
    if guest_user_ids:
        parameters["guest_user_ids"] = list(guest_user_ids)
        guest_owner_clause = "owner_user_id = ANY(CAST(:guest_user_ids AS varchar[]))"
        # OAuth rows are not expected for non-login principals, but deleting
        # them makes the boundary fail closed if malformed legacy data exists.
        await session.execute(
            text(f"DELETE FROM channel_oauth_states WHERE project_id=:project_id AND {guest_owner_clause}"),
            parameters,
        )
        await session.execute(
            text(f"DELETE FROM channel_conversations WHERE project_id=:project_id AND {guest_owner_clause}"),
            parameters,
        )
        await session.execute(
            text(f"DELETE FROM channel_connections WHERE project_id=:project_id AND {guest_owner_clause}"),
            parameters,
        )

    # Challenges and bindings retain RESTRICT references to Agent rows, so they
    # must be removed before the shared asset version chains are deleted.
    await session.execute(
        text("DELETE FROM project_channel_group_binding_challenges WHERE project_id=:project_id"),
        parameters,
    )
    await session.execute(
        text("DELETE FROM channel_external_principals WHERE project_id=:project_id"),
        parameters,
    )
    await session.execute(
        text("DELETE FROM project_channel_group_bindings WHERE project_id=:project_id"),
        parameters,
    )

    for row in guest_memberships:
        try:
            async with session.begin_nested():
                await session.execute(
                    delete(ProjectMembershipRow).where(
                        ProjectMembershipRow.id == row.membership_id,
                        ProjectMembershipRow.project_id == project_uuid,
                        ProjectMembershipRow.user_id == row.user_id,
                        ProjectMembershipRow.role == "channel_guest",
                    )
                )
                await session.flush()
        except IntegrityError:
            # A retained immutable shell still owns this exact membership.
            continue

    for guest_user_id in guest_user_ids:
        try:
            async with session.begin_nested():
                await session.execute(
                    delete(UserRow).where(
                        UserRow.id == guest_user_id,
                        UserRow.principal_type == "channel_guest",
                        ~exists(select(ProjectMembershipRow.id).where(ProjectMembershipRow.user_id == guest_user_id)),
                    )
                )
                await session.flush()
        except IntegrityError:
            # Audit/governance rows may legitimately retain a pseudonymous
            # guest principal. Never delete those references or a human user.
            continue


async def purge_project_shared_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> None:
    """Irreversibly remove one deleted project's shared assets and secrets.

    The project, memberships, immutable jobs, and audit rows remain as bounded
    governance tombstones. Project Agent rows that are still required by a
    retained Thread/Automation FK are reduced to a content-free definition
    shell. Skill and MCP version bodies are physically removed.
    """

    project_uuid = uuid.UUID(str(project_id))
    parameters = {
        "project_id": project_uuid,
        "purged_at": datetime.now(UTC),
    }

    await purge_project_channel_guest_scope(
        session,
        project_id=project_uuid,
    )

    # The default pointer must be removed before project Agent packages can be
    # physically deleted or reduced to retained shells.
    await session.execute(
        text("DELETE FROM project_default_agents WHERE project_id=:project_id"),
        parameters,
    )

    for table_name in (
        "project_system_agent_bindings",
        "project_system_skill_bindings",
        "project_system_mcp_bindings",
    ):
        await session.execute(
            text(f"DELETE FROM {table_name} WHERE project_id=:project_id"),
            parameters,
        )

    # Private Run snapshots were removed in the prior phase. Final purge is the
    # irreversible boundary for all Project-owned domain secrets and their
    # secret-free tombstones, including values bound to System definitions.
    for state_table in (
        "project_skill_secret_states",
        "project_mcp_secret_states",
        "project_channel_secret_states",
    ):
        await session.execute(
            text(f"UPDATE {state_table} SET current_generation_id=NULL WHERE project_id=:project_id"),
            parameters,
        )
    for table_name in (
        "project_skill_secret_generations",
        "project_mcp_secret_generations",
        "project_channel_secret_generations",
        "project_skill_secret_states",
        "project_mcp_secret_states",
        "project_channel_secret_states",
        "project_skill_secret_tombstones",
        "project_mcp_secret_tombstones",
        "project_channel_secret_tombstones",
    ):
        await session.execute(
            text(f"DELETE FROM {table_name} WHERE project_id=:project_id"),
            parameters,
        )

    await session.execute(
        text("DELETE FROM project_invitations WHERE project_id=:project_id"),
        parameters,
    )

    project_mcp_versions = """SELECT version.id
        FROM mcp_server_versions AS version
        JOIN mcp_servers AS asset ON asset.id=version.mcp_server_id
        WHERE asset.scope='project' AND asset.project_id=:project_id"""

    # Remove Agent definition references only after the project is locked,
    # pending deletion, and due. The database trigger independently enforces
    # that same eligibility before allowing each reference DELETE.
    await session.execute(
        text(
            """DELETE FROM agent_skill_refs AS ref
               WHERE ref.agent_id IN (
                   SELECT asset.id FROM agents AS asset
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               ) OR (
                   ref.skill_asset_scope='project'
                   AND ref.skill_asset_id IN (
                       SELECT asset.id FROM skills AS asset
                       WHERE asset.scope='project'
                         AND asset.project_id=:project_id
                   )
               )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM agent_mcp_refs AS ref
               WHERE ref.agent_id IN (
                   SELECT asset.id FROM agents AS asset
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               ) OR ref.mcp_server_version_id IN (
                   SELECT version.id FROM mcp_server_versions AS version
                   JOIN mcp_servers AS asset ON asset.id=version.mcp_server_id
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM skill_version_files AS file
               WHERE file.skill_version_id IN (
                   SELECT version.id FROM skill_versions AS version
                   JOIN skills AS asset ON asset.id=version.skill_id
                   WHERE asset.scope='project' AND asset.project_id=:project_id
               )"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM mcp_version_secret_slots AS slot
                 WHERE slot.mcp_server_version_id IN ({project_mcp_versions})"""
        ),
        parameters,
    )

    for table_name, pointer_column, revision_column in (
        ("skills", "current_version_id", "revision"),
        ("mcp_servers", "current_published_version_id", "version"),
    ):
        await session.execute(
            text(
                f"""UPDATE {table_name}
                        SET {pointer_column}=NULL, status='archived',
                            source_key=NULL, updated_at=:purged_at,
                            {revision_column}={revision_column} + 1
                      WHERE scope='project' AND project_id=:project_id"""
            ),
            parameters,
        )

    await _delete_project_version_leaves(
        session,
        project_id=project_uuid,
        asset_table="skills",
        version_table="skill_versions",
        asset_id_column="skill_id",
    )
    await _delete_project_version_leaves(
        session,
        project_id=project_uuid,
        asset_table="mcp_servers",
        version_table="mcp_server_versions",
        asset_id_column="mcp_server_id",
    )

    await session.execute(
        text(
            """DELETE FROM skills
               WHERE scope='project' AND project_id=:project_id"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM mcp_servers
               WHERE scope='project' AND project_id=:project_id"""
        ),
        parameters,
    )
    await session.execute(
        text(
            """DELETE FROM agents AS asset
               WHERE asset.scope='project' AND asset.project_id=:project_id
                 AND NOT EXISTS (
                     SELECT 1 FROM threads_meta AS thread
                     WHERE thread.agent_asset_id=asset.id
                       AND thread.agent_scope='project'
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM scheduled_tasks AS task
                     WHERE task.agent_asset_id=asset.id
                       AND task.agent_scope='project'
                 )"""
        ),
        parameters,
    )
    retained_agent_ids = (
        (
            await session.execute(
                text(
                    """SELECT id FROM agents
                         WHERE scope='project' AND project_id=:project_id
                         ORDER BY id
                         FOR UPDATE"""
                ),
                parameters,
            )
        )
        .scalars()
        .all()
    )
    for agent_id in retained_agent_ids:
        # Retained Thread/Automation shells still require an Agent FK target,
        # but no authored definition content survives final project purge.
        await session.execute(
            text("SELECT set_config('deerflow.agent_definition_mutation_id', :agent_id, true)"),
            {"agent_id": str(agent_id)},
        )
        await session.execute(
            text(
                """UPDATE agents
                      SET slug='purged-' || replace(id::text, '-', ''),
                          display_name='purged', status='archived',
                          definition_id=:definition_id,
                          description='', soul='', model_ref='default',
                          model_settings='{}'::jsonb, tool_groups='[]'::jsonb,
                          payload_checksum=:payload_checksum,
                          agents_instructions='', identity='', user_context='',
                          payload_schema_version=4, source_key=NULL,
                          updated_at=:purged_at, revision=revision + 1
                    WHERE id=:agent_id AND scope='project'
                      AND project_id=:project_id"""
            ),
            {
                **parameters,
                "agent_id": agent_id,
                "definition_id": uuid.uuid5(
                    _PURGE_NAMESPACE,
                    f"purged-agent-definition:{agent_id}",
                ),
                "payload_checksum": _PURGED_AGENT_PAYLOAD_CHECKSUM,
            },
        )
    await session.execute(
        text(
            """UPDATE projects
                  SET display_name='Deleted project', description='', icon='folder',
                      updated_at=:purged_at
                WHERE id=:project_id"""
        ),
        parameters,
    )


class RetentionPurger:
    def __init__(
        self,
        sessions: async_sessionmaker,
        *,
        audit: TrustedOperationAuditSink,
        approval_audit: RetentionExecutionApprovalAuditPort,
        quota: ProjectQuotaEnforcer,
        repository: RetentionPurgeRepository | None = None,
    ) -> None:
        if type(audit) is not TrustedOperationAuditSink:
            raise TypeError("retention purge requires audit authority")
        if type(quota) is not ProjectQuotaEnforcer:
            raise TypeError("retention purge requires quota authority")
        if repository is not None and type(repository) is not RetentionPurgeRepository:
            raise TypeError("retention purge repository is invalid")
        self._sessions = sessions
        self._audit = audit
        self._approval_audit = approval_audit
        self._quota = quota
        self.repository = RetentionPurgeRepository() if repository is None else repository

    async def purge(
        self,
        candidate: RetentionCandidate,
        *,
        now: datetime | None = None,
    ) -> RetentionPurgeResult:
        if type(candidate) is not RetentionCandidate:
            raise TypeError("retention candidate is required")
        purged_at = _aware(now or datetime.now(UTC))
        purge_id = retention_purge_id(candidate.idempotency_key)
        async with self._sessions() as session, session.begin():
            await self.repository.verify_still_eligible(session, candidate, now=purged_at)
            purged_count = await self.repository.physically_purge(
                session,
                candidate,
                quota=self._quota,
                approval_audit=self._approval_audit,
            )
            await session.flush()
            await self._audit.purge_completed(
                session,
                purge_id=purge_id,
                project_id=(None if candidate.resource_kind == "account" else candidate.project_id),
                resource_kind=candidate.resource_kind,
                purged_count=purged_count,
                request_id=candidate.request_id,
            )
        return RetentionPurgeResult(
            purge_id=purge_id,
            resource_kind=candidate.resource_kind,
            purged_count=purged_count,
            purged_at=purged_at,
        )


__all__ = [
    "RetentionCandidate",
    "RetentionExecutionApprovalActive",
    "RetentionExecutionActive",
    "RetentionNotEligible",
    "RetentionPurgeResult",
    "RetentionPurgeRepository",
    "RetentionPurger",
    "purge_private_scope",
    "purge_project_channel_guest_scope",
    "retention_purge_id",
]
