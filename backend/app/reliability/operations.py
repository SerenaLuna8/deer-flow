from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal

from sqlalchemy import and_, case, exists, func, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.audit.models import (
    AuditAuthorityRejected,
    AuditCursorRejected,
    SystemAuditContext,
    resolve_system_audit_context,
)
from app.quotas.models import QUOTA_DIMENSIONS, QuotaDimension
from app.reliability.errors import ReliabilityInvalid, ReliabilityNotFound
from deerflow.persistence.jobs.model import (
    DeadJobRow,
    JobAttemptRow,
    JobRow,
    WorkerNodeRow,
)
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.quotas.model import ProjectUsageCounterRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.user.model import UserRow

ProjectStatus = Literal["active", "pending_deletion"]
JobStatus = Literal[
    "queued",
    "leased",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "dead",
]
JobType = Literal[
    "private_run",
    "automation_run",
    "retention_purge",
    "mcp_discovery",
    "memory_dream",
    "memory_dream_prepare",
    "memory_seal",
]
ProviderHealthStatus = Literal["ready", "degraded", "unavailable"]


@dataclass(frozen=True, slots=True)
class OperationsCounts:
    projects: int
    suspended_projects: int
    queued_jobs: int
    running_jobs: int
    dead_jobs: int
    ready_jobs: int
    oldest_ready_job_age_seconds: int | None
    stale_leases: int
    waiting_for_worker_runs: int
    waiting_for_terminalization_runs: int


@dataclass(frozen=True, slots=True)
class AggregateUsage:
    dimension: QuotaDimension
    used: int
    reserved: int


@dataclass(frozen=True, slots=True)
class ChannelProviderHealth:
    provider: str
    status: ProviderHealthStatus
    checked_at: datetime
    code: str


@dataclass(frozen=True, slots=True)
class OperationsOverview:
    counts: OperationsCounts
    usage: tuple[AggregateUsage, ...]


def safe_channel_provider_health(
    raw_status: object,
    *,
    checked_at: datetime | None = None,
) -> tuple[ChannelProviderHealth, ...]:
    """Reduce channel process state to a closed, public aggregate contract."""

    selected_time = checked_at or datetime.now(UTC)
    if selected_time.tzinfo is None or selected_time.utcoffset() is None:
        raise ValueError("provider health time must be timezone-aware")
    if not isinstance(raw_status, dict):
        return ()
    service_running = raw_status.get("service_running") is True
    channels = raw_status.get("channels")
    if not isinstance(channels, dict):
        return ()
    result: list[ChannelProviderHealth] = []
    for provider in sorted(channels):
        value = channels[provider]
        if not isinstance(provider, str) or not isinstance(value, dict):
            continue
        enabled = value.get("enabled") is True
        running = value.get("running") is True
        if service_running and enabled and running:
            status: ProviderHealthStatus = "ready"
            code = "CHANNEL_READY"
        elif enabled:
            status = "degraded"
            code = "CHANNEL_STOPPED"
        else:
            status = "unavailable"
            code = "CHANNEL_DISABLED"
        result.append(
            ChannelProviderHealth(
                provider=provider,
                status=status,
                checked_at=selected_time,
                code=code,
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AdminProjectRecord:
    project_id: uuid.UUID
    slug: str
    display_name: str
    status: ProjectStatus
    is_suspended: bool
    state_version: int
    created_at: datetime
    updated_at: datetime
    deletion_effective_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminProjectPage:
    items: tuple[AdminProjectRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class AdminJobRecord:
    job_id: uuid.UUID
    dead_job_id: uuid.UUID | None
    project_id: uuid.UUID
    project_slug: str
    project_display_name: str
    job_type: JobType
    status: JobStatus
    retry_safety: str
    safe_to_requeue: bool
    public_error_code: str | None
    predecessor_dead_job_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class AdminJobPage:
    items: tuple[AdminJobRecord, ...]
    next_cursor: str | None


async def resolve_current_system_audit_context(
    session: AsyncSession,
    user_id: uuid.UUID,
    request_id: str,
) -> SystemAuditContext:
    if type(user_id) is not uuid.UUID:
        raise AuditAuthorityRejected()
    row = (await session.execute(select(UserRow.id, UserRow.system_role).where(UserRow.id == str(user_id)).with_for_update(of=UserRow))).one_or_none()
    if row is None or row.system_role != "system_admin":
        raise AuditAuthorityRejected()
    return resolve_system_audit_context(
        SimpleNamespace(
            id=uuid.UUID(row.id),
            system_role=row.system_role,
        ),
        request_id=request_id,
    )


class SystemOperationsRepository:
    """Public-coordinate platform reads with caller-owned transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def overview(
        self,
        *,
        worker_fresh_for_seconds: int,
        now: datetime | None = None,
    ) -> OperationsOverview:
        selected_time = now or datetime.now(UTC)
        if selected_time.tzinfo is None or selected_time.utcoffset() is None:
            raise ValueError("overview time must be timezone-aware")
        if type(worker_fresh_for_seconds) is not int or worker_fresh_for_seconds < 1:
            raise ValueError("Worker freshness policy must be positive")
        counts = await self._counts(
            worker_fresh_for_seconds=worker_fresh_for_seconds,
        )
        current_daily_bucket = selected_time.astimezone(UTC).date().isoformat()
        usage_rows = (
            await self.session.execute(
                select(
                    ProjectUsageCounterRow.dimension,
                    func.coalesce(func.sum(ProjectUsageCounterRow.used), 0),
                    func.coalesce(func.sum(ProjectUsageCounterRow.reserved), 0),
                )
                .where(
                    or_(
                        and_(
                            ProjectUsageCounterRow.dimension != "mcp_calls_daily",
                            ProjectUsageCounterRow.bucket == "lifetime",
                        ),
                        and_(
                            ProjectUsageCounterRow.dimension == "mcp_calls_daily",
                            ProjectUsageCounterRow.bucket == current_daily_bucket,
                        ),
                    )
                )
                .group_by(ProjectUsageCounterRow.dimension)
            )
        ).all()
        indexed = {str(dimension): (int(used), int(reserved)) for dimension, used, reserved in usage_rows}
        return OperationsOverview(
            counts=counts,
            usage=tuple(
                AggregateUsage(
                    dimension=dimension,
                    used=indexed.get(dimension, (0, 0))[0],
                    reserved=indexed.get(dimension, (0, 0))[1],
                )
                for dimension in QUOTA_DIMENSIONS
            ),
        )

    async def _counts(
        self,
        *,
        worker_fresh_for_seconds: int,
    ) -> OperationsCounts:
        project_counts = (
            await self.session.execute(
                select(
                    func.count(ProjectRow.id),
                    func.coalesce(
                        func.sum(case((ProjectRow.is_suspended.is_(True), 1), else_=0)),
                        0,
                    ),
                )
            )
        ).one()
        clock = select(func.clock_timestamp().label("observed_at")).cte(
            "operations_clock",
        )
        ready_job = and_(
            JobRow.status.in_(("queued", "retry_wait")),
            JobRow.available_at <= clock.c.observed_at,
        )
        stale_lease = and_(
            JobRow.status.in_(("leased", "running")),
            JobRow.lease_expires_at.is_not(None),
            JobRow.lease_expires_at <= clock.c.observed_at,
        )
        eligible_worker_exists = exists(
            select(WorkerNodeRow.id)
            .where(
                WorkerNodeRow.capabilities_json.cast(JSONB).contains(
                    ["private_run"],
                ),
                WorkerNodeRow.draining.is_(False),
                WorkerNodeRow.heartbeat_at >= clock.c.observed_at - timedelta(seconds=worker_fresh_for_seconds),
                or_(
                    JobRow.execution_domain_affinity.is_(None),
                    WorkerNodeRow.execution_domain_affinity == JobRow.execution_domain_affinity,
                ),
            )
            .correlate(JobRow, clock)
        )
        latest_recovery_attempt_exists = exists(
            select(JobAttemptRow.id)
            .where(
                JobAttemptRow.job_id == JobRow.id,
                JobAttemptRow.attempt_number == JobRow.attempt_count,
                JobAttemptRow.outcome.in_(("retry", "lease_lost")),
                JobAttemptRow.finished_at.is_not(None),
            )
            .correlate(JobRow)
        )
        active_attempt_exact = exists(
            select(JobAttemptRow.id)
            .where(
                JobAttemptRow.job_id == JobRow.id,
                JobAttemptRow.attempt_number == JobRow.attempt_count,
                JobAttemptRow.worker_id == JobRow.lease_owner_id,
                JobAttemptRow.lease_token_hash == JobRow.lease_token_hash,
                JobAttemptRow.outcome.is_(None),
                JobAttemptRow.finished_at.is_(None),
            )
            .correlate(JobRow)
        )
        exact_run_base = and_(
            RunRow.job_id == JobRow.id,
            RunRow.project_id == JobRow.project_id,
            RunRow.owner_user_id == JobRow.owner_user_id,
            RunRow.run_id == JobRow.run_id,
            RunRow.origin_trace_id == JobRow.origin_trace_id,
            RunRow.cancel_requested_at.is_(None),
            RunRow.authorization_cancel_requested_at.is_(None),
        )
        unowned_run_exact = exists(
            select(RunRow.run_id)
            .where(
                exact_run_base,
                RunRow.status == "pending",
                RunRow.execution_lease_token_hash.is_(None),
                RunRow.execution_lease_expires_at.is_(None),
            )
            .correlate(JobRow)
        )
        active_run_exact = exists(
            select(RunRow.run_id)
            .where(
                exact_run_base,
                or_(
                    and_(
                        RunRow.status == "pending",
                        RunRow.execution_lease_token_hash.is_(None),
                        RunRow.execution_lease_expires_at.is_(None),
                    ),
                    and_(
                        RunRow.status == "running",
                        RunRow.execution_started_at.is_not(None),
                        or_(
                            and_(
                                RunRow.execution_lease_token_hash == JobRow.lease_token_hash,
                                RunRow.execution_lease_expires_at == JobRow.lease_expires_at,
                            ),
                            and_(
                                JobRow.attempt_count > 1,
                                RunRow.execution_lease_expires_at.is_not(None),
                                RunRow.execution_lease_expires_at <= clock.c.observed_at,
                            ),
                        ),
                    ),
                ),
            )
            .correlate(JobRow, clock)
        )
        unowned_candidate = and_(
            JobRow.job_type == "private_run",
            JobRow.cancel_requested_at.is_(None),
            JobRow.lease_owner_id.is_(None),
            JobRow.lease_token_hash.is_(None),
            JobRow.lease_expires_at.is_(None),
            ready_job,
            unowned_run_exact,
            or_(
                and_(JobRow.status == "queued", JobRow.attempt_count == 0),
                and_(
                    JobRow.status == "retry_wait",
                    JobRow.attempt_count > 0,
                    latest_recovery_attempt_exists,
                ),
            ),
        )
        expired_candidate = and_(
            JobRow.job_type == "private_run",
            JobRow.cancel_requested_at.is_(None),
            stale_lease,
            JobRow.lease_owner_id.is_not(None),
            JobRow.lease_token_hash.is_not(None),
            active_attempt_exact,
            active_run_exact,
        )
        recoverable = and_(
            JobRow.retry_safety == "safe",
            JobRow.attempt_count < JobRow.max_attempts,
        )
        waiting_for_worker = and_(
            or_(
                and_(unowned_candidate, recoverable),
                and_(expired_candidate, recoverable),
            ),
            ~eligible_worker_exists,
        )
        waiting_for_terminalization = or_(
            and_(
                unowned_candidate,
                JobRow.attempt_count > 0,
                or_(
                    JobRow.retry_safety != "safe",
                    JobRow.attempt_count >= JobRow.max_attempts,
                ),
            ),
            and_(
                expired_candidate,
                or_(
                    JobRow.retry_safety != "safe",
                    JobRow.attempt_count >= JobRow.max_attempts,
                ),
            ),
        )
        oldest_ready_at = func.min(
            case((ready_job, JobRow.available_at), else_=None),
        )
        job_counts = (
            await self.session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            case((JobRow.status == "queued", 1), else_=0),
                        ),
                        0,
                    ).label("queued_jobs"),
                    func.coalesce(
                        func.sum(
                            case((JobRow.status == "running", 1), else_=0),
                        ),
                        0,
                    ).label("running_jobs"),
                    func.coalesce(
                        func.sum(
                            case((JobRow.status == "dead", 1), else_=0),
                        ),
                        0,
                    ).label("dead_jobs"),
                    func.coalesce(
                        func.sum(case((ready_job, 1), else_=0)),
                        0,
                    ).label("ready_jobs"),
                    case(
                        (oldest_ready_at.is_(None), None),
                        else_=func.greatest(
                            0,
                            func.floor(
                                func.extract(
                                    "epoch",
                                    clock.c.observed_at - oldest_ready_at,
                                )
                            ),
                        ),
                    ).label("oldest_ready_job_age_seconds"),
                    func.coalesce(
                        func.sum(case((stale_lease, 1), else_=0)),
                        0,
                    ).label("stale_leases"),
                    func.coalesce(
                        func.sum(case((waiting_for_worker, 1), else_=0)),
                        0,
                    ).label("waiting_for_worker_runs"),
                    func.coalesce(
                        func.sum(
                            case(
                                (waiting_for_terminalization, 1),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("waiting_for_terminalization_runs"),
                )
                .select_from(clock.outerjoin(JobRow, true()))
                .group_by(clock.c.observed_at)
            )
        ).one()
        return OperationsCounts(
            projects=int(project_counts[0]),
            suspended_projects=int(project_counts[1]),
            queued_jobs=int(job_counts.queued_jobs),
            running_jobs=int(job_counts.running_jobs),
            dead_jobs=int(job_counts.dead_jobs),
            ready_jobs=int(job_counts.ready_jobs),
            oldest_ready_job_age_seconds=(None if job_counts.oldest_ready_job_age_seconds is None else int(job_counts.oldest_ready_job_age_seconds)),
            stale_leases=int(job_counts.stale_leases),
            waiting_for_worker_runs=int(job_counts.waiting_for_worker_runs),
            waiting_for_terminalization_runs=int(job_counts.waiting_for_terminalization_runs),
        )

    async def list_projects(
        self,
        *,
        limit: int,
        cursor: str | None,
        query: str | None,
        status: ProjectStatus | None,
        suspended: bool | None,
        request_id: str,
    ) -> AdminProjectPage:
        selected_cursor = _decode_cursor(cursor, kind="project")
        statement = select(
            ProjectRow.id,
            ProjectRow.slug,
            ProjectRow.display_name,
            ProjectRow.status,
            ProjectRow.is_suspended,
            ProjectRow.membership_version,
            ProjectRow.created_at,
            ProjectRow.updated_at,
            ProjectRow.deletion_effective_at,
        )
        if selected_cursor is not None:
            statement = statement.where(ProjectRow.id < selected_cursor)
        if query is not None:
            normalized_query = query.strip().lower()
            if not normalized_query:
                raise ReliabilityInvalid(request_id)
            statement = statement.where(
                or_(
                    func.lower(ProjectRow.slug).contains(
                        normalized_query,
                        autoescape=True,
                    ),
                    func.lower(ProjectRow.display_name).contains(
                        normalized_query,
                        autoescape=True,
                    ),
                )
            )
        if status is not None:
            statement = statement.where(ProjectRow.status == status)
        if suspended is not None:
            statement = statement.where(ProjectRow.is_suspended.is_(suspended))
        rows = (await self.session.execute(statement.order_by(ProjectRow.id.desc()).limit(limit + 1))).all()
        page_rows = rows[:limit]
        return AdminProjectPage(
            items=tuple(
                AdminProjectRecord(
                    project_id=row.id,
                    slug=row.slug,
                    display_name=row.display_name,
                    status=row.status,
                    is_suspended=row.is_suspended,
                    state_version=row.membership_version,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    deletion_effective_at=row.deletion_effective_at,
                )
                for row in page_rows
            ),
            next_cursor=(
                _encode_cursor(
                    page_rows[-1].id,
                    kind="project",
                )
                if len(rows) > limit
                else None
            ),
        )

    async def get_project(
        self,
        project_id: uuid.UUID,
        *,
        request_id: str,
    ) -> AdminProjectRecord:
        row = (
            await self.session.execute(
                select(
                    ProjectRow.id,
                    ProjectRow.slug,
                    ProjectRow.display_name,
                    ProjectRow.status,
                    ProjectRow.is_suspended,
                    ProjectRow.membership_version,
                    ProjectRow.created_at,
                    ProjectRow.updated_at,
                    ProjectRow.deletion_effective_at,
                ).where(ProjectRow.id == project_id)
            )
        ).one_or_none()
        if row is None:
            raise ReliabilityNotFound(request_id)
        return AdminProjectRecord(
            project_id=row.id,
            slug=row.slug,
            display_name=row.display_name,
            status=row.status,
            is_suspended=row.is_suspended,
            state_version=row.membership_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            deletion_effective_at=row.deletion_effective_at,
        )

    async def list_jobs(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: uuid.UUID | None,
        project_query: str | None,
        status: JobStatus | None,
        job_type: JobType | None,
        request_id: str,
    ) -> AdminJobPage:
        selected_cursor = _decode_cursor(cursor, kind="job")
        successor = aliased(JobRow)
        statement = (
            select(
                JobRow.id,
                DeadJobRow.job_id.label("dead_job_id"),
                JobRow.project_id,
                ProjectRow.slug.label("project_slug"),
                ProjectRow.display_name.label("project_display_name"),
                JobRow.job_type,
                JobRow.status,
                JobRow.retry_safety,
                func.coalesce(
                    DeadJobRow.public_error_code,
                    JobRow.public_error_code,
                ).label("public_error_code"),
                JobRow.predecessor_dead_job_id,
                (~exists(select(successor.id).where(successor.predecessor_dead_job_id == JobRow.id))).label("has_no_successor"),
            )
            .join(
                ProjectRow,
                ProjectRow.id == JobRow.project_id,
            )
            .outerjoin(
                DeadJobRow,
                and_(
                    DeadJobRow.job_id == JobRow.id,
                    DeadJobRow.project_id == JobRow.project_id,
                ),
            )
        )
        if selected_cursor is not None:
            statement = statement.where(JobRow.id < selected_cursor)
        if project_id is not None:
            statement = statement.where(JobRow.project_id == project_id)
        if project_query is not None:
            normalized_query = project_query.strip().lower()
            if not normalized_query:
                raise ReliabilityInvalid(request_id)
            statement = statement.where(
                or_(
                    func.lower(ProjectRow.slug).contains(
                        normalized_query,
                        autoescape=True,
                    ),
                    func.lower(ProjectRow.display_name).contains(
                        normalized_query,
                        autoescape=True,
                    ),
                )
            )
        if status is not None:
            statement = statement.where(JobRow.status == status)
        if job_type is not None:
            statement = statement.where(JobRow.job_type == job_type)
        rows = (await self.session.execute(statement.order_by(JobRow.id.desc()).limit(limit + 1))).all()
        page_rows = rows[:limit]
        return AdminJobPage(
            items=tuple(_job_record(row) for row in page_rows),
            next_cursor=(
                _encode_cursor(
                    page_rows[-1].id,
                    kind="job",
                )
                if len(rows) > limit
                else None
            ),
        )

    async def public_job(
        self,
        *,
        project_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> AdminJobRecord | None:
        successor = aliased(JobRow)
        row = (
            await self.session.execute(
                select(
                    JobRow.id,
                    DeadJobRow.job_id.label("dead_job_id"),
                    JobRow.project_id,
                    ProjectRow.slug.label("project_slug"),
                    ProjectRow.display_name.label("project_display_name"),
                    JobRow.job_type,
                    JobRow.status,
                    JobRow.retry_safety,
                    func.coalesce(
                        DeadJobRow.public_error_code,
                        JobRow.public_error_code,
                    ).label("public_error_code"),
                    JobRow.predecessor_dead_job_id,
                    (~exists(select(successor.id).where(successor.predecessor_dead_job_id == JobRow.id))).label("has_no_successor"),
                )
                .join(
                    ProjectRow,
                    ProjectRow.id == JobRow.project_id,
                )
                .outerjoin(
                    DeadJobRow,
                    and_(
                        DeadJobRow.job_id == JobRow.id,
                        DeadJobRow.project_id == JobRow.project_id,
                    ),
                )
                .where(
                    JobRow.project_id == project_id,
                    JobRow.id == job_id,
                )
            )
        ).one_or_none()
        return None if row is None else _job_record(row)


def _job_record(row) -> AdminJobRecord:
    return AdminJobRecord(
        job_id=row.id,
        dead_job_id=row.dead_job_id,
        project_id=row.project_id,
        project_slug=row.project_slug,
        project_display_name=row.project_display_name,
        job_type=row.job_type,
        status=row.status,
        retry_safety=row.retry_safety,
        safe_to_requeue=(row.status == "dead" and row.dead_job_id is not None and row.retry_safety == "safe" and row.job_type == "retention_purge" and row.has_no_successor),
        public_error_code=row.public_error_code,
        predecessor_dead_job_id=row.predecessor_dead_job_id,
    )


def _encode_cursor(row_id: uuid.UUID, *, kind: Literal["project", "job"]) -> str:
    payload = {
        "v": 1,
        "k": kind,
        "i": str(row_id),
    }
    return (
        base64.urlsafe_b64encode(
            json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
        .decode("ascii")
        .rstrip("=")
    )


def _decode_cursor(
    value: str | None,
    *,
    kind: Literal["project", "job"],
) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("ascii"))
        if type(payload) is not dict or set(payload) != {"v", "k", "i"}:
            raise ValueError
        if payload["v"] != 1 or payload["k"] != kind:
            raise ValueError
        row_id = uuid.UUID(payload["i"])
        return row_id
    except (
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise AuditCursorRejected() from None


__all__ = [
    "AdminJobPage",
    "AdminJobRecord",
    "AdminProjectPage",
    "AdminProjectRecord",
    "ChannelProviderHealth",
    "JobStatus",
    "JobType",
    "OperationsOverview",
    "ProjectStatus",
    "SystemOperationsRepository",
    "safe_channel_provider_health",
    "resolve_current_system_audit_context",
]
