from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.execution_profile import RequestedRunExecutionProfile
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.jobs.sql import JobRepository, JobScope
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.trace_context import generate_trace_id, normalize_trace_id


@dataclass(frozen=True, slots=True)
class PrivateRunCreate:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    assistant_id: str | None = None
    status: str = "pending"
    multitask_strategy: str = "reject"
    metadata: dict[str, Any] = field(default_factory=dict)
    kwargs: dict[str, Any] = field(default_factory=dict)
    model_name: str | None = None
    origin_trace_id: str = field(default_factory=generate_trace_id)
    follow_up_to_run_id: str | None = None
    execution_profile: RequestedRunExecutionProfile = field(
        default_factory=RequestedRunExecutionProfile,
    )

    def __post_init__(self) -> None:
        normalized = normalize_trace_id(self.origin_trace_id)
        if normalized is None:
            raise ValueError("origin_trace_id is invalid")
        object.__setattr__(self, "origin_trace_id", normalized)
        if self.follow_up_to_run_id is not None and (not isinstance(self.follow_up_to_run_id, str) or not self.follow_up_to_run_id or len(self.follow_up_to_run_id) > 64 or self.follow_up_to_run_id == self.run_id):
            raise ValueError("follow_up_to_run_id is invalid")
        if type(self.execution_profile) is not RequestedRunExecutionProfile:
            raise TypeError(
                "execution_profile must be RequestedRunExecutionProfile",
            )


@dataclass(frozen=True, slots=True)
class PrivateRunRecord:
    run_id: str
    thread_id: str
    project_id: uuid.UUID
    owner_user_id: str
    assistant_id: str | None
    status: str
    multitask_strategy: str
    metadata: dict[str, Any]
    kwargs: dict[str, Any]
    origin_trace_id: str
    error: str | None
    model_name: str | None
    created_at: datetime
    updated_at: datetime
    follow_up_to_run_id: str | None = None
    job_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PrivateRunExecutionState:
    run: PrivateRunRecord
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class PrivateRunSettlement:
    run: PrivateRunRecord
    run_terminal_published: bool


@dataclass(frozen=True, slots=True)
class PrivateRunUsageSnapshot:
    """Attempt-local token usage produced by the Worker Run journal."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    token_usage_by_model: dict[str, dict[str, int]] = field(
        default_factory=dict,
    )

    def __post_init__(self) -> None:
        for field_name in (
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "llm_call_count",
            "lead_agent_tokens",
            "subagent_tokens",
            "middleware_tokens",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise TypeError(
                    f"{field_name} must be a non-negative integer",
                )
        normalized: dict[str, dict[str, int]] = {}
        if not isinstance(self.token_usage_by_model, Mapping):
            raise TypeError("token_usage_by_model must be a mapping")
        for model_name, raw_usage in self.token_usage_by_model.items():
            if type(model_name) is not str or not model_name:
                raise TypeError("token usage model names must be non-empty strings")
            if not isinstance(raw_usage, Mapping):
                raise TypeError("per-model token usage must be a mapping")
            usage: dict[str, int] = {}
            for counter_name, counter_value in raw_usage.items():
                if type(counter_name) is not str or not counter_name:
                    raise TypeError(
                        "token usage counter names must be non-empty strings",
                    )
                if type(counter_value) is not int or counter_value < 0:
                    raise TypeError(
                        "per-model token usage counters must be non-negative integers",
                    )
                usage[counter_name] = counter_value
            normalized[model_name] = usage
        object.__setattr__(self, "token_usage_by_model", normalized)


class PrivateRunConflict(Exception):
    """Session-bound invariant failure; public boundaries supply request IDs."""


class PrivateRunExecutionLeaseLost(PrivateRunConflict):
    """The supplied durable job token cannot mutate the scoped Run."""


class PrivateRunRepository:
    """Session-bound run repository whose every statement carries private scope."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self.session = session
        self.jobs = jobs or JobRepository(session)

    @staticmethod
    def _lease_token_hash(lease_token: str) -> str:
        if not isinstance(lease_token, str) or not lease_token:
            raise PrivateRunExecutionLeaseLost
        return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()

    @staticmethod
    def coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise PrivateRunConflict
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise PrivateRunConflict from None

    @classmethod
    def predicates(cls, scope: PrivateResourceScope):
        project_id, owner_user_id = cls.coordinates(scope)
        return (
            RunRow.project_id == project_id,
            RunRow.owner_user_id == owner_user_id,
        )

    @staticmethod
    def record(row: RunRow) -> PrivateRunRecord:
        return PrivateRunRecord(
            run_id=row.run_id,
            thread_id=row.thread_id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            assistant_id=row.assistant_id,
            status=row.status,
            multitask_strategy=row.multitask_strategy,
            metadata=dict(row.metadata_json or {}),
            kwargs=dict(row.kwargs_json or {}),
            origin_trace_id=row.origin_trace_id,
            error=row.error,
            model_name=row.model_name,
            created_at=row.created_at,
            updated_at=row.updated_at,
            follow_up_to_run_id=row.follow_up_to_run_id,
            job_id=row.job_id,
        )

    async def create(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        request: PrivateRunCreate,
    ) -> PrivateRunRecord:
        project_id, owner_user_id = self.coordinates(scope)
        thread_exists = (
            await self.session.execute(
                select(ThreadMetaRow.thread_id).where(
                    ThreadMetaRow.thread_id == thread_id,
                    ThreadMetaRow.project_id == project_id,
                    ThreadMetaRow.owner_user_id == owner_user_id,
                    ThreadMetaRow.deleted_at.is_(None),
                    ThreadMetaRow.frozen_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if thread_exists is None:
            raise PrivateRunConflict
        now = datetime.now(UTC)
        row = RunRow(
            run_id=request.run_id,
            thread_id=thread_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            assistant_id=request.assistant_id,
            status=request.status,
            multitask_strategy=request.multitask_strategy,
            metadata_json=dict(request.metadata),
            kwargs_json=dict(request.kwargs),
            origin_trace_id=request.origin_trace_id,
            model_name=request.model_name,
            follow_up_to_run_id=request.follow_up_to_run_id,
            created_at=now,
            updated_at=now,
        )
        try:
            self.session.add(row)
            await self.session.flush()
        except IntegrityError:
            raise PrivateRunConflict from None
        return self.record(row)

    async def has_conflicting_active_run(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
    ) -> bool:
        """Lock and detect pending/running runs for one private Thread."""

        row = (
            await self.session.execute(
                select(RunRow.run_id)
                .where(
                    RunRow.thread_id == thread_id,
                    *self.predicates(scope),
                    RunRow.status.in_(("pending", "running")),
                )
                .order_by(RunRow.created_at, RunRow.run_id)
                .with_for_update(of=RunRow)
                .limit(1)
            )
        ).scalar_one_or_none()
        return row is not None

    async def get(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        lock: bool = False,
    ) -> PrivateRunRecord | None:
        statement = select(RunRow).where(
            RunRow.run_id == run_id,
            *self.predicates(scope),
            RunRow.status != "deleted",
        )
        if lock:
            statement = statement.with_for_update(of=RunRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def attach_job(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
    ) -> PrivateRunRecord:
        """Attach the exact durable job once under private Run authority."""

        if not isinstance(job_id, uuid.UUID):
            raise PrivateRunConflict
        row = (
            await self.session.execute(
                select(RunRow)
                .where(
                    RunRow.run_id == run_id,
                    *self.predicates(scope),
                )
                .with_for_update(of=RunRow)
            )
        ).scalar_one_or_none()
        if row is None or (row.job_id is not None and row.job_id != job_id):
            raise PrivateRunConflict
        row.job_id = job_id
        row.updated_at = datetime.now(UTC)
        await self.session.flush()
        # The canonical PostgreSQL schema owns ``updated_at`` through
        # ``trg_runs_updated_at``. Refresh the trigger-produced value so an
        # admitted response is byte-for-byte stable with an idempotent retry.
        await self.session.refresh(row, attribute_names=["updated_at"])
        return self.record(row)

    async def _locked_job_run(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
    ) -> tuple[JobRow, RunRow]:
        project_id, owner_user_id = self.coordinates(scope)
        job = (
            await self.session.execute(
                select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.job_type.in_(("private_run", "automation_run")),
                    JobRow.project_id == project_id,
                    JobRow.owner_user_id == owner_user_id,
                    JobRow.run_id == run_id,
                    sa.or_(
                        sa.and_(
                            JobRow.job_type == "private_run",
                            JobRow.automation_occurrence_id.is_(None),
                        ),
                        sa.and_(
                            JobRow.job_type == "automation_run",
                            JobRow.automation_occurrence_id.is_not(None),
                        ),
                    ),
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        if job is None:
            raise PrivateRunExecutionLeaseLost
        run = (
            await self.session.execute(
                select(RunRow)
                .where(
                    RunRow.run_id == run_id,
                    RunRow.job_id == job_id,
                    *self.predicates(scope),
                )
                .with_for_update(of=RunRow)
            )
        ).scalar_one_or_none()
        if run is None:
            raise PrivateRunExecutionLeaseLost
        if normalize_trace_id(job.origin_trace_id) is None or job.origin_trace_id != run.origin_trace_id:
            raise PrivateRunExecutionLeaseLost
        return job, run

    @staticmethod
    def _active_job_lease(
        job: JobRow,
        *,
        token_hash: str,
        now: datetime,
    ) -> bool:
        return job.status == "running" and job.lease_token_hash == token_hash and job.lease_expires_at is not None and job.lease_expires_at > now

    async def begin_execution(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
        lease_token: str,
        origin_trace_id: str | None = None,
        now: datetime | None = None,
    ) -> PrivateRunExecutionState:
        started_at = now or datetime.now(UTC)
        token_hash = self._lease_token_hash(lease_token)
        job, run = await self._locked_job_run(
            scope=scope,
            run_id=run_id,
            job_id=job_id,
        )
        if origin_trace_id is not None and (normalize_trace_id(origin_trace_id) is None or origin_trace_id != run.origin_trace_id):
            raise PrivateRunExecutionLeaseLost
        if not self._active_job_lease(job, token_hash=token_hash, now=started_at):
            raise PrivateRunExecutionLeaseLost
        if run.status not in {"pending", "running"}:
            raise PrivateRunConflict
        if run.execution_lease_token_hash not in {None, token_hash} and (run.execution_lease_expires_at is None or run.execution_lease_expires_at > started_at):
            raise PrivateRunExecutionLeaseLost
        cancel_requested = any(
            value is not None
            for value in (
                job.cancel_requested_at,
                run.cancel_requested_at,
                run.authorization_cancel_requested_at,
            )
        )
        run.status = "running"
        run.execution_lease_token_hash = token_hash
        run.execution_lease_expires_at = job.lease_expires_at
        run.execution_heartbeat_at = job.heartbeat_at or started_at
        run.execution_started_at = run.execution_started_at or started_at
        run.updated_at = started_at
        await self.session.flush()
        return PrivateRunExecutionState(
            run=self.record(run),
            cancel_requested=cancel_requested,
        )

    async def prepare_checkpoint_takeover(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: str,
        latest_checkpoint_id: str | None,
        now: datetime | None = None,
    ) -> bool:
        """Record this attempt's baseline and decide whether to resume latest.

        A later attempt resumes without replaying its original input/Command
        once the durable checkpoint advances beyond the Job's first-attempt
        baseline. Comparing every takeover with that stable baseline keeps all
        later attempts in resume mode even when no additional progress occurs.
        """

        checked_at = now or datetime.now(UTC)
        if latest_checkpoint_id is not None and (not latest_checkpoint_id or len(latest_checkpoint_id) > 128):
            raise PrivateRunConflict
        token_hash = self._lease_token_hash(lease_token)
        job, run = await self._locked_job_run(
            scope=scope,
            run_id=run_id,
            job_id=job_id,
        )
        if (
            not self._active_job_lease(
                job,
                token_hash=token_hash,
                now=checked_at,
            )
            or run.status != "running"
            or run.execution_lease_token_hash != token_hash
        ):
            raise PrivateRunExecutionLeaseLost
        attempt = (
            await self.session.execute(
                select(JobAttemptRow)
                .where(
                    JobAttemptRow.id == attempt_id,
                    JobAttemptRow.job_id == job_id,
                    JobAttemptRow.lease_token_hash == token_hash,
                    JobAttemptRow.outcome.is_(None),
                )
                .with_for_update(of=JobAttemptRow)
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise PrivateRunExecutionLeaseLost
        baseline = (
            await self.session.execute(
                select(
                    JobAttemptRow.id,
                    JobAttemptRow.checkpoint_cursor,
                )
                .where(
                    JobAttemptRow.job_id == job_id,
                    JobAttemptRow.attempt_number < attempt.attempt_number,
                )
                .order_by(JobAttemptRow.attempt_number.asc())
                .limit(1)
            )
        ).one_or_none()
        attempt.checkpoint_cursor = latest_checkpoint_id
        await self.session.flush()
        return baseline is not None and latest_checkpoint_id is not None and latest_checkpoint_id != baseline.checkpoint_cursor

    async def heartbeat_execution(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime | None = None,
    ) -> None:
        heartbeat_at = now or datetime.now(UTC)
        token_hash = self._lease_token_hash(lease_token)
        job, run = await self._locked_job_run(
            scope=scope,
            run_id=run_id,
            job_id=job_id,
        )
        if (
            not self._active_job_lease(
                job,
                token_hash=token_hash,
                now=heartbeat_at,
            )
            or run.status != "running"
            or run.execution_lease_token_hash != token_hash
        ):
            raise PrivateRunExecutionLeaseLost
        run.execution_lease_expires_at = job.lease_expires_at
        run.execution_heartbeat_at = job.heartbeat_at or heartbeat_at
        run.updated_at = heartbeat_at
        await self.session.flush()

    async def assert_execution_active(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime | None = None,
    ) -> bool:
        """Validate the current job/run lease without extending it.

        Runtime side-effect boundaries call this read-only check immediately
        before model, tool, MCP, sandbox, checkpoint, and file operations.  A
        stale worker therefore cannot regain authority merely by reaching a
        side-effect hook after its heartbeat loop has lost ownership.
        """

        checked_at = now or datetime.now(UTC)
        token_hash = self._lease_token_hash(lease_token)
        job, run = await self._locked_job_run(
            scope=scope,
            run_id=run_id,
            job_id=job_id,
        )
        if (
            not self._active_job_lease(
                job,
                token_hash=token_hash,
                now=checked_at,
            )
            or run.status != "running"
            or run.execution_lease_token_hash != token_hash
            or run.execution_lease_expires_at is None
            or run.execution_lease_expires_at <= checked_at
        ):
            raise PrivateRunExecutionLeaseLost
        return any(
            value is not None
            for value in (
                job.cancel_requested_at,
                run.cancel_requested_at,
                run.authorization_cancel_requested_at,
            )
        )

    async def stream_cleanup_allowed(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
    ) -> bool:
        """Allow delayed bridge cleanup only after this logical Run ends."""

        job, run = await self._locked_job_run(
            scope=scope,
            run_id=run_id,
            job_id=job_id,
        )
        return (job.status, run.status) in {
            ("succeeded", "success"),
            ("cancelled", "interrupted"),
            ("dead", "error"),
        }

    async def mark_execution_side_effect_unknown(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime | None = None,
    ) -> bool:
        """Persist the unsafe replay boundary before an external side effect."""

        checked_at = now or datetime.now(UTC)
        token_hash = self._lease_token_hash(lease_token)
        job, run = await self._locked_job_run(
            scope=scope,
            run_id=run_id,
            job_id=job_id,
        )
        if (
            not self._active_job_lease(
                job,
                token_hash=token_hash,
                now=checked_at,
            )
            or run.status != "running"
            or run.execution_lease_token_hash != token_hash
            or run.execution_lease_expires_at is None
            or run.execution_lease_expires_at <= checked_at
        ):
            raise PrivateRunExecutionLeaseLost
        cancel_requested = any(
            value is not None
            for value in (
                job.cancel_requested_at,
                run.cancel_requested_at,
                run.authorization_cancel_requested_at,
            )
        )
        if not cancel_requested and job.retry_safety == "safe":
            job.retry_safety = "unknown"
            job.updated_at = checked_at
            await self.session.flush()
        return cancel_requested

    async def settle_execution(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        job_id: uuid.UUID,
        lease_token: str,
        outcome: Literal["succeeded", "cancelled", "failed"],
        public_error_code: str | None = None,
        ambiguous_side_effect: bool = False,
        retryable_failure: bool = True,
        cancel_preempts_outcome: bool = True,
        retry_initial_seconds: int = 2,
        retry_max_seconds: int = 300,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
        now: datetime | None = None,
    ) -> PrivateRunSettlement:
        if type(cancel_preempts_outcome) is not bool:
            raise PrivateRunConflict
        if type(retryable_failure) is not bool:
            raise PrivateRunConflict
        if attempt_usage is not None and type(attempt_usage) is not PrivateRunUsageSnapshot:
            raise PrivateRunConflict
        settled_at = now or datetime.now(UTC)
        token_hash = self._lease_token_hash(lease_token)
        job, run = await self._locked_job_run(
            scope=scope,
            run_id=run_id,
            job_id=job_id,
        )
        terminal_pair = {
            "succeeded": ("success", "succeeded"),
            "cancelled": ("interrupted", "cancelled"),
        }.get(outcome)
        if terminal_pair is not None and (run.status, job.status) == terminal_pair:
            return PrivateRunSettlement(
                run=self.record(run),
                run_terminal_published=True,
            )
        if (
            not self._active_job_lease(
                job,
                token_hash=token_hash,
                now=settled_at,
            )
            or run.execution_lease_token_hash != token_hash
        ):
            raise PrivateRunExecutionLeaseLost

        cancel_requested = any(
            value is not None
            for value in (
                job.cancel_requested_at,
                run.cancel_requested_at,
                run.authorization_cancel_requested_at,
            )
        )
        if cancel_requested and cancel_preempts_outcome:
            outcome = "cancelled"
            public_error_code = None

        run_terminal_published = False
        if outcome == "succeeded":
            changed = await self.jobs.settle_success(
                job_id,
                lease_token=lease_token,
                now=settled_at,
            )
            run.status = "success"
            run.error = None
        elif outcome == "cancelled":
            changed = await self.jobs.settle_cancelled(
                job_id,
                lease_token=lease_token,
                now=settled_at,
            )
            run.status = "interrupted"
            run.error = run.authorization_cancel_reason or run.cancel_reason
        else:
            if not public_error_code:
                raise PrivateRunConflict
            if ambiguous_side_effect:
                job.retry_safety = "unknown"
                await self.session.flush()
            job_result = await self.jobs.retry_or_dead_result(
                job_id,
                lease_token=lease_token,
                public_error_code=public_error_code,
                retryable=retryable_failure,
                retry_initial_seconds=retry_initial_seconds,
                retry_max_seconds=retry_max_seconds,
                now=settled_at,
            )
            changed = job_result.changed
            run_terminal_published = job_result.run_terminal_published
            if job.status == "retry_wait":
                run.status = "pending"
                run.error = None
            else:
                run.status = "error"
                run.error = job.public_error_code or public_error_code
        if not changed:
            raise PrivateRunExecutionLeaseLost
        if attempt_usage is not None:
            # A logical Run may span multiple retry attempts. Each journal
            # snapshot starts at zero, so settlement must add rather than replace.
            run.total_input_tokens += attempt_usage.total_input_tokens
            run.total_output_tokens += attempt_usage.total_output_tokens
            run.total_tokens += attempt_usage.total_tokens
            run.llm_call_count += attempt_usage.llm_call_count
            run.lead_agent_tokens += attempt_usage.lead_agent_tokens
            run.subagent_tokens += attempt_usage.subagent_tokens
            run.middleware_tokens += attempt_usage.middleware_tokens
            by_model = {model_name: dict(usage) for model_name, usage in (run.token_usage_by_model or {}).items()}
            for model_name, usage in attempt_usage.token_usage_by_model.items():
                model_totals = by_model.setdefault(model_name, {})
                for counter_name, counter_value in usage.items():
                    model_totals[counter_name] = model_totals.get(counter_name, 0) + counter_value
            run.token_usage_by_model = by_model
        run.execution_lease_token_hash = None
        run.execution_lease_expires_at = None
        run.execution_heartbeat_at = None
        run.updated_at = settled_at
        await self.session.flush()
        return PrivateRunSettlement(
            run=self.record(run),
            run_terminal_published=run_terminal_published,
        )

    async def request_cancel(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        job_id: uuid.UUID,
        reason: str,
        now: datetime | None = None,
    ) -> Literal["requested", "cancelled", "terminal"]:
        requested_at = now or datetime.now(UTC)
        job, run = await self._locked_job_run(
            scope=scope,
            run_id=run_id,
            job_id=job_id,
        )
        if run.thread_id != thread_id:
            raise PrivateRunConflict
        if run.status == "interrupted":
            return "terminal"
        if run.status in {"success", "error", "timeout"}:
            raise PrivateRunConflict
        job_scope = JobScope(job.project_id, job.owner_user_id)
        if not await self.jobs.request_cancel(
            job_scope,
            job_id,
            reason=reason,
            now=requested_at,
        ):
            raise PrivateRunConflict
        run.cancel_requested_at = run.cancel_requested_at or requested_at
        run.cancel_reason = run.cancel_reason or reason
        if await self.jobs.settle_requested_cancel(
            job_scope,
            job_id,
            now=requested_at,
        ):
            run.status = "interrupted"
            run.error = run.cancel_reason
            run.execution_lease_token_hash = None
            run.execution_lease_expires_at = None
            run.execution_heartbeat_at = None
            result: Literal["requested", "cancelled", "terminal"] = "cancelled"
        else:
            result = "requested"
        run.updated_at = requested_at
        await self.session.flush()
        return result

    async def list_by_thread(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[PrivateRunRecord, ...]:
        statement = (
            select(RunRow)
            .where(
                RunRow.thread_id == thread_id,
                *self.predicates(scope),
                RunRow.status != "deleted",
            )
            .order_by(RunRow.created_at.desc(), RunRow.run_id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.session.execute(statement)).scalars()
        return tuple(self.record(row) for row in rows)

    async def get_many_by_thread(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_ids: set[str],
    ) -> dict[str, PrivateRunRecord]:
        selected = {run_id for run_id in run_ids if isinstance(run_id, str) and run_id}
        if not selected:
            return {}
        rows = (
            await self.session.execute(
                select(RunRow).where(
                    RunRow.thread_id == thread_id,
                    RunRow.run_id.in_(selected),
                    *self.predicates(scope),
                    RunRow.status != "deleted",
                )
            )
        ).scalars()
        return {row.run_id: self.record(row) for row in rows}

    async def update_status(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> bool:
        revoked = RunRow.authorization_cancel_requested_at.is_not(None)
        values: dict[str, Any] = {
            "status": case((revoked, "interrupted"), else_=status),
            "updated_at": datetime.now(UTC),
        }
        if error is not None:
            values["error"] = case((revoked, "authorization_revoked"), else_=error)
        else:
            values["error"] = case((revoked, "authorization_revoked"), else_=RunRow.error)
        result = await self.session.execute(update(RunRow).where(RunRow.run_id == run_id, *self.predicates(scope)).values(**values))
        return result.rowcount != 0

    async def update_model_name(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        model_name: str | None,
    ) -> bool:
        result = await self.session.execute(update(RunRow).where(RunRow.run_id == run_id, *self.predicates(scope)).values(model_name=model_name, updated_at=datetime.now(UTC)))
        return result.rowcount != 0

    async def update_admitted_execution_profile(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        model_name: str,
        kwargs: Mapping[str, object],
    ) -> bool:
        """Persist only the server-resolved model/profile on a new Run."""

        if type(model_name) is not str or not model_name or not isinstance(kwargs, Mapping):
            raise PrivateRunConflict
        result = await self.session.execute(
            update(RunRow)
            .where(
                RunRow.run_id == run_id,
                *self.predicates(scope),
            )
            .values(
                model_name=model_name,
                kwargs_json=dict(kwargs),
                updated_at=datetime.now(UTC),
            )
        )
        return result.rowcount != 0

    async def delete(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
    ) -> bool:
        deleted_at = datetime.now(UTC)
        result = await self.session.execute(
            update(RunRow)
            .where(
                RunRow.run_id == run_id,
                *self.predicates(scope),
                RunRow.status != "deleted",
            )
            .values(
                assistant_id=None,
                status="deleted",
                metadata_json={},
                kwargs_json={},
                error=None,
                first_human_message=None,
                last_ai_message=None,
                model_name=None,
                token_usage_by_model={},
                follow_up_to_run_id=None,
                updated_at=deleted_at,
            )
        )
        return result.rowcount != 0
