"""Transaction-local authority for deleting an immutable Run closure."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.run.model import RunRow

_RESOURCE_KINDS = frozenset({"project", "former_owner", "account", "run"})
_TERMINAL_RUN_STATUSES = frozenset({"success", "error", "timeout", "interrupted", "deleted"})


class RetentionPurgeAuthorityConflict(RuntimeError):
    """The exact Run set is not closed and quiescent for physical deletion."""


@dataclass(frozen=True, slots=True)
class RetentionPurgeRun:
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class RetentionPurgeAuthority:
    """An exact Run-set capability installed only in the current transaction.

    PostgreSQL owns the enforcement.  The value object is useful for audit and
    tests, while the rows in ``pg_temp.retention_purge_run_authority`` are what
    the immediate Run-closure trigger accepts.  ``ON COMMIT DELETE ROWS`` makes
    the capability both connection-local and transaction-bounded.
    """

    purge_id: uuid.UUID
    resource_kind: str
    runs: tuple[RetentionPurgeRun, ...]

    @classmethod
    async def issue_verified_scope(
        cls,
        session: AsyncSession,
        *,
        purge_id: uuid.UUID,
        resource_kind: str,
        project_id: uuid.UUID | None,
        owner_user_id: str | None,
        project_ids: tuple[uuid.UUID, ...] = (),
        locked_runs: tuple[RunRow, ...],
    ) -> RetentionPurgeAuthority:
        """Issue after the caller has locked and revalidated purge eligibility."""

        if resource_kind not in {"project", "former_owner", "account"}:
            raise ValueError("invalid retention purge resource kind")
        if resource_kind == "project":
            if project_id is None or owner_user_id is not None or project_ids:
                raise ValueError("invalid project retention authority scope")
        elif resource_kind == "former_owner":
            if project_id is None or owner_user_id is None or project_ids:
                raise ValueError("invalid former-owner retention authority scope")
        else:
            if project_id is not None or owner_user_id is None or not project_ids:
                raise ValueError("invalid account retention authority scope")

        project_set = frozenset(project_ids)

        def in_scope(row: RunRow) -> bool:
            if resource_kind == "project":
                return row.project_id == project_id
            if resource_kind == "former_owner":
                return row.project_id == project_id and row.owner_user_id == owner_user_id
            return row.project_id in project_set and row.owner_user_id == owner_user_id

        if any(row.asset_closure_sealed is not True or row.status not in _TERMINAL_RUN_STATUSES or not in_scope(row) for row in locked_runs):
            raise RetentionPurgeAuthorityConflict
        return await cls._install(
            session,
            purge_id=purge_id,
            resource_kind=resource_kind,
            rows=locked_runs,
        )

    @classmethod
    async def issue_single_run(
        cls,
        session: AsyncSession,
        *,
        purge_id: uuid.UUID,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        now: datetime,
    ) -> RetentionPurgeAuthority:
        """Lock and authorize one terminal Run after its governance prefix."""

        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("single-Run retention clock must be timezone-aware")
        jobs = tuple(
            (
                await session.execute(
                    select(JobRow)
                    .where(
                        JobRow.project_id == project_id,
                        JobRow.owner_user_id == owner_user_id,
                        JobRow.run_id == run_id,
                    )
                    .order_by(JobRow.id)
                    .with_for_update(of=JobRow)
                )
            )
            .scalars()
            .all()
        )
        row = await session.scalar(
            select(RunRow)
            .where(
                RunRow.project_id == project_id,
                RunRow.owner_user_id == owner_user_id,
                RunRow.thread_id == thread_id,
                RunRow.run_id == run_id,
            )
            .with_for_update(of=RunRow)
        )
        if row is None or row.asset_closure_sealed is not True or row.status not in _TERMINAL_RUN_STATUSES or any(job.status in {"queued", "retry_wait", "leased", "running"} for job in jobs):
            raise RetentionPurgeAuthorityConflict
        active_attempt = False
        if jobs:
            active_attempt = bool(
                await session.scalar(
                    select(JobAttemptRow.id)
                    .where(
                        JobAttemptRow.job_id.in_(tuple(job.id for job in jobs)),
                        JobAttemptRow.outcome.is_(None),
                    )
                    .order_by(JobAttemptRow.job_id, JobAttemptRow.attempt_number)
                    .with_for_update(of=JobAttemptRow)
                    .limit(1)
                )
            )
        if active_attempt:
            raise RetentionPurgeAuthorityConflict
        return await cls._install(
            session,
            purge_id=purge_id,
            resource_kind="run",
            rows=(row,),
        )

    @classmethod
    async def _install(
        cls,
        session: AsyncSession,
        *,
        purge_id: uuid.UUID,
        resource_kind: str,
        rows: tuple[RunRow, ...],
    ) -> RetentionPurgeAuthority:
        if resource_kind not in _RESOURCE_KINDS:
            raise ValueError("invalid retention purge resource kind")
        if not session.in_transaction():
            raise RuntimeError("retention purge authority requires a transaction")
        await session.execute(
            text(
                """CREATE TEMP TABLE IF NOT EXISTS
                       retention_purge_run_authority (
                           purge_id uuid NOT NULL,
                           resource_kind varchar(24) NOT NULL,
                           project_id uuid NOT NULL,
                           owner_user_id varchar(36),
                           thread_id varchar(64) NOT NULL,
                           run_id varchar(64) NOT NULL,
                           PRIMARY KEY (purge_id, project_id, run_id),
                           CHECK (resource_kind IN
                                  ('project', 'former_owner', 'account', 'run')),
                           CHECK (
                               (resource_kind = 'project'
                                AND owner_user_id IS NULL)
                               OR
                               (resource_kind IN
                                    ('former_owner', 'account', 'run')
                                AND owner_user_id IS NOT NULL)
                           )
                       ) ON COMMIT DELETE ROWS"""
            )
        )
        runs = tuple(
            RetentionPurgeRun(
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                thread_id=row.thread_id,
                run_id=row.run_id,
            )
            for row in rows
        )
        if runs:
            await session.execute(
                text(
                    """INSERT INTO pg_temp.retention_purge_run_authority (
                           purge_id, resource_kind, project_id, owner_user_id,
                           thread_id, run_id
                       ) VALUES (
                           :purge_id, :resource_kind, :project_id,
                           :owner_user_id, :thread_id, :run_id
                       )
                       ON CONFLICT (purge_id, project_id, run_id) DO NOTHING"""
                ),
                [
                    {
                        "purge_id": purge_id,
                        "resource_kind": resource_kind,
                        "project_id": run.project_id,
                        "owner_user_id": (None if resource_kind == "project" else run.owner_user_id),
                        "thread_id": run.thread_id,
                        "run_id": run.run_id,
                    }
                    for run in runs
                ],
            )
        return cls(
            purge_id=purge_id,
            resource_kind=resource_kind,
            runs=runs,
        )


__all__ = [
    "RetentionPurgeAuthority",
    "RetentionPurgeAuthorityConflict",
    "RetentionPurgeRun",
]
