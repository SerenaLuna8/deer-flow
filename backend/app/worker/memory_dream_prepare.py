"""Worker-only durable drain and child Dream admission for ``/Dream``."""

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
from app.private_work.memory_dream_service import (
    MemoryDreamAdmissionService,
    MemoryDreamModelUnavailable,
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
from app.worker.service import JobLeaseAuthority, JobOutcome, JobSettlement, LeaseLost
from deerflow.config.app_config import AppConfig
from deerflow.memory_contract import (
    MemoryDocumentScope,
    MemoryDreamPrepareConflict,
    MemoryDreamPrepareNotFound,
)
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.private_work.memory_dream_prepare_repository import (
    MemoryDreamPrepareRepository,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

_MEMORY_DREAM_PREPARE_REQUEST_ID = "memory-dream-prepare-worker"
_DREAM_PREPARE_KEEP: tuple[str, int] = ("messages", 0)
_DREAM_PREPARE_COMPACTION_FAILURE_CODES = {
    "prompt_budget_too_small": "MEMORY_DREAM_PREPARE_PROMPT_BUDGET_TOO_SMALL",
    "source_too_large": "MEMORY_DREAM_PREPARE_SOURCE_TOO_LARGE",
}


@dataclass(frozen=True, slots=True)
class _PrepareWork:
    context: PrivateWorkContext
    scope: MemoryDocumentScope
    thread_id: str
    request_id: str
    app_config: AppConfig


class MemoryDreamPrepareJobHandler:
    """Resume pass-by-pass compaction, then atomically admit the child Dream."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        app_config: AppConfig,
        barrier: ProjectChatControlService,
        admission: MemoryDreamAdmissionService | None = None,
        repository_builder=MemoryDreamPrepareRepository,
        job_repository_builder=JobRepository,
        personalization_repository_builder=AccountPersonalizationRepository,
        retry_initial_seconds: int = 5,
        retry_max_seconds: int = 300,
        audit=None,
    ) -> None:
        if (
            not callable(session_factory)
            or not callable(getattr(barrier, "compact", None))
            or not callable(getattr(barrier, "lock_and_verify_dream_archive_ready", None))
            or not callable(repository_builder)
            or not callable(job_repository_builder)
            or not callable(personalization_repository_builder)
            or type(retry_initial_seconds) is not int
            or retry_initial_seconds < 1
            or type(retry_max_seconds) is not int
            or retry_max_seconds < retry_initial_seconds
        ):
            raise ValueError("Dream preparation Worker configuration is invalid")
        if audit is not None and not callable(getattr(audit, "memory_dream_admitted", None)):
            raise ValueError("Dream preparation Worker audit port is invalid")
        self._sessions = session_factory
        self._app_config = app_config
        self._barrier = barrier
        self._admission = admission or MemoryDreamAdmissionService(
            job_repository_builder=job_repository_builder,
        )
        self._repository_builder = repository_builder
        self._job_repository_builder = job_repository_builder
        self._personalization_repository_builder = personalization_repository_builder
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds
        self._audit = audit

    def _repository(self, session: AsyncSession) -> MemoryDreamPrepareRepository:
        return self._repository_builder(
            session,
            jobs=self._job_repository_builder(session),
        )

    @staticmethod
    async def _lock_settlement_authority(
        session: AsyncSession,
        scope: MemoryDocumentScope,
    ) -> None:
        """Take Project -> Membership before any preparation/resource lock."""

        project = await session.scalar(sa.select(ProjectRow.id).where(ProjectRow.id == scope.project_id).with_for_update(of=ProjectRow))
        membership = await session.scalar(
            sa.select(ProjectMembershipRow.id)
            .where(
                ProjectMembershipRow.project_id == scope.project_id,
                ProjectMembershipRow.user_id == scope.owner_user_id,
            )
            .with_for_update(of=ProjectMembershipRow)
        )
        if project is None or membership is None:
            raise MemoryDreamPrepareNotFound

    async def _authorize(self, claim: JobClaim) -> _PrepareWork | None:
        owner_user_id = claim.scope.owner_user_id or ""
        scope = MemoryDocumentScope(
            project_id=claim.scope.project_id,
            owner_user_id=owner_user_id,
            namespace=claim.namespace or "",
        )
        async with self._sessions() as session, session.begin():
            try:
                project_context = await resolve_project_context_in_transaction(
                    session,
                    uuid.UUID(owner_user_id),
                    claim.scope.project_id,
                    _MEMORY_DREAM_PREPARE_REQUEST_ID,
                    lock=False,
                )
                project_context.require(Capability.PRIVATE_WORK_CREATE)
                project_context.require(Capability.SHARED_ASSETS_EXECUTE)
            except (ProjectForbidden, ProjectNotFound, ValueError):
                return None
            policy, _revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
                session,
                RuntimePolicySection.AGENT_RUNTIME,
                for_update=False,
            )
            if not isinstance(policy, AgentRuntimePolicyValue) or not policy.memory.enabled:
                return None
            try:
                preference = await self._personalization_repository_builder(session).read_memory(owner_user_id)
            except AccountPersonalizationNotFound:
                return None
            if not preference.memory_enabled:
                return None
            try:
                row = await self._repository(session).read_execution(
                    scope,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    now=datetime.now(UTC),
                )
            except (MemoryDreamPrepareNotFound, MemoryDreamPrepareConflict):
                return None
            thread_live = await session.scalar(
                sa.select(sa.literal(True)).where(
                    ThreadMetaRow.project_id == scope.project_id,
                    ThreadMetaRow.owner_user_id == scope.owner_user_id,
                    ThreadMetaRow.thread_id == row.thread_id,
                    ThreadMetaRow.deleted_at.is_(None),
                    ThreadMetaRow.frozen_at.is_(None),
                )
            )
            if not thread_live:
                return None
            return _PrepareWork(
                context=PrivateWorkContext.from_project(project_context),
                scope=scope,
                thread_id=row.thread_id,
                request_id=row.request_id,
                app_config=self._app_config.with_runtime_policy(
                    project_memory_compaction_app_config_policy(policy),
                ),
            )

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobOutcome | JobSettlement:
        if claim.job_type != "memory_dream_prepare" or claim.scope.owner_user_id is None or not claim.namespace or claim.run_id is not None or claim.occurrence_id is not None:
            return JobOutcome.cancelled()
        await authority.heartbeat()
        try:
            work = await self._authorize(claim)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._failure_settlement(
                claim,
                "MEMORY_DREAM_PREPARE_AUTHORITY_UNAVAILABLE",
            )
        if work is None:
            return self._cancel_settlement(claim)

        while True:
            await authority.heartbeat()
            if authority.cancel_requested:
                return self._cancel_settlement(claim)
            try:
                async with self._sessions() as session, session.begin():
                    await self._repository(session).set_phase(
                        work.scope,
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        phase="draining",
                        now=datetime.now(UTC),
                    )
                result = await self._barrier.compact(
                    work.context,
                    work.thread_id,
                    force=True,
                    keep=_DREAM_PREPARE_KEEP,
                    app_config=work.app_config,
                )
                await authority.heartbeat()
            except asyncio.CancelledError:
                raise
            except PrivateWorkThreadBusy:
                return self._failure_settlement(
                    claim,
                    "MEMORY_DREAM_PREPARE_THREAD_BUSY",
                )
            except PrivateWorkCompactionDisabled:
                return self._failure_settlement(
                    claim,
                    "MEMORY_DREAM_PREPARE_COMPACTION_DISABLED",
                )
            except PrivateWorkConflict:
                return self._failure_settlement(
                    claim,
                    "MEMORY_DREAM_PREPARE_HEAD_CHANGED",
                )
            except PrivateWorkNotFound:
                return self._cancel_settlement(claim)
            except PrivateWorkError:
                return self._failure_settlement(
                    claim,
                    "MEMORY_DREAM_PREPARE_DRAIN_FAILED",
                )
            except (MemoryDreamPrepareConflict, MemoryDreamPrepareNotFound):
                raise LeaseLost(claim.job_id) from None
            except Exception:
                return self._failure_settlement(
                    claim,
                    "MEMORY_DREAM_PREPARE_DRAIN_FAILED",
                )

            if result.compacted:
                checkpoint_id = result.checkpoint_id
                if result.removed_message_count <= 0 or not result.summary_updated or not isinstance(checkpoint_id, str) or not checkpoint_id:
                    return self._failure_settlement(
                        claim,
                        "MEMORY_DREAM_PREPARE_PROGRESS_STALLED",
                    )
                try:
                    async with self._sessions() as session, session.begin():
                        await self._repository(session).record_pass(
                            work.scope,
                            job_id=claim.job_id,
                            lease_token=claim.lease_token,
                            checkpoint_id=checkpoint_id,
                            now=datetime.now(UTC),
                        )
                except (MemoryDreamPrepareConflict, MemoryDreamPrepareNotFound):
                    raise LeaseLost(claim.job_id) from None
                continue
            if result.reason != "not_enough_messages":
                return self._failure_settlement(
                    claim,
                    _DREAM_PREPARE_COMPACTION_FAILURE_CODES.get(
                        result.reason or "",
                        "MEMORY_DREAM_PREPARE_DRAIN_FAILED",
                    ),
                )
            return self._final_settlement(claim, work)

    def _failure_settlement(
        self,
        claim: JobClaim,
        public_error_code: str,
    ) -> JobSettlement:
        async def commit() -> None:
            scope = MemoryDocumentScope(
                project_id=claim.scope.project_id,
                owner_user_id=claim.scope.owner_user_id or "",
                namespace=claim.namespace or "",
            )
            async with self._sessions() as session, session.begin():
                try:
                    await self._lock_settlement_authority(session, scope)
                    await self._repository(session).retry_or_dead(
                        scope,
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        public_error_code=public_error_code,
                        retry_initial_seconds=self._retry_initial_seconds,
                        retry_max_seconds=self._retry_max_seconds,
                        now=datetime.now(UTC),
                    )
                except (MemoryDreamPrepareConflict, MemoryDreamPrepareNotFound):
                    raise LeaseLost(claim.job_id) from None

        return JobSettlement(JobOutcome.failed(public_error_code), commit)

    def _cancel_settlement(self, claim: JobClaim) -> JobSettlement:
        async def commit() -> None:
            scope = MemoryDocumentScope(
                project_id=claim.scope.project_id,
                owner_user_id=claim.scope.owner_user_id or "",
                namespace=claim.namespace or "",
            )
            async with self._sessions() as session, session.begin():
                try:
                    await self._lock_settlement_authority(session, scope)
                    await self._repository(session).settle_cancelled(
                        scope,
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        now=datetime.now(UTC),
                    )
                except MemoryDreamPrepareNotFound:
                    settled = await self._job_repository_builder(session).settle_cancelled(
                        claim.job_id,
                        lease_token=claim.lease_token,
                    )
                    if not settled:
                        raise LeaseLost(claim.job_id)
                except MemoryDreamPrepareConflict:
                    raise LeaseLost(claim.job_id) from None

        return JobSettlement(JobOutcome.cancelled(), commit)

    def _final_settlement(
        self,
        claim: JobClaim,
        work: _PrepareWork,
    ) -> JobSettlement:
        async def commit() -> None:
            now = datetime.now(UTC)
            async with self._sessions() as session, session.begin():
                try:
                    project_context = await resolve_project_context_in_transaction(
                        session,
                        uuid.UUID(work.scope.owner_user_id),
                        work.scope.project_id,
                        _MEMORY_DREAM_PREPARE_REQUEST_ID,
                        lock=True,
                    )
                    project_context.require(Capability.PRIVATE_WORK_CREATE)
                    project_context.require(Capability.SHARED_ASSETS_EXECUTE)
                except (ProjectForbidden, ProjectNotFound, ValueError):
                    await self._repository(session).settle_cancelled(
                        work.scope,
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        now=now,
                    )
                    return
                context = PrivateWorkContext.from_project(project_context)
                repository = self._repository(session)
                ready = await self._barrier.lock_and_verify_dream_archive_ready(
                    session,
                    context,
                    work.thread_id,
                    app_config=work.app_config,
                )
                # The barrier now holds the Thread row.  Only then may the
                # preparation row be locked/updated, matching admission order.
                await repository.set_phase(
                    work.scope,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    phase="verifying",
                    now=now,
                )
                if not ready:
                    await repository.retry_or_dead(
                        work.scope,
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        public_error_code="MEMORY_DREAM_PREPARE_HEAD_CHANGED",
                        retry_initial_seconds=self._retry_initial_seconds,
                        retry_max_seconds=self._retry_max_seconds,
                        now=now,
                    )
                    return
                try:
                    admitted = await self._admission.admit(
                        session,
                        work.scope,
                        trigger="manual_dream",
                        now=now,
                    )
                except MemoryDreamModelUnavailable:
                    await repository.retry_or_dead(
                        work.scope,
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        public_error_code="MEMORY_DREAM_MODEL_UNAVAILABLE",
                        retry_initial_seconds=self._retry_initial_seconds,
                        retry_max_seconds=self._retry_max_seconds,
                        now=now,
                    )
                    return
                await repository.link_dream(
                    work.scope,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    admitted=admitted,
                    now=now,
                )
                if admitted.disposition == "queued" and admitted.job_id is not None and self._audit is not None:
                    await self._audit.memory_dream_admitted(
                        session,
                        project_id=work.scope.project_id,
                        job_id=admitted.job_id,
                        request_id=work.request_id,
                        origin="prepared",
                        trigger=("budget_rewrite" if admitted.admission_kind == "budget_rewrite" else "manual_dream"),
                        history_count=admitted.history_count,
                        parent_prepare_job_id=claim.job_id,
                        prepare_lease_token=claim.lease_token,
                        now=now,
                    )
                await repository.settle_success(
                    work.scope,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    now=now,
                )

        return JobSettlement(JobOutcome.succeeded(), commit)


__all__ = ["MemoryDreamPrepareJobHandler"]
