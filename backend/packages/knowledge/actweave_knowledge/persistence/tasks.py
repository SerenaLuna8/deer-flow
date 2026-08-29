"""Single-row claim/lease queue operations over ``knowledge_tasks``.

The claim selects one due row with ``FOR UPDATE SKIP LOCKED`` so concurrent
Workers never take the same task, and an expired ``running`` lease becomes
claimable again without an Attempt-history table. Settlement is always guarded
by the claim token, so a stale Worker whose lease was re-claimed can neither
settle nor extend the row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import KnowledgeDocumentRow, KnowledgeTaskRow

TASK_OPEN_STATUSES = ("queued", "running", "retry_wait")

_CLAIMABLE_STATUSES = ("queued", "retry_wait")
_EXPIRED_LEASE_MESSAGE = "任务租约到期，Worker 可能已中断"


async def claim_next_task(
    session: AsyncSession,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> KnowledgeTaskRow | None:
    """Claim the oldest due task inside the caller's transaction.

    Returns the claimed row already flipped to ``running`` with a fresh
    ``claim_token`` and lease, or ``None`` when nothing is due.
    """

    moment = now or datetime.now(UTC)
    candidate = (
        select(KnowledgeTaskRow.id)
        .where(
            # Claiming always spends one attempt, so a row is claimable only
            # while attempts remain; exhausted rows (including expired
            # ``running`` leases) are settled by the executor instead.
            KnowledgeTaskRow.attempt_count < KnowledgeTaskRow.max_attempts,
            or_(
                KnowledgeTaskRow.status.in_(_CLAIMABLE_STATUSES) & (KnowledgeTaskRow.available_at <= moment),
                (KnowledgeTaskRow.status == "running") & (KnowledgeTaskRow.lease_until <= moment),
            ),
        )
        .order_by(KnowledgeTaskRow.available_at, KnowledgeTaskRow.created_at, KnowledgeTaskRow.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task_id: UUID | None = await session.scalar(candidate)
    if task_id is None:
        return None

    claim_token = uuid4()
    await session.execute(
        update(KnowledgeTaskRow)
        .where(KnowledgeTaskRow.id == task_id)
        .values(
            status="running",
            claim_token=claim_token,
            lease_until=moment + timedelta(seconds=lease_seconds),
            attempt_count=KnowledgeTaskRow.attempt_count + 1,
            updated_at=moment,
        )
    )
    # The row was never in the identity map, so this SELECT already sees the
    # transaction's own UPDATE; no refresh needed.
    row = await session.get(KnowledgeTaskRow, task_id)
    if row is None:  # pragma: no cover - row is locked by this transaction
        return None
    return row


async def extend_task_lease(
    session: AsyncSession,
    task_id: UUID,
    claim_token: UUID,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Extend the lease of a running claim; False when the claim was lost."""

    moment = now or datetime.now(UTC)
    result = await session.execute(
        update(KnowledgeTaskRow)
        .where(
            KnowledgeTaskRow.id == task_id,
            KnowledgeTaskRow.claim_token == claim_token,
            KnowledgeTaskRow.status == "running",
        )
        .values(lease_until=moment + timedelta(seconds=lease_seconds), updated_at=moment)
    )
    return result.rowcount == 1


def settle_task_row_success(row: KnowledgeTaskRow, *, now: datetime) -> None:
    """Flip a locked, still-owned ``running`` row to ``succeeded`` in place.

    For handlers that settle inside their own publish transaction; the caller
    must have loaded ``row`` FOR UPDATE with the claim-token guard.
    """

    row.status = "succeeded"
    row.claim_token = None
    row.lease_until = None
    row.error_message = None
    row.finished_at = now
    row.updated_at = now


async def settle_task_success(
    session: AsyncSession,
    task_id: UUID,
    claim_token: UUID,
    *,
    now: datetime | None = None,
) -> bool:
    """Settle a running claim as succeeded; False when the claim was lost."""

    moment = now or datetime.now(UTC)
    result = await session.execute(
        update(KnowledgeTaskRow)
        .where(
            KnowledgeTaskRow.id == task_id,
            KnowledgeTaskRow.claim_token == claim_token,
            KnowledgeTaskRow.status == "running",
        )
        .values(
            status="succeeded",
            claim_token=None,
            lease_until=None,
            error_message=None,
            finished_at=moment,
            updated_at=moment,
        )
    )
    return result.rowcount == 1


async def settle_task_failure(
    session: AsyncSession,
    task_id: UUID,
    claim_token: UUID,
    *,
    error_message: str,
    retry_delay_seconds: int,
    now: datetime | None = None,
) -> str | None:
    """Settle a running claim after a failed attempt.

    Returns the resulting status: ``"retry_wait"`` while attempts remain,
    ``"failed"`` when the budget is spent, or ``None`` when the claim was
    lost. A finally-failed ``ingest_document`` task also marks its document
    ``failed`` so the failure is explainable in document views.
    """

    moment = now or datetime.now(UTC)
    row = await session.scalar(
        select(KnowledgeTaskRow)
        .where(
            KnowledgeTaskRow.id == task_id,
            KnowledgeTaskRow.claim_token == claim_token,
            KnowledgeTaskRow.status == "running",
        )
        .with_for_update()
    )
    if row is None:
        return None
    row.claim_token = None
    row.lease_until = None
    row.error_message = error_message
    row.updated_at = moment
    if row.attempt_count >= row.max_attempts:
        row.status = "failed"
        row.finished_at = moment
        if row.kind == "ingest_document":
            await _mark_ingest_document_failed(
                session,
                document_id=row.resource_id,
                target_version=row.target_version,
                error_message=error_message,
                now=moment,
            )
        return "failed"
    row.status = "retry_wait"
    row.available_at = moment + timedelta(seconds=retry_delay_seconds)
    return "retry_wait"


async def recover_expired_tasks(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Settle expired ``running`` leases left behind by interrupted Workers.

    Rows with attempts remaining return to ``retry_wait`` (immediately due);
    exhausted rows become ``failed``, and an exhausted ingest additionally
    marks its document ``failed``. Returns the number of rows recovered.
    """

    moment = now or datetime.now(UTC)
    expired = (await session.execute(select(KnowledgeTaskRow).where(KnowledgeTaskRow.status == "running", KnowledgeTaskRow.lease_until <= moment).with_for_update(skip_locked=True))).scalars().all()
    for row in expired:
        row.claim_token = None
        row.lease_until = None
        row.updated_at = moment
        if row.attempt_count >= row.max_attempts:
            row.status = "failed"
            # The expiry is what terminated the final attempt; a message kept
            # from an earlier retry would misattribute the permanent failure.
            row.error_message = _EXPIRED_LEASE_MESSAGE
            row.finished_at = moment
            if row.kind == "ingest_document":
                await _mark_ingest_document_failed(
                    session,
                    document_id=row.resource_id,
                    target_version=row.target_version,
                    error_message=row.error_message,
                    now=moment,
                )
        else:
            row.status = "retry_wait"
            row.available_at = moment
    return len(expired)


async def _mark_ingest_document_failed(
    session: AsyncSession,
    *,
    document_id: UUID,
    target_version: int | None,
    error_message: str,
    now: datetime,
) -> None:
    """Mark the ingested document failed if it still matches the task version."""

    await session.execute(
        update(KnowledgeDocumentRow)
        .where(
            KnowledgeDocumentRow.id == document_id,
            KnowledgeDocumentRow.version == target_version,
            KnowledgeDocumentRow.status.in_(("queued", "processing")),
        )
        .values(status="failed", error_message=error_message, updated_at=now)
    )
