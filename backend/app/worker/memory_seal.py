"""Worker-only idle sealing of one thread's unarchived turns into Memory.

The handler reuses the exact pre-Dream drain machinery: the manual ``/compact``
path with ``keep=("messages", 0)`` and the locked archive barrier. It never
invents a second compression semantic, never touches the UI message journal,
and yields as a no-op the moment a live Run appears on the thread.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import (
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkCompactionDisabled,
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkThreadBusy,
)
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.system_runtime_settings.app_config_projection import (
    project_memory_compaction_app_config_policy,
)
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
)
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
)
from deerflow.config.app_config import AppConfig
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.thread_meta.model import ThreadMetaRow

_MEMORY_SEAL_REQUEST_ID = "memory-seal-worker"
_SEAL_KEEP: tuple[str, int] = ("messages", 0)
_SEAL_COMPACTION_FAILURE_CODES = {
    "prompt_budget_too_small": "MEMORY_SEAL_PROMPT_BUDGET_TOO_SMALL",
    "source_too_large": "MEMORY_SEAL_SOURCE_TOO_LARGE",
}


@dataclass(frozen=True, slots=True)
class _SealWork:
    context: PrivateWorkContext
    app_config: AppConfig


class MemorySealJobHandler:
    """Drain one idle thread with the manual-compact semantics, then stamp it."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        app_config: AppConfig,
        barrier: ProjectChatControlService,
        job_repository_builder=JobRepository,
        personalization_repository_builder=AccountPersonalizationRepository,
        audit=None,
    ) -> None:
        if (
            not callable(session_factory)
            or not callable(getattr(barrier, "compact", None))
            or not callable(getattr(barrier, "lock_and_verify_dream_archive_ready", None))
            or not callable(job_repository_builder)
            or not callable(personalization_repository_builder)
        ):
            raise ValueError("Seal Worker configuration is invalid")
        if audit is not None and not callable(getattr(audit, "memory_seal_settled", None)):
            raise ValueError("Seal Worker audit port is invalid")
        self._sessions = session_factory
        self._app_config = app_config
        self._barrier = barrier
        self._job_repository_builder = job_repository_builder
        self._personalization_repository_builder = personalization_repository_builder
        self._audit = audit

    async def _authorize(
        self,
        claim: JobClaim,
        thread_id: str,
    ) -> _SealWork | None:
        """Mint the private authority context, or None when sealing must stop.

        Platform policy, owner preference, and the thread row are all re-read
        at execution time: admission-time truths may have flipped, and a
        disabled or vanished coordinate must cancel instead of draining.
        """

        owner_user_id = claim.scope.owner_user_id or ""
        async with self._sessions() as session, session.begin():
            try:
                project_context = await resolve_project_context_in_transaction(
                    session,
                    uuid.UUID(owner_user_id),
                    claim.scope.project_id,
                    _MEMORY_SEAL_REQUEST_ID,
                    lock=False,
                )
                project_context.require(Capability.PRIVATE_WORK_CREATE)
            except (ProjectForbidden, ProjectNotFound, ValueError):
                return None
            policy, _revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
                session,
                RuntimePolicySection.AGENT_RUNTIME,
                for_update=False,
            )
            if not isinstance(policy, AgentRuntimePolicyValue) or not policy.memory.enabled or policy.memory.idle_seal_minutes <= 0:
                return None
            try:
                preference = await self._personalization_repository_builder(session).read_memory(owner_user_id)
            except AccountPersonalizationNotFound:
                return None
            if not preference.memory_enabled:
                return None
            thread_live = await session.scalar(
                sa.select(sa.literal(True)).where(
                    ThreadMetaRow.project_id == claim.scope.project_id,
                    ThreadMetaRow.owner_user_id == owner_user_id,
                    ThreadMetaRow.thread_id == thread_id,
                    ThreadMetaRow.deleted_at.is_(None),
                    ThreadMetaRow.frozen_at.is_(None),
                )
            )
            if not thread_live:
                return None
        return _SealWork(
            context=PrivateWorkContext.from_project(project_context),
            app_config=self._app_config.with_runtime_policy(
                project_memory_compaction_app_config_policy(policy),
            ),
        )

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobOutcome | JobSettlement:
        if claim.job_type != "memory_seal" or claim.scope.owner_user_id is None or not claim.namespace or claim.run_id is not None or claim.occurrence_id is not None:
            return JobOutcome.cancelled()
        thread_id = claim.namespace
        await authority.heartbeat()
        try:
            work = await self._authorize(claim, thread_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return JobOutcome.failed("MEMORY_SEAL_AUTHORITY_UNAVAILABLE")
        if work is None:
            return JobOutcome.cancelled()

        committed_checkpoints: set[str] = set()
        while True:
            await authority.heartbeat()
            if authority.cancel_requested:
                return JobOutcome.cancelled()
            try:
                result = await self._barrier.compact(
                    work.context,
                    thread_id,
                    force=True,
                    keep=_SEAL_KEEP,
                    app_config=work.app_config,
                )
            except asyncio.CancelledError:
                raise
            except PrivateWorkThreadBusy:
                # A live Run owns the thread again: yield without stamping.
                return self._settlement(claim, work, disposition="noop")
            except PrivateWorkCompactionDisabled:
                return JobOutcome.failed("MEMORY_SEAL_COMPACTION_DISABLED")
            except PrivateWorkNotFound:
                return JobOutcome.cancelled()
            except PrivateWorkError:
                return JobOutcome.failed("MEMORY_SEAL_DRAIN_FAILED")
            except Exception:
                return JobOutcome.failed("MEMORY_SEAL_DRAIN_FAILED")
            if result.compacted:
                checkpoint_id = result.checkpoint_id
                if result.removed_message_count <= 0 or not isinstance(checkpoint_id, str) or not checkpoint_id or checkpoint_id in committed_checkpoints:
                    return JobOutcome.failed("MEMORY_SEAL_PROGRESS_STALLED")
                committed_checkpoints.add(checkpoint_id)
                continue
            if result.reason != "not_enough_messages":
                return JobOutcome.failed(
                    _SEAL_COMPACTION_FAILURE_CODES.get(
                        result.reason or "",
                        "MEMORY_SEAL_DRAIN_FAILED",
                    )
                )
            return self._settlement(claim, work, disposition="sealed")

    def _settlement(
        self,
        claim: JobClaim,
        work: _SealWork,
        *,
        disposition: str,
    ) -> JobSettlement:
        """Verify, stamp, audit, and settle in one final transaction.

        The sealed stamp is only written behind the locked archive barrier that
        re-proves the drained head and the absence of a live Run; any race
        downgrades this settlement to a no-op instead of forging a seal.
        """

        thread_id = claim.namespace or ""
        now = datetime.now(UTC)
        context = work.context

        async def commit() -> None:
            final_disposition = disposition
            async with self._sessions() as session, session.begin():
                if final_disposition == "sealed":
                    try:
                        ready = await self._barrier.lock_and_verify_dream_archive_ready(
                            session,
                            context,
                            thread_id,
                            app_config=work.app_config,
                        )
                    except PrivateWorkConflict:
                        ready = False
                    except PrivateWorkNotFound:
                        ready = False
                    if ready:
                        # The explicit updated_at assignment suppresses the ORM
                        # onupdate: a background seal must not surface the
                        # thread as recently active.
                        await session.execute(
                            sa.update(ThreadMetaRow)
                            .where(
                                ThreadMetaRow.project_id == context.project_id,
                                ThreadMetaRow.owner_user_id == str(context.user_id),
                                ThreadMetaRow.thread_id == thread_id,
                            )
                            .values(
                                memory_sealed_at=now,
                                updated_at=ThreadMetaRow.updated_at,
                            )
                        )
                    else:
                        final_disposition = "noop"
                settled = await self._job_repository_builder(session).settle_success(
                    claim.job_id,
                    lease_token=claim.lease_token,
                )
                if not settled:
                    raise LeaseLost(claim.job_id)
                if self._audit is not None:
                    await self._audit.memory_seal_settled(
                        session,
                        project_id=context.project_id,
                        job_id=claim.job_id,
                        request_id=_MEMORY_SEAL_REQUEST_ID,
                        disposition=final_disposition,
                    )

        return JobSettlement(JobOutcome.succeeded(), commit)


__all__ = ["MemorySealJobHandler"]
