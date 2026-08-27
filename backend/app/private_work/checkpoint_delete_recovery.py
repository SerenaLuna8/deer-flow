"""Durable, idempotent recovery for raw LangGraph checkpoint deletion."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.thread_repository import (
    CheckpointDeleteCandidate,
    PrivateThreadRecord,
    PrivateThreadRepository,
)
from deerflow.runtime.private_scope import PrivateResourceScope

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 50
_DEFAULT_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CheckpointDeleteRecoveryReport:
    selected: int
    completed: int
    retry_required: int


def checkpoint_delete_candidate_from_record(
    record: PrivateThreadRecord,
) -> CheckpointDeleteCandidate:
    """Project a tombstone into the content-free raw cleanup contract."""

    if record.deleted_at is None:
        raise ValueError("checkpoint delete candidate requires a tombstone")
    if record.checkpoint_delete_status not in {"pending", "retry_required"}:
        raise ValueError("checkpoint delete candidate requires requested cleanup")
    return CheckpointDeleteCandidate(
        thread_id=record.thread_id,
        project_id=record.project_id,
        owner_user_id=record.owner_user_id,
        thread_kind=record.thread_kind,
        created_at=record.created_at,
        deleted_at=record.deleted_at,
    )


async def _persist_status_locked(
    session: AsyncSession,
    candidate: CheckpointDeleteCandidate,
    status: str,
) -> bool:
    scope = PrivateResourceScope(
        project_id=str(candidate.project_id),
        owner_user_id=candidate.owner_user_id,
        membership_version=0,
    )
    return await PrivateThreadRepository(
        session,
    ).set_checkpoint_delete_status(
        scope=scope,
        thread_id=candidate.thread_id,
        thread_kind=candidate.thread_kind,
        status=status,
    )


async def recover_checkpoint_delete_candidate(
    raw_saver: BaseCheckpointSaver,
    session_factory: async_sessionmaker[AsyncSession],
    candidate: CheckpointDeleteCandidate,
) -> bool:
    """Try one physical deletion and persist only monotonic recovery state.

    The authoritative tombstone already exists when this function is called.
    One exact tombstone-generation row lock is intentionally held across the
    raw saver call. This narrow fence prevents an old in-memory candidate from
    deleting checkpoints for a same-ID Thread recreated after retention. It
    also serializes multiple Gateways without a process-local claim.
    """

    try:
        async with session_factory() as session, session.begin():
            repository = PrivateThreadRepository(session)
            if not await repository.lock_checkpoint_delete_candidate(candidate):
                return True
            try:
                await raw_saver.adelete_thread(candidate.thread_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                await _persist_status_locked(
                    session,
                    candidate,
                    "retry_required",
                )
                return False
            await _persist_status_locked(
                session,
                candidate,
                "complete",
            )
            return True
    except asyncio.CancelledError:
        raise
    except Exception:
        # Raw/status/database failure is durable retry work. The public Thread
        # tombstone already committed and therefore remains successful.
        return False


class CheckpointDeleteReconciler:
    """Gateway-owned immediate and periodic tombstone cleanup."""

    def __init__(
        self,
        raw_saver: BaseCheckpointSaver,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("checkpoint delete recovery batch size is invalid")
        if interval_seconds <= 0:
            raise ValueError("checkpoint delete recovery interval is invalid")
        self._raw_saver = raw_saver
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        if self._closed or self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run_forever(),
            name="checkpoint-delete-reconciler",
        )

    async def aclose(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def run_once(self) -> CheckpointDeleteRecoveryReport:
        # The read transaction ends before any raw saver call below.
        async with self._session_factory() as session:
            candidates = await PrivateThreadRepository(
                session,
            ).list_checkpoint_delete_candidates(limit=self._batch_size)

        completed = 0
        retry_required = 0
        for candidate in candidates:
            try:
                recovered = await self.recover_candidate(candidate)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One malformed or concurrently removed item must not starve
                # unrelated tombstones. Never log private coordinates/content.
                recovered = False
            if recovered:
                completed += 1
            else:
                retry_required += 1
                logger.warning("One checkpoint deletion recovery item remains pending")
        return CheckpointDeleteRecoveryReport(
            selected=len(candidates),
            completed=completed,
            retry_required=retry_required,
        )

    async def recover_candidate(
        self,
        candidate: CheckpointDeleteCandidate,
    ) -> bool:
        """Recover one previously selected candidate under its DB fence."""

        return await recover_checkpoint_delete_candidate(
            self._raw_saver,
            self._session_factory,
            candidate,
        )

    async def _run_forever(self) -> None:
        while not self._closed:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Checkpoint deletion recovery pass deferred")
            await asyncio.sleep(self._interval_seconds)


@asynccontextmanager
async def checkpoint_delete_reconciler_runtime(
    raw_saver: BaseCheckpointSaver,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[CheckpointDeleteReconciler]:
    """Run recovery for exactly the lifetime of an open raw saver."""

    reconciler = CheckpointDeleteReconciler(raw_saver, session_factory)
    await reconciler.start()
    try:
        yield reconciler
    finally:
        await reconciler.aclose()


__all__ = [
    "CheckpointDeleteReconciler",
    "CheckpointDeleteRecoveryReport",
    "checkpoint_delete_candidate_from_record",
    "checkpoint_delete_reconciler_runtime",
    "recover_checkpoint_delete_candidate",
]
