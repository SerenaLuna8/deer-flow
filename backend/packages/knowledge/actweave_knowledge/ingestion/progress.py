"""Claim-guarded progress reporting for indexing task handlers.

The reporter owns the current attempt's stage and verified-unit counters and
persists every change in its own short transaction — never inside a
transaction that waits on provider I/O. Each write (and the read-only guard
used as the model client's ``batch_guard``) matches the exact claim token,
attempt, and target version, so a handler whose lease was re-claimed or whose
retry already started a newer attempt stops instead of dispatching further
provider batches or overwriting the current attempt's progress.
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import KNOWLEDGE_STORAGE_UNAVAILABLE, KNOWLEDGE_TASK_FAILED, KnowledgeError
from ..persistence.models import KnowledgeTaskRow
from ..persistence.tasks import update_task_progress
from ..tasks.worker import KnowledgeProjectInactive, KnowledgeTaskClaim, ProjectActiveCheck

logger = logging.getLogger(__name__)


def _storage_unavailable() -> KnowledgeError:
    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


def _claim_lost() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_TASK_FAILED, "任务租约已失效，停止执行")


async def ensure_locked_task_lease(session: AsyncSession, task: KnowledgeTaskRow) -> None:
    """Re-check database time after acquiring locks that could have waited."""

    alive = await session.scalar(select(KnowledgeTaskRow.lease_until > func.clock_timestamp()).where(KnowledgeTaskRow.id == task.id))
    if not alive:
        raise _claim_lost()


async def lock_indexing_claim(
    session: AsyncSession,
    claim: KnowledgeTaskClaim,
    *,
    project_active_check: ProjectActiveCheck | None,
) -> KnowledgeTaskRow:
    """Lock Project then Task before a batch, progress update, or publication."""

    if project_active_check is not None and not await project_active_check(session, claim.project_id):
        raise KnowledgeProjectInactive()
    task = await session.scalar(
        select(KnowledgeTaskRow)
        .where(
            KnowledgeTaskRow.id == claim.id,
            KnowledgeTaskRow.claim_token == claim.claim_token,
            KnowledgeTaskRow.status == "running",
            KnowledgeTaskRow.attempt_count == claim.attempt_count,
            KnowledgeTaskRow.target_version == claim.target_version,
            KnowledgeTaskRow.lease_until > func.clock_timestamp(),
        )
        .with_for_update()
    )
    if task is None:
        raise _claim_lost()
    await ensure_locked_task_lease(session, task)
    return task


class KnowledgeTaskProgressReporter:
    """Stage and verified-batch progress of one claimed indexing attempt."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        claim: KnowledgeTaskClaim,
        *,
        project_active_check: ProjectActiveCheck | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._claim = claim
        self._project_active_check = project_active_check
        self._stage = "queued"
        self._completed_units = 0
        self._total_units: int | None = None

    async def advance_stage(self, stage: str) -> None:
        """Enter ``stage`` keeping the current counters (``publishing`` keeps
        the completed embedding counts visible)."""

        self._stage = stage
        await self._write()

    async def begin_embedding(self, total_units: int) -> None:
        """Enter ``embedding`` with a verifiable total and zero verified units."""

        self._stage = "embedding"
        self._completed_units = 0
        self._total_units = total_units
        await self._write()

    async def add_verified_units(self, count: int) -> None:
        """Record one validated provider batch; the model client's
        ``on_batch_verified`` hook."""

        self._completed_units += count
        await self._write()

    async def ensure_claim_alive(self) -> None:
        """Read-only claim check; the model client's ``batch_guard`` hook.

        Raises instead of returning a flag so an expired or re-claimed lease
        stops undispatched provider batches — including the client's single
        internal retry.
        """

        try:
            async with self._session_factory() as session, session.begin():
                await lock_indexing_claim(session, self._claim, project_active_check=self._project_active_check)
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _write(self) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await lock_indexing_claim(session, self._claim, project_active_check=self._project_active_check)
                recorded = await update_task_progress(
                    session,
                    task_id=self._claim.id,
                    claim_token=self._claim.claim_token,
                    attempt_count=self._claim.attempt_count,
                    target_version=self._claim.target_version,
                    stage=self._stage,
                    completed_units=self._completed_units,
                    total_units=self._total_units,
                )
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if not recorded:
            raise _claim_lost()
