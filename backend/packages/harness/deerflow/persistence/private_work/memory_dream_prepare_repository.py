"""Durable state and lease fences for thread-scoped Dream preparation."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.memory_contract import (
    MemoryDocumentScope,
    MemoryDreamAdmissionRecord,
    MemoryDreamPrepareAdmission,
    MemoryDreamPrepareAdmissionDisposition,
    MemoryDreamPrepareConflict,
    MemoryDreamPrepareNotFound,
    MemoryDreamPreparePhase,
    MemoryDreamPrepareRecord,
    MemoryDreamPrepareResultDisposition,
    memory_dream_prepare_idempotency_key,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDreamPrepareRunRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow

_ACTIVE_JOB_STATUSES = ("queued", "leased", "running", "retry_wait")


class MemoryDreamPrepareRepository:
    """Session-bound preparation state machine; callers own commit/rollback."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self.session = session
        self.jobs = jobs or JobRepository(session)

    @staticmethod
    def _scope_predicates(scope: MemoryDocumentScope):
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        return (
            MemoryDreamPrepareRunRow.project_id == scope.project_id,
            MemoryDreamPrepareRunRow.owner_user_id == scope.owner_user_id,
            MemoryDreamPrepareRunRow.namespace == scope.namespace,
        )

    @staticmethod
    def _lease_hash(lease_token: str) -> str:
        if type(lease_token) is not str or not lease_token:
            raise ValueError("Dream preparation lease token is invalid")
        return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _record(row: MemoryDreamPrepareRunRow, job: JobRow) -> MemoryDreamPrepareRecord:
        phase = row.phase
        result_disposition = row.result_disposition
        if job.status == "cancelled":
            phase = "cancelled"
            result_disposition = "cancelled"
        elif job.status in {"failed", "dead"}:
            phase = "failed"
            result_disposition = "failed"
        return MemoryDreamPrepareRecord(
            job_id=uuid.UUID(str(row.job_id)),
            thread_id=row.thread_id,
            phase=phase,
            compacted_passes=int(row.compacted_passes),
            dream_job_id=(None if row.dream_job_id is None else uuid.UUID(str(row.dream_job_id))),
            history_count=(None if row.history_count is None else int(row.history_count)),
            admission_kind=row.admission_kind,
            result_disposition=result_disposition,
            job_status=job.status,
            public_error_code=job.public_error_code,
            cancel_requested=job.cancel_requested_at is not None,
            updated_at=row.updated_at,
        )

    async def _pair(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None,
        thread_id: str | None = None,
        active_only: bool = False,
        latest: bool = False,
    ) -> tuple[MemoryDreamPrepareRunRow, JobRow] | None:
        statement = (
            sa.select(MemoryDreamPrepareRunRow, JobRow)
            .join(
                JobRow,
                sa.and_(
                    JobRow.id == MemoryDreamPrepareRunRow.job_id,
                    JobRow.project_id == MemoryDreamPrepareRunRow.project_id,
                    JobRow.owner_user_id == MemoryDreamPrepareRunRow.owner_user_id,
                    JobRow.namespace == MemoryDreamPrepareRunRow.namespace,
                    JobRow.job_type == "memory_dream_prepare",
                ),
            )
            .where(*self._scope_predicates(scope))
        )
        if job_id is not None:
            statement = statement.where(MemoryDreamPrepareRunRow.job_id == job_id)
        if operation_id is not None:
            statement = statement.where(MemoryDreamPrepareRunRow.operation_id == operation_id)
        if thread_id is not None:
            statement = statement.where(MemoryDreamPrepareRunRow.thread_id == thread_id)
        if active_only:
            statement = statement.where(
                MemoryDreamPrepareRunRow.completed_at.is_(None),
                JobRow.status.in_(_ACTIVE_JOB_STATUSES),
            )
        if latest:
            statement = statement.order_by(
                MemoryDreamPrepareRunRow.updated_at.desc(),
                MemoryDreamPrepareRunRow.job_id.desc(),
            ).limit(1)
        return (await self.session.execute(statement)).one_or_none()

    async def _row(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None,
        thread_id: str | None = None,
        active_only: bool = False,
        for_update: bool = False,
    ) -> MemoryDreamPrepareRunRow | None:
        statement = sa.select(MemoryDreamPrepareRunRow).where(*self._scope_predicates(scope))
        if job_id is not None:
            statement = statement.where(MemoryDreamPrepareRunRow.job_id == job_id)
        if operation_id is not None:
            statement = statement.where(MemoryDreamPrepareRunRow.operation_id == operation_id)
        if thread_id is not None:
            statement = statement.where(MemoryDreamPrepareRunRow.thread_id == thread_id)
        if active_only:
            statement = statement.where(MemoryDreamPrepareRunRow.completed_at.is_(None))
        if for_update:
            statement = statement.with_for_update(of=MemoryDreamPrepareRunRow)
        return await self.session.scalar(statement)

    async def _job_for_row(
        self,
        scope: MemoryDocumentScope,
        row: MemoryDreamPrepareRunRow,
        *,
        for_update: bool = False,
    ) -> JobRow | None:
        statement = sa.select(JobRow).where(
            JobRow.id == row.job_id,
            JobRow.job_type == "memory_dream_prepare",
            JobRow.project_id == scope.project_id,
            JobRow.owner_user_id == scope.owner_user_id,
            JobRow.namespace == scope.namespace,
        )
        if for_update:
            statement = statement.with_for_update(of=JobRow)
        return await self.session.scalar(statement)

    async def _lock_thread(
        self,
        scope: MemoryDocumentScope,
        thread_id: str,
        *,
        require_live: bool,
    ) -> ThreadMetaRow:
        statement = sa.select(ThreadMetaRow).where(
            ThreadMetaRow.project_id == scope.project_id,
            ThreadMetaRow.owner_user_id == scope.owner_user_id,
            ThreadMetaRow.thread_id == thread_id,
        )
        if require_live:
            statement = statement.where(
                ThreadMetaRow.deleted_at.is_(None),
                ThreadMetaRow.frozen_at.is_(None),
            )
        thread = await self.session.scalar(statement.with_for_update(of=ThreadMetaRow))
        if thread is None:
            raise MemoryDreamPrepareNotFound
        return thread

    async def _lock_execution(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime,
    ) -> MemoryDreamPrepareRunRow:
        """Lock Thread -> preparation and validate the still-owned Job lease.

        Project and Membership are caller-owned locks.  The Job remains
        deliberately unlocked here: the following ``JobRepository`` transition
        is the single authority path that locks it after the preparation row.
        """

        preview = await self._row(scope, job_id=job_id)
        if preview is None:
            raise MemoryDreamPrepareNotFound
        await self._lock_thread(scope, preview.thread_id, require_live=False)
        row = await self._row(scope, job_id=job_id, for_update=True)
        if row is None:
            raise MemoryDreamPrepareNotFound
        job = await self._job_for_row(scope, row)
        if job is None:
            raise MemoryDreamPrepareNotFound
        if job.status not in {"leased", "running"} or job.lease_token_hash != self._lease_hash(lease_token) or job.lease_expires_at is None or job.lease_expires_at <= now:
            raise MemoryDreamPrepareConflict("Dream preparation lease changed")
        return row

    @staticmethod
    def _terminalize_stale(
        row: MemoryDreamPrepareRunRow,
        job: JobRow,
        *,
        now: datetime,
    ) -> bool:
        if row.completed_at is not None:
            return False
        if job.status == "cancelled":
            row.phase = "cancelled"
            row.result_disposition = "cancelled"
        elif job.status in {"failed", "dead"}:
            row.phase = "failed"
            row.result_disposition = "failed"
        else:
            return False
        row.completed_at = job.completed_at or now
        row.updated_at = max(row.updated_at, row.completed_at)
        return True

    async def admit(
        self,
        scope: MemoryDocumentScope,
        *,
        thread_id: str,
        operation_id: uuid.UUID,
        request_id: str,
        now: datetime,
        max_attempts: int = 3,
    ) -> MemoryDreamPrepareAdmission:
        if (
            type(thread_id) is not str
            or not thread_id
            or len(thread_id) > 64
            or not isinstance(operation_id, uuid.UUID)
            or type(request_id) is not str
            or not 1 <= len(request_id) <= 512
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= 20
        ):
            raise ValueError("Dream preparation admission input is invalid")

        # Project and Membership are locked by the service. Thread is the next
        # authority lock and serializes operation-id and active-thread checks.
        operation_preview = await self._row(scope, operation_id=operation_id)
        if operation_preview is not None and operation_preview.thread_id != thread_id:
            raise MemoryDreamPrepareConflict("Dream preparation operation belongs to another thread")
        await self._lock_thread(scope, thread_id, require_live=True)

        existing_row = await self._row(scope, operation_id=operation_id, for_update=True)
        if existing_row is not None:
            if existing_row.thread_id != thread_id:
                raise MemoryDreamPrepareConflict("Dream preparation operation belongs to another thread")
            existing_job = await self._job_for_row(scope, existing_row, for_update=True)
            if existing_job is None:
                raise MemoryDreamPrepareConflict("Dream preparation Job disappeared")
            self._terminalize_stale(existing_row, existing_job, now=now)
            return MemoryDreamPrepareAdmission(
                disposition="queued",
                record=self._record(existing_row, existing_job),
            )
        active_row = await self._row(
            scope,
            thread_id=thread_id,
            active_only=True,
            for_update=True,
        )
        if active_row is not None:
            active_job = await self._job_for_row(scope, active_row, for_update=True)
            if active_job is None:
                raise MemoryDreamPrepareConflict("Dream preparation Job disappeared")
            if not self._terminalize_stale(active_row, active_job, now=now):
                return MemoryDreamPrepareAdmission(
                    disposition="already_running",
                    record=self._record(active_row, active_job),
                )
            await self.session.flush()

        job_id = await self.jobs.enqueue(
            EnqueueJob(
                job_type="memory_dream_prepare",
                scope=JobScope(scope.project_id, scope.owner_user_id),
                namespace=scope.namespace,
                idempotency_key=memory_dream_prepare_idempotency_key(
                    scope,
                    operation_id,
                ),
                run_id=None,
                occurrence_id=None,
                max_attempts=max_attempts,
                retry_safety="safe",
                priority=10,
            )
        )
        row = MemoryDreamPrepareRunRow(
            job_id=job_id,
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            thread_id=thread_id,
            operation_id=operation_id,
            request_id=request_id,
            phase="queued",
            compacted_passes=0,
            result_disposition="queued",
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        job = await self.session.get(JobRow, job_id)
        if job is None:
            raise MemoryDreamPrepareConflict("Dream preparation Job disappeared")
        return MemoryDreamPrepareAdmission(
            disposition="queued",
            record=self._record(row, job),
        )

    async def read(
        self,
        scope: MemoryDocumentScope,
        job_id: uuid.UUID,
    ) -> MemoryDreamPrepareRecord:
        pair = await self._pair(scope, job_id=job_id)
        if pair is None:
            raise MemoryDreamPrepareNotFound
        return self._record(*pair)

    async def read_latest(
        self,
        scope: MemoryDocumentScope,
        *,
        thread_id: str,
    ) -> MemoryDreamPrepareRecord:
        pair = await self._pair(scope, thread_id=thread_id, latest=True)
        if pair is None:
            raise MemoryDreamPrepareNotFound
        return self._record(*pair)

    async def read_execution(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime,
    ) -> MemoryDreamPrepareRunRow:
        pair = await self._pair(scope, job_id=job_id)
        if pair is None:
            raise MemoryDreamPrepareNotFound
        row, job = pair
        if job.status not in {"leased", "running"} or job.lease_token_hash != self._lease_hash(lease_token) or job.lease_expires_at is None or job.lease_expires_at <= now:
            raise MemoryDreamPrepareConflict("Dream preparation lease changed")
        return row

    async def set_phase(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        phase: Literal["draining", "verifying"],
        now: datetime,
    ) -> None:
        token_hash = self._lease_hash(lease_token)
        result = await self.session.execute(
            sa.update(MemoryDreamPrepareRunRow)
            .where(
                *self._scope_predicates(scope),
                MemoryDreamPrepareRunRow.job_id == job_id,
                MemoryDreamPrepareRunRow.completed_at.is_(None),
                sa.exists(
                    sa.select(JobRow.id).where(
                        JobRow.id == job_id,
                        JobRow.job_type == "memory_dream_prepare",
                        JobRow.project_id == scope.project_id,
                        JobRow.owner_user_id == scope.owner_user_id,
                        JobRow.namespace == scope.namespace,
                        JobRow.status.in_(("leased", "running")),
                        JobRow.lease_token_hash == token_hash,
                        JobRow.lease_expires_at > now,
                    )
                ),
            )
            .values(phase=phase, updated_at=now)
        )
        if result.rowcount != 1:
            raise MemoryDreamPrepareConflict("Dream preparation lease changed")

    async def record_pass(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        checkpoint_id: str,
        now: datetime,
    ) -> None:
        if type(checkpoint_id) is not str or not checkpoint_id or len(checkpoint_id) > 128:
            raise ValueError("Dream preparation checkpoint is invalid")
        token_hash = self._lease_hash(lease_token)
        result = await self.session.execute(
            sa.update(MemoryDreamPrepareRunRow)
            .where(
                *self._scope_predicates(scope),
                MemoryDreamPrepareRunRow.job_id == job_id,
                MemoryDreamPrepareRunRow.completed_at.is_(None),
                sa.or_(
                    MemoryDreamPrepareRunRow.last_checkpoint_id.is_(None),
                    MemoryDreamPrepareRunRow.last_checkpoint_id != checkpoint_id,
                ),
                sa.exists(
                    sa.select(JobRow.id).where(
                        JobRow.id == job_id,
                        JobRow.job_type == "memory_dream_prepare",
                        JobRow.project_id == scope.project_id,
                        JobRow.owner_user_id == scope.owner_user_id,
                        JobRow.namespace == scope.namespace,
                        JobRow.status.in_(("leased", "running")),
                        JobRow.lease_token_hash == token_hash,
                        JobRow.lease_expires_at > now,
                    )
                ),
            )
            .values(
                phase="draining",
                compacted_passes=MemoryDreamPrepareRunRow.compacted_passes + 1,
                last_checkpoint_id=checkpoint_id,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            current = await self.read_execution(
                scope,
                job_id=job_id,
                lease_token=lease_token,
                now=now,
            )
            if current.last_checkpoint_id == checkpoint_id:
                return
            raise MemoryDreamPrepareConflict("Dream preparation progress did not advance")

    async def link_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        admitted: MemoryDreamAdmissionRecord,
        now: datetime,
    ) -> None:
        token_hash = self._lease_hash(lease_token)
        values: dict[str, object] = {
            "phase": "dream_admitted",
            "dream_job_id": admitted.job_id,
            "history_count": admitted.history_count,
            "admission_kind": (admitted.admission_kind if admitted.job_id is not None else None),
            "result_disposition": admitted.disposition,
            "updated_at": now,
        }
        result = await self.session.execute(
            sa.update(MemoryDreamPrepareRunRow)
            .where(
                *self._scope_predicates(scope),
                MemoryDreamPrepareRunRow.job_id == job_id,
                MemoryDreamPrepareRunRow.completed_at.is_(None),
                sa.exists(
                    sa.select(JobRow.id).where(
                        JobRow.id == job_id,
                        JobRow.job_type == "memory_dream_prepare",
                        JobRow.project_id == scope.project_id,
                        JobRow.owner_user_id == scope.owner_user_id,
                        JobRow.namespace == scope.namespace,
                        JobRow.status.in_(("leased", "running")),
                        JobRow.lease_token_hash == token_hash,
                        JobRow.lease_expires_at > now,
                    )
                ),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise MemoryDreamPrepareConflict("Dream preparation lease changed")

    async def settle_success(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime,
    ) -> None:
        row = await self._lock_execution(
            scope,
            job_id=job_id,
            lease_token=lease_token,
            now=now,
        )
        if (
            row.phase != "dream_admitted"
            or (row.dream_job_id is None and not (row.result_disposition == "nothing_pending" and row.history_count == 0 and row.admission_kind is None))
            or (row.dream_job_id is not None and row.result_disposition not in {"queued", "already_running"})
        ):
            raise MemoryDreamPrepareConflict("Dream preparation has no result")
        if not await self.jobs.settle_success(job_id, lease_token=lease_token, now=now):
            raise MemoryDreamPrepareConflict("Dream preparation lease changed")
        row.phase = "succeeded"
        row.completed_at = now
        row.updated_at = now
        await self.session.flush()

    async def request_cancel(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        reason: str,
        now: datetime,
    ) -> MemoryDreamPrepareRecord:
        """Request cancellation and atomically terminalize an unowned prepare.

        The preliminary read reveals only owner-scoped state.  The authoritative
        mutation follows the global Project/Membership -> Thread -> preparation
        row -> Job order; callers already hold Project and Membership.
        """

        preview = await self._row(scope, job_id=job_id)
        if preview is None:
            raise MemoryDreamPrepareNotFound
        await self._lock_thread(scope, preview.thread_id, require_live=False)
        row = await self._row(scope, job_id=job_id, for_update=True)
        if row is None:
            raise MemoryDreamPrepareNotFound
        job = await self._job_for_row(scope, row)
        if job is None:
            raise MemoryDreamPrepareNotFound
        if job.status not in _ACTIVE_JOB_STATUSES:
            return self._record(row, job)

        # This explicit second locking statement makes the Prepare -> Job suffix
        # deterministic; a joined FOR UPDATE cannot promise table lock order.
        job = await self._job_for_row(scope, row, for_update=True)
        if job is None:
            raise MemoryDreamPrepareNotFound
        if job.status not in _ACTIVE_JOB_STATUSES:
            return self._record(row, job)

        job_scope = JobScope(scope.project_id, scope.owner_user_id)
        if not await self.jobs.request_cancel(
            job_scope,
            job_id,
            reason=reason,
            now=now,
        ):
            raise MemoryDreamPrepareConflict("Dream preparation changed")
        if job.status in {"queued", "retry_wait"}:
            if not await self.jobs.settle_requested_cancel(
                job_scope,
                job_id,
                now=now,
            ):
                raise MemoryDreamPrepareConflict("Dream preparation changed")
            row.phase = "cancelled"
            row.result_disposition = "cancelled"
            row.completed_at = now
            row.updated_at = now
            await self.session.flush()
        return self._record(row, job)

    async def retry_or_dead(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        public_error_code: str,
        retry_initial_seconds: int,
        retry_max_seconds: int,
        now: datetime,
    ) -> None:
        """Retry safely or close both Job and preparation on terminal failure."""

        row = await self._lock_execution(
            scope,
            job_id=job_id,
            lease_token=lease_token,
            now=now,
        )
        result = await self.jobs.retry_or_dead_result(
            job_id,
            lease_token=lease_token,
            public_error_code=public_error_code,
            retry_initial_seconds=retry_initial_seconds,
            retry_max_seconds=retry_max_seconds,
            now=now,
        )
        if not result.changed:
            raise MemoryDreamPrepareConflict("Dream preparation lease changed")
        job = await self._job_for_row(scope, row)
        if job is None:
            raise MemoryDreamPrepareConflict("Dream preparation disappeared")
        if job.status == "dead":
            row.phase = "failed"
            row.result_disposition = "failed"
            row.completed_at = now
            row.updated_at = now
            await self.session.flush()

    async def settle_cancelled(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime,
    ) -> None:
        row = await self._lock_execution(
            scope,
            job_id=job_id,
            lease_token=lease_token,
            now=now,
        )
        if not await self.jobs.settle_cancelled(job_id, lease_token=lease_token, now=now):
            raise MemoryDreamPrepareConflict("Dream preparation lease changed")
        row.phase = "cancelled"
        row.result_disposition = "cancelled"
        row.completed_at = now
        row.updated_at = now
        await self.session.flush()


__all__ = [
    "MemoryDreamPrepareAdmission",
    "MemoryDreamPrepareAdmissionDisposition",
    "MemoryDreamPrepareConflict",
    "MemoryDreamPrepareNotFound",
    "MemoryDreamPreparePhase",
    "MemoryDreamPrepareRecord",
    "MemoryDreamPrepareRepository",
    "MemoryDreamPrepareResultDisposition",
    "memory_dream_prepare_idempotency_key",
]
