"""Durable retention admission owned by Gateway governance transactions.

The Job row is the retention case: its exact project/former-owner scope,
generation-bound idempotency key, and ``available_at`` deadline survive process
restarts.  Scheduler is intentionally not involved; normal Worker polling makes
the delayed case executable when its PostgreSQL deadline arrives.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.projects.model import ProjectMembershipRow

_ACTIVE_JOB_STATUSES = ("queued", "leased", "running", "retry_wait")
_RETENTION_MAX_ATTEMPTS = 5
_EARLY_DELETE_PRIORITY = 32767


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retention timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def former_owner_retention_key(
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    membership_id: uuid.UUID,
    activation_generation: int,
    retention_until: datetime,
    early_delete: bool,
) -> str:
    if not isinstance(activation_generation, int) or activation_generation < 1:
        raise ValueError("activation_generation must be positive")
    project = uuid.UUID(str(project_id))
    owner = uuid.UUID(str(owner_user_id))
    membership = uuid.UUID(str(membership_id))
    deadline = _aware(retention_until).isoformat(timespec="microseconds")
    mode = "early" if early_delete else "deadline"
    return _digest(
        f"retention:former-owner:{project}:{owner}:{membership}:{activation_generation}:{deadline}:{mode}",
    )


def project_retention_key(
    project_id: uuid.UUID,
    deletion_effective_at: datetime,
) -> str:
    project = uuid.UUID(str(project_id))
    deadline = _aware(deletion_effective_at).isoformat(timespec="microseconds")
    return _digest(f"retention:project:{project}:{deadline}")


class RetentionJobAdmission:
    """Session-bound admission/cancellation without transaction ownership."""

    @staticmethod
    async def admit_former_owner(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        membership_id: uuid.UUID,
        activation_generation: int,
        retention_until: datetime,
        available_at: datetime | None = None,
    ) -> uuid.UUID:
        deadline = _aware(retention_until)
        executable_at = deadline if available_at is None else _aware(available_at)
        key = former_owner_retention_key(
            project_id=project_id,
            owner_user_id=owner_user_id,
            membership_id=membership_id,
            activation_generation=activation_generation,
            retention_until=deadline,
            early_delete=False,
        )
        return await JobRepository(session).enqueue(
            EnqueueJob(
                job_type="retention_purge",
                scope=JobScope(project_id, owner_user_id),
                idempotency_key=key,
                run_id=None,
                occurrence_id=None,
                max_attempts=_RETENTION_MAX_ATTEMPTS,
                retry_safety="safe",
                available_at=executable_at,
            )
        )

    @staticmethod
    async def admit_project(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        deletion_effective_at: datetime,
        now: datetime,
    ) -> uuid.UUID:
        deadline = _aware(deletion_effective_at)
        await RetentionJobAdmission._cancel_matching(
            session,
            project_id=project_id,
            owner_user_id=None,
            owner_mode="former_owners",
            reason="project_purge_precedence",
            now=now,
            replacement_deadline=deadline,
        )
        return await JobRepository(session).enqueue(
            EnqueueJob(
                job_type="retention_purge",
                scope=JobScope(project_id, None),
                idempotency_key=project_retention_key(project_id, deadline),
                run_id=None,
                occurrence_id=None,
                max_attempts=_RETENTION_MAX_ATTEMPTS,
                retry_safety="safe",
                available_at=deadline,
            )
        )

    @staticmethod
    async def admit_early_delete(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        membership_id: uuid.UUID,
        activation_generation: int,
        retention_until: datetime,
        now: datetime,
    ) -> uuid.UUID:
        requested_at = _aware(now)
        deadline = _aware(retention_until)
        key = former_owner_retention_key(
            project_id=project_id,
            owner_user_id=owner_user_id,
            membership_id=membership_id,
            activation_generation=activation_generation,
            retention_until=deadline,
            early_delete=True,
        )
        return await JobRepository(session).enqueue(
            EnqueueJob(
                job_type="retention_purge",
                scope=JobScope(project_id, owner_user_id),
                idempotency_key=key,
                run_id=None,
                occurrence_id=None,
                max_attempts=_RETENTION_MAX_ATTEMPTS,
                retry_safety="safe",
                priority=_EARLY_DELETE_PRIORITY,
                available_at=requested_at,
            )
        )

    @staticmethod
    async def cancel_former_owner(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        now: datetime,
        reason: str = "former_owner_rejoined",
    ) -> None:
        await RetentionJobAdmission._cancel_matching(
            session,
            project_id=project_id,
            owner_user_id=owner_user_id,
            owner_mode="exact",
            reason=reason,
            now=now,
        )

    @staticmethod
    async def restore_project(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        now: datetime,
    ) -> None:
        restored_at = _aware(now)
        await RetentionJobAdmission._cancel_matching(
            session,
            project_id=project_id,
            owner_user_id=None,
            owner_mode="project",
            reason="project_restored",
            now=restored_at,
        )
        # Whole-project deletion temporarily owns cleanup precedence.  If the
        # project is restored, generation-exact former-member cases must resume
        # at their original deadline (or immediately when already overdue).
        memberships = (
            (
                await session.execute(
                    select(ProjectMembershipRow)
                    .where(
                        ProjectMembershipRow.project_id == uuid.UUID(str(project_id)),
                        ProjectMembershipRow.status.in_(("left", "removed")),
                        ProjectMembershipRow.retention_until.is_not(None),
                    )
                    .order_by(ProjectMembershipRow.id)
                    .with_for_update(of=ProjectMembershipRow)
                )
            )
            .scalars()
            .all()
        )
        for membership in memberships:
            assert membership.retention_until is not None
            key = former_owner_retention_key(
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=membership.retention_until,
                early_delete=False,
            )
            resumed = await session.execute(
                update(JobRow)
                .where(
                    JobRow.job_type == "retention_purge",
                    JobRow.idempotency_key == key,
                    JobRow.project_id == membership.project_id,
                    JobRow.owner_user_id == membership.user_id,
                    JobRow.status == "cancelled",
                    JobRow.cancel_reason == "project_purge_precedence",
                )
                .values(
                    status="queued",
                    available_at=max(
                        restored_at,
                        _aware(membership.retention_until),
                    ),
                    cancel_requested_at=None,
                    cancel_reason=None,
                    completed_at=None,
                    public_error_code=None,
                    updated_at=restored_at,
                )
            )
            if resumed.rowcount == 0:
                existing = await session.scalar(
                    select(JobRow.id).where(
                        JobRow.job_type == "retention_purge",
                        JobRow.idempotency_key == key,
                    )
                )
                if existing is None:
                    await RetentionJobAdmission.admit_former_owner(
                        session,
                        project_id=membership.project_id,
                        owner_user_id=membership.user_id,
                        membership_id=membership.id,
                        activation_generation=membership.activation_generation,
                        retention_until=membership.retention_until,
                        available_at=max(
                            restored_at,
                            _aware(membership.retention_until),
                        ),
                    )

    @staticmethod
    async def _cancel_matching(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str | None,
        owner_mode: str,
        reason: str,
        now: datetime,
        replacement_deadline: datetime | None = None,
    ) -> None:
        statement = select(JobRow.id, JobRow.owner_user_id).where(
            JobRow.job_type == "retention_purge",
            JobRow.project_id == uuid.UUID(str(project_id)),
            JobRow.status.in_(_ACTIVE_JOB_STATUSES),
        )
        if owner_mode == "exact":
            statement = statement.where(
                JobRow.owner_user_id == str(uuid.UUID(str(owner_user_id))),
            )
        elif owner_mode == "project":
            statement = statement.where(JobRow.owner_user_id.is_(None))
        elif owner_mode == "former_owners":
            if replacement_deadline is None:
                raise ValueError("project replacement deadline is required")
            # A project purge may replace an ordinary former-owner case only
            # when it deletes the same data no later.  A later project
            # deadline must never extend the former member's original term.
            # Explicit early-delete remains independently exercisable.
            statement = statement.where(
                JobRow.owner_user_id.is_not(None),
                JobRow.priority != _EARLY_DELETE_PRIORITY,
                JobRow.available_at >= _aware(replacement_deadline),
            )
        else:  # pragma: no cover - internal invariant
            raise ValueError("invalid retention cancellation mode")
        rows = (await session.execute(statement.order_by(JobRow.id))).all()
        repository = JobRepository(session)
        changed_at = _aware(now)
        for row in rows:
            scope = JobScope(project_id, row.owner_user_id)
            if await repository.request_cancel(
                scope,
                row.id,
                reason=reason,
                now=changed_at,
            ):
                await repository.settle_requested_cancel(
                    scope,
                    row.id,
                    now=changed_at,
                )


__all__ = [
    "RetentionJobAdmission",
    "former_owner_retention_key",
    "project_retention_key",
]
