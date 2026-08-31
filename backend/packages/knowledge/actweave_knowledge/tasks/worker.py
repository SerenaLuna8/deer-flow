"""Claim-execute-settle loop over ``knowledge_tasks``.

Each loop iteration recovers expired leases, claims one due task with
``FOR UPDATE SKIP LOCKED``, revalidates Project-active eligibility through the
host in that same transaction, runs the kind's handler under the configured
timeout while a heartbeat extends the lease, and settles the claim by token.
An inactive Project claim returns to retry_wait without spending an attempt.
Handlers may settle their own claim inside a publish transaction (the ingest
handler does); the worker's success settlement then finds no running claim
and is a no-op. Setting the stop event stops new claims. A timeout cancels the
handler, but Knowledge's blocking-call adapter joins already-started parser or
object-store work before cancellation completes, so settlement and retry never
overlap that work. A permanently stuck synchronous dependency can therefore
hold the claim beyond the nominal timeout instead of spawning unsafe retries.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import KNOWLEDGE_TASK_FAILED, KnowledgeError
from ..persistence.models import KnowledgeTaskRow
from ..persistence.tasks import (
    claim_next_task,
    defer_running_task_for_inactive_project,
    defer_task_claim_for_inactive_project,
    extend_task_lease,
    recover_expired_tasks,
    settle_task_failure,
    settle_task_success,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 60
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_RETRY_DELAY_SECONDS = 30


@dataclass(frozen=True, slots=True)
class KnowledgeTaskClaim:
    """Immutable snapshot of one claimed ``knowledge_tasks`` row."""

    id: UUID
    project_id: UUID
    resource_id: UUID
    kind: str
    target_version: int | None
    claim_token: UUID
    attempt_count: int
    max_attempts: int
    storage_key: str | None = None
    # Frozen re-parse parameters (ingest_document only): the handler applies
    # these instead of the document's stored columns, which swap on publish.
    reparse_settings: dict | None = None


TaskHandler = Callable[[KnowledgeTaskClaim], Awaitable[None]]
ProjectActiveCheck = Callable[[AsyncSession, UUID], Awaitable[bool]]


class KnowledgeProjectInactive(KnowledgeError):
    """Pause an indexing claim after its in-flight work has drained."""

    def __init__(self) -> None:
        super().__init__(KNOWLEDGE_TASK_FAILED, "Project 不再 active，暂停 Knowledge 任务")


class KnowledgeTaskWorker:
    """Run ``concurrency`` claim loops until the stop event is set."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: dict[str, TaskHandler],
        project_active_check: ProjectActiveCheck,
        concurrency: int,
        task_timeout_seconds: int,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = dict(handlers)
        self._project_active_check = project_active_check
        self._concurrency = concurrency
        self._task_timeout_seconds = task_timeout_seconds
        self._lease_seconds = lease_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._retry_delay_seconds = retry_delay_seconds

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run all claim loops; the first loop failure stops the worker."""

        loops = [asyncio.create_task(self._loop(stop_event), name=f"knowledge-task-loop-{index}") for index in range(self._concurrency)]
        try:
            await asyncio.gather(*loops)
        finally:
            for loop_task in loops:
                loop_task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)

    async def _loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            worked = await self._run_once()
            if not worked and not stop_event.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_seconds)

    async def _run_once(self) -> bool:
        """Claim and execute at most one task; False when nothing was due."""

        claimed_or_deferred = False
        try:
            async with self._session_factory() as session, session.begin():
                await recover_expired_tasks(session)
                row = await claim_next_task(session, lease_seconds=self._lease_seconds)
                claimed_or_deferred = row is not None
                if row is not None and not await self._project_active_check(
                    session,
                    row.project_id,
                ):
                    # Project deletion admission is an execution fence. Return
                    # the task without spending its attempt in this same claim
                    # transaction, so restore can resume it but no handler can
                    # start with a pending-deletion Project snapshot.
                    await defer_task_claim_for_inactive_project(session, row)
                    row = None
                claim = (
                    KnowledgeTaskClaim(
                        id=row.id,
                        project_id=row.project_id,
                        resource_id=row.resource_id,
                        kind=row.kind,
                        target_version=row.target_version,
                        claim_token=row.claim_token,  # type: ignore[arg-type]
                        attempt_count=row.attempt_count,
                        max_attempts=row.max_attempts,
                        storage_key=row.storage_key,
                        reparse_settings=row.reparse_settings,
                    )
                    if row is not None
                    else None
                )
        except SQLAlchemyError:
            logger.warning("knowledge task claim failed; database unavailable", exc_info=True)
            return False
        if claim is None:
            return claimed_or_deferred
        await self._execute(claim)
        return True

    async def _execute(self, claim: KnowledgeTaskClaim) -> None:
        handler = self._handlers.get(claim.kind)
        if handler is None:  # kinds are constrained by CHECK; defensive only
            await self._settle_failure(claim, KnowledgeError(KNOWLEDGE_TASK_FAILED, f"没有 {claim.kind} 的任务处理器"))
            return
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(claim, stop_heartbeat), name=f"knowledge-task-heartbeat-{claim.id}")
        error: KnowledgeError | None = None
        project_inactive = False
        try:
            # wait_for waits for handler cancellation to finish. Knowledge
            # handlers settle started blocking calls before propagating that
            # cancellation, so the heartbeat covers the complete drain.
            await asyncio.wait_for(handler(claim), timeout=self._task_timeout_seconds)
        except TimeoutError:
            error = KnowledgeError(KNOWLEDGE_TASK_FAILED, f"任务执行超过 {self._task_timeout_seconds} 秒")
        except KnowledgeProjectInactive:
            project_inactive = True
        except KnowledgeError as exc:
            error = exc
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("knowledge task %s handler crashed", claim.id)
            error = KnowledgeError(KNOWLEDGE_TASK_FAILED, "任务执行失败")
        finally:
            stop_heartbeat.set()
            try:
                await heartbeat
            except Exception:
                # A heartbeat bug must not mask the handler outcome, but it
                # must be visible; the lease machinery covers the fallout.
                logger.exception("knowledge task %s heartbeat crashed", claim.id)
        if project_inactive:
            await self._defer_inactive_claim(claim)
        elif error is None:
            await self._settle_success(claim)
        else:
            await self._settle_failure(claim, error)

    async def _defer_inactive_claim(self, claim: KnowledgeTaskClaim) -> None:
        """Release a paused claim only after handler cleanup and heartbeat drain."""

        try:
            async with self._session_factory() as session, session.begin():
                await defer_running_task_for_inactive_project(session, claim.id, claim.claim_token)
        except SQLAlchemyError:
            logger.warning("knowledge task %s inactive Project deferral failed", claim.id, exc_info=True)

    async def _heartbeat(self, claim: KnowledgeTaskClaim, stop_heartbeat: asyncio.Event) -> None:
        interval = max(self._lease_seconds / 3.0, 1.0)
        while True:
            try:
                await asyncio.wait_for(stop_heartbeat.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                async with self._session_factory() as session, session.begin():
                    alive = await extend_task_lease(
                        session,
                        claim.id,
                        claim.claim_token,
                        lease_seconds=self._lease_seconds,
                    )
            except SQLAlchemyError:
                # Transient database trouble: keep trying until the lease
                # question can actually be answered.
                logger.warning("knowledge task %s heartbeat failed; database unavailable", claim.id, exc_info=True)
                continue
            if not alive:
                if await self._task_settled(claim):
                    # The handler settled its own claim (the ingest publish
                    # does); a heartbeat racing that commit is not a loss.
                    return
                # The lease expired and was re-claimed. Every publish and
                # settlement is token-guarded, so the stale handler can finish
                # without effect.
                logger.warning("knowledge task %s lease was lost during execution", claim.id)
                return

    async def _task_settled(self, claim: KnowledgeTaskClaim) -> bool:
        try:
            async with self._session_factory() as session:
                status = await session.scalar(select(KnowledgeTaskRow.status).where(KnowledgeTaskRow.id == claim.id))
        except SQLAlchemyError:
            return False
        return status in ("succeeded", "failed")

    async def _settle_success(self, claim: KnowledgeTaskClaim) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await settle_task_success(session, claim.id, claim.claim_token)
        except SQLAlchemyError:
            # The claim stays running until its lease expires, then the
            # recovery sweep retries or fails it; nothing is lost.
            logger.warning("knowledge task %s success settlement failed", claim.id, exc_info=True)

    async def _settle_failure(self, claim: KnowledgeTaskClaim, error: KnowledgeError) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                # A terminal Provider error (or timeout) may arrive before the
                # next batch guard observes Project deletion. It must pause
                # without consuming the retry budget just like that guard.
                if not await self._project_active_check(session, claim.project_id):
                    await defer_running_task_for_inactive_project(session, claim.id, claim.claim_token)
                    return
                outcome = await settle_task_failure(
                    session,
                    claim.id,
                    claim.claim_token,
                    error_message=error.message,
                    retry_delay_seconds=self._retry_delay_seconds * claim.attempt_count,
                )
        except SQLAlchemyError:
            logger.warning("knowledge task %s failure settlement failed", claim.id, exc_info=True)
            return
        if outcome == "failed":
            logger.warning("knowledge task %s failed permanently: %s", claim.id, error.message)
