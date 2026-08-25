"""Idle-thread seal discovery and admission for the Scheduler.

The Scheduler only discovers idle threads and enqueues durable ``memory_seal``
Jobs; the Worker performs the actual drain. Every admission runs in its own
short transaction with the Project -> Membership -> Thread lock order used by
Dream admission, and re-verifies every due condition under those locks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import (
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
from app.private_work.account_private_lifecycle import (
    AccountPrivateLifecycle,
    AccountPrivateLifecycleClosed,
    AccountPrivateLifecyclePort,
)
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow

_SCHEDULER_REQUEST_ID = "memory-seal-scheduler"
_SEAL_JOB_MAX_ATTEMPTS = 5
_SEAL_KEY_DOMAIN = "actweave.memory.seal.v2"
_ACTIVE_JOB_STATUSES = ("queued", "leased", "running", "retry_wait")
logger = logging.getLogger(__name__)


def compute_seal_idempotency_key(
    *,
    project_id: str,
    owner_user_id: str,
    thread_id: str,
    activity_at: datetime,
) -> str:
    """Hash one seal admission identity from the Thread activity epoch."""

    if activity_at.tzinfo is None or activity_at.utcoffset() is None:
        raise ValueError("Seal activity timestamp must be timezone-aware")

    payload = {
        "activity_at": activity_at.astimezone(UTC)
        .isoformat(
            timespec="microseconds",
        )
        .replace("+00:00", "Z"),
        "domain": _SEAL_KEY_DOMAIN,
        "owner_user_id": owner_user_id,
        "project_id": project_id,
        "thread_id": thread_id,
    }
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class MemorySealAdmissionService:
    """Enqueue one ``memory_seal`` Job per idle thread, race-free."""

    def __init__(
        self,
        *,
        job_repository_builder=JobRepository,
        personalization_repository_builder=AccountPersonalizationRepository,
        audit=None,
        account_private_lifecycle: AccountPrivateLifecyclePort | None = None,
    ) -> None:
        if not callable(job_repository_builder) or not callable(personalization_repository_builder):
            raise ValueError("Seal admission configuration is invalid")
        if audit is not None and not callable(getattr(audit, "memory_seal_admitted", None)):
            raise ValueError("Seal admission audit port is invalid")
        self._job_repository_builder = job_repository_builder
        self._personalization_repository_builder = personalization_repository_builder
        self._audit = audit
        self._account_private_lifecycle = account_private_lifecycle or AccountPrivateLifecycle()

    @staticmethod
    async def _platform_idle_minutes(session: AsyncSession) -> int | None:
        """Return the idle threshold, or None when sealing is off platform-wide."""

        policy, _revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
            session,
            RuntimePolicySection.AGENT_RUNTIME,
            for_update=False,
        )
        if not isinstance(policy, AgentRuntimePolicyValue) or not policy.memory.enabled:
            return None
        if policy.memory.idle_seal_minutes <= 0:
            return None
        return policy.memory.idle_seal_minutes

    @staticmethod
    def _settled_run_exists(thread: type[ThreadMetaRow]) -> sa.ColumnElement[bool]:
        """A settled Run newer than the last seal proves there is new work."""

        return sa.exists(
            sa.select(sa.literal(1)).where(
                RunRow.project_id == thread.project_id,
                RunRow.owner_user_id == thread.owner_user_id,
                RunRow.thread_id == thread.thread_id,
                RunRow.status.not_in(("pending", "running")),
                RunRow.finalization_status != "finalizing",
                sa.or_(
                    thread.memory_sealed_at.is_(None),
                    RunRow.updated_at > thread.memory_sealed_at,
                ),
            )
        )

    @staticmethod
    def _active_run_exists(thread: type[ThreadMetaRow]) -> sa.ColumnElement[bool]:
        return sa.exists(
            sa.select(sa.literal(1)).where(
                RunRow.project_id == thread.project_id,
                RunRow.owner_user_id == thread.owner_user_id,
                RunRow.thread_id == thread.thread_id,
                sa.or_(
                    RunRow.status.in_(("pending", "running")),
                    RunRow.finalization_status == "finalizing",
                ),
            )
        )

    @staticmethod
    def _active_seal_job_exists(thread: type[ThreadMetaRow]) -> sa.ColumnElement[bool]:
        return sa.exists(
            sa.select(sa.literal(1)).where(
                JobRow.job_type == "memory_seal",
                JobRow.project_id == thread.project_id,
                JobRow.owner_user_id == thread.owner_user_id,
                JobRow.namespace == thread.thread_id,
                JobRow.status.in_(_ACTIVE_JOB_STATUSES),
            )
        )

    @staticmethod
    def _terminal_seal_failure_for_activity_exists(
        thread: type[ThreadMetaRow],
    ) -> sa.ColumnElement[bool]:
        """A terminal failure already consumed the current activity epoch."""

        return sa.exists(
            sa.select(sa.literal(1)).where(
                JobRow.job_type == "memory_seal",
                JobRow.project_id == thread.project_id,
                JobRow.owner_user_id == thread.owner_user_id,
                JobRow.namespace == thread.thread_id,
                JobRow.status.in_(("failed", "dead")),
                JobRow.created_at >= thread.updated_at,
            )
        )

    @classmethod
    def _due_predicates(
        cls,
        *,
        now: datetime,
        idle_minutes: int,
    ) -> tuple[sa.ColumnElement[bool], ...]:
        return (
            ThreadMetaRow.deleted_at.is_(None),
            ThreadMetaRow.frozen_at.is_(None),
            ThreadMetaRow.updated_at <= now - timedelta(minutes=idle_minutes),
            cls._settled_run_exists(ThreadMetaRow),
            ~cls._active_run_exists(ThreadMetaRow),
            ~cls._active_seal_job_exists(ThreadMetaRow),
            ~cls._terminal_seal_failure_for_activity_exists(ThreadMetaRow),
        )

    async def list_due_threads(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        max_jobs: int = 20,
    ) -> tuple[tuple[uuid.UUID, str, str], ...]:
        """Discover up to ``max_jobs`` sealable threads without taking locks."""

        if type(max_jobs) is not int or not 1 <= max_jobs <= 20:
            raise ValueError("Seal Scheduler batch is invalid")
        idle_minutes = await self._platform_idle_minutes(session)
        if idle_minutes is None:
            return ()
        rows = await session.execute(
            sa.select(
                ThreadMetaRow.project_id,
                ThreadMetaRow.owner_user_id,
                ThreadMetaRow.thread_id,
            )
            .join(UserRow, UserRow.id == ThreadMetaRow.owner_user_id)
            .where(
                UserRow.memory_enabled.is_(True),
                *self._due_predicates(now=now, idle_minutes=idle_minutes),
            )
            .order_by(ThreadMetaRow.updated_at)
            .limit(max_jobs)
        )
        return tuple((row.project_id, row.owner_user_id, row.thread_id) for row in rows)

    async def admit_thread(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        now: datetime,
    ) -> uuid.UUID | None:
        """Lock Project -> Membership -> User -> Thread, then enqueue one Job."""

        context = await resolve_project_context_in_transaction(
            session,
            uuid.UUID(owner_user_id),
            project_id,
            _SCHEDULER_REQUEST_ID,
            lock=True,
        )
        context.require(Capability.PRIVATE_WORK_CREATE)
        try:
            account_private_generation = await self._account_private_lifecycle.require_active_after_membership(
                session,
                owner_user_id,
            )
        except AccountPrivateLifecycleClosed:
            return None
        thread = (
            await session.execute(
                sa.select(ThreadMetaRow)
                .where(
                    ThreadMetaRow.project_id == project_id,
                    ThreadMetaRow.owner_user_id == owner_user_id,
                    ThreadMetaRow.thread_id == thread_id,
                )
                .with_for_update(of=ThreadMetaRow)
            )
        ).scalar_one_or_none()
        if thread is None:
            return None
        idle_minutes = await self._platform_idle_minutes(session)
        if idle_minutes is None:
            return None
        try:
            preference = await self._personalization_repository_builder(session).read_memory(owner_user_id)
        except AccountPersonalizationNotFound:
            return None
        if not preference.memory_enabled:
            return None
        still_due = await session.scalar(
            sa.select(sa.literal(True)).where(
                ThreadMetaRow.project_id == project_id,
                ThreadMetaRow.owner_user_id == owner_user_id,
                ThreadMetaRow.thread_id == thread_id,
                *self._due_predicates(now=now, idle_minutes=idle_minutes),
            )
        )
        if not still_due:
            return None
        idempotency_key = compute_seal_idempotency_key(
            project_id=str(project_id),
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            activity_at=thread.updated_at,
        )
        existing_job_id = await session.scalar(
            sa.select(JobRow.id)
            .where(
                JobRow.job_type == "memory_seal",
                JobRow.project_id == project_id,
                JobRow.owner_user_id == owner_user_id,
                JobRow.namespace == thread_id,
                JobRow.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        if existing_job_id is not None:
            return None
        job_id = await self._job_repository_builder(session).enqueue(
            EnqueueJob(
                job_type="memory_seal",
                scope=JobScope(project_id, owner_user_id),
                namespace=thread_id,
                idempotency_key=idempotency_key,
                run_id=None,
                occurrence_id=None,
                max_attempts=_SEAL_JOB_MAX_ATTEMPTS,
                owner_private_generation=account_private_generation,
                retry_safety="safe",
            )
        )
        if self._audit is not None:
            await self._audit.memory_seal_admitted(
                session,
                project_id=project_id,
                job_id=job_id,
                request_id=_SCHEDULER_REQUEST_ID,
            )
        return job_id


class MemorySealSchedulerService:
    """Bound one Scheduler poll to a bounded set of seal admissions."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        admission: MemorySealAdmissionService | None = None,
        max_jobs_per_poll: int = 20,
    ) -> None:
        if not callable(session_factory) or type(max_jobs_per_poll) is not int or not 1 <= max_jobs_per_poll <= 20:
            raise ValueError("Seal Scheduler configuration is invalid")
        self._sessions = session_factory
        self._admission = admission or MemorySealAdmissionService()
        self._max_jobs_per_poll = max_jobs_per_poll

    async def admit_due(
        self,
        *,
        now: datetime,
    ) -> int:
        async with self._sessions() as session, session.begin():
            candidates = await self._admission.list_due_threads(
                session,
                now=now,
                max_jobs=self._max_jobs_per_poll,
            )

        admitted = 0
        for project_id, owner_user_id, thread_id in candidates:
            try:
                async with self._sessions() as session, session.begin():
                    job_id = await self._admission.admit_thread(
                        session,
                        project_id=project_id,
                        owner_user_id=owner_user_id,
                        thread_id=thread_id,
                        now=now,
                    )
            except (
                AccountPersonalizationNotFound,
                ProjectForbidden,
                ProjectNotFound,
                ValueError,
            ):
                continue
            except Exception as error:  # noqa: BLE001 - isolate thread scopes
                logger.error(
                    "Memory seal admission failed: error_type=%s",
                    type(error).__name__,
                )
                continue
            if job_id is not None:
                admitted += 1
        return admitted


__all__ = [
    "MemorySealAdmissionService",
    "MemorySealSchedulerService",
    "compute_seal_idempotency_key",
]
