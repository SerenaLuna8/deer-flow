"""M6 durable job contracts and application admission helpers."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import (
    DeadJobRecord,
    DeadJobRequeuedEvent,
    EnqueueJob,
    JobAuditPort,
    JobClaim,
    JobHeartbeat,
    JobIdempotencyConflict,
    JobOwnerRef,
    JobOwnerRefRequired,
    JobRepository,
    JobRequeueForbidden,
    JobScope,
    JobType,
    RetrySafety,
)


@dataclass(frozen=True, slots=True)
class AdmittedJobRecord:
    job_id: uuid.UUID
    job_type: JobType
    project_id: uuid.UUID
    owner_user_id: str
    run_id: str
    idempotency_key: str
    status: str


def private_run_idempotency_key(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required")
    return hashlib.sha256(f"private_run:{run_id}".encode()).hexdigest()


def automation_run_idempotency_key(occurrence_id: str) -> str:
    if not isinstance(occurrence_id, str) or not occurrence_id:
        raise ValueError("occurrence_id is required")
    return hashlib.sha256(
        f"automation_run:{occurrence_id}".encode(),
    ).hexdigest()


class PrivateRunJobRepository:
    """Session-bound composition over the generic durable job state machine."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jobs = JobRepository(session)

    @staticmethod
    def _record(row: JobRow) -> AdmittedJobRecord:
        if row.owner_user_id is None or row.run_id is None:
            raise RuntimeError("private job authority is incomplete")
        return AdmittedJobRecord(
            job_id=row.id,
            job_type=row.job_type,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            run_id=row.run_id,
            idempotency_key=row.idempotency_key,
            status=row.status,
        )

    async def enqueue(
        self,
        *,
        scope: JobScope,
        run_id: str,
        max_attempts: int = 3,
    ) -> AdmittedJobRecord:
        key = private_run_idempotency_key(run_id)
        job_id = await self._jobs.enqueue(
            EnqueueJob(
                job_type="private_run",
                scope=scope,
                idempotency_key=key,
                run_id=run_id,
                occurrence_id=None,
                max_attempts=max_attempts,
                retry_safety="safe",
            )
        )
        row = (
            await self._session.execute(
                select(JobRow).where(
                    JobRow.id == job_id,
                    JobRow.job_type == "private_run",
                    JobRow.project_id == scope.project_id,
                    JobRow.owner_user_id == scope.owner_user_id,
                    JobRow.run_id == run_id,
                    JobRow.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise JobIdempotencyConflict("private job authority mismatch")
        return self._record(row)

    async def get(
        self,
        *,
        scope: JobScope,
        run_id: str,
        job_id: uuid.UUID,
        lock: bool = False,
    ) -> AdmittedJobRecord | None:
        statement = select(JobRow).where(
            JobRow.id == job_id,
            JobRow.job_type == "private_run",
            JobRow.project_id == scope.project_id,
            JobRow.owner_user_id == scope.owner_user_id,
            JobRow.run_id == run_id,
            JobRow.idempotency_key == private_run_idempotency_key(run_id),
        )
        if lock:
            statement = statement.with_for_update(of=JobRow)
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else self._record(row)


class AutomationRunJobRepository:
    """Session-bound admission for one occurrence-owned private Run job."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jobs = JobRepository(session)

    async def enqueue(
        self,
        *,
        scope: JobScope,
        run_id: str,
        occurrence_id: str,
        max_attempts: int = 3,
    ) -> AdmittedJobRecord:
        key = automation_run_idempotency_key(occurrence_id)
        job_id = await self._jobs.enqueue(
            EnqueueJob(
                job_type="automation_run",
                scope=scope,
                idempotency_key=key,
                run_id=run_id,
                occurrence_id=occurrence_id,
                max_attempts=max_attempts,
                retry_safety="safe",
            )
        )
        row = (
            await self._session.execute(
                select(JobRow).where(
                    JobRow.id == job_id,
                    JobRow.job_type == "automation_run",
                    JobRow.project_id == scope.project_id,
                    JobRow.owner_user_id == scope.owner_user_id,
                    JobRow.run_id == run_id,
                    JobRow.automation_occurrence_id == occurrence_id,
                    JobRow.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise JobIdempotencyConflict(
                "automation job authority mismatch",
            )
        return PrivateRunJobRepository._record(row)

    async def get(
        self,
        *,
        scope: JobScope,
        run_id: str,
        occurrence_id: str,
        job_id: uuid.UUID,
        lock: bool = False,
    ) -> AdmittedJobRecord | None:
        statement = select(JobRow).where(
            JobRow.id == job_id,
            JobRow.job_type == "automation_run",
            JobRow.project_id == scope.project_id,
            JobRow.owner_user_id == scope.owner_user_id,
            JobRow.run_id == run_id,
            JobRow.automation_occurrence_id == occurrence_id,
            JobRow.idempotency_key == automation_run_idempotency_key(occurrence_id),
        )
        if lock:
            statement = statement.with_for_update(of=JobRow)
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else PrivateRunJobRepository._record(row)


__all__ = [
    "DeadJobRecord",
    "DeadJobRequeuedEvent",
    "AdmittedJobRecord",
    "AutomationRunJobRepository",
    "EnqueueJob",
    "JobAuditPort",
    "JobClaim",
    "JobHeartbeat",
    "JobIdempotencyConflict",
    "JobOwnerRef",
    "JobOwnerRefRequired",
    "JobRepository",
    "JobRequeueForbidden",
    "JobScope",
    "JobType",
    "PrivateRunJobRepository",
    "RetrySafety",
    "automation_run_idempotency_key",
    "private_run_idempotency_key",
]
