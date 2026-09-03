"""Single-row claim/lease queue operations over ``knowledge_tasks``.

The claim selects one due row with ``FOR UPDATE SKIP LOCKED`` so concurrent
Workers never take the same task, and an expired ``running`` lease becomes
claimable again without an Attempt-history table. Settlement is always guarded
by the claim token, so a stale Worker whose lease was re-claimed can neither
settle nor extend the row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeTaskRow

if TYPE_CHECKING:
    from ..tasks.worker import KnowledgeTaskClaim


async def lock_extraction_claim(session: AsyncSession, claim: KnowledgeTaskClaim) -> tuple[KnowledgeTaskRow, KnowledgeDocumentRow]:
    """Lock Task -> Base -> Document after the caller's required Project fence.

    The caller must invoke its injected ProjectActiveCheck in this same
    transaction first. This package helper never reconstructs host authority.
    """
    from ..contracts import KNOWLEDGE_CONFLICT, KNOWLEDGE_TASK_FAILED, KnowledgeError

    locked = await _lock_live_task_claim(session, claim.id, claim.claim_token)
    if locked is None:
        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")
    task, _ = locked
    if (task.project_id, task.resource_id, task.kind, task.target_version, task.attempt_count) != (claim.project_id, claim.resource_id, claim.kind, claim.target_version, claim.attempt_count):
        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务身份已失效")
    base_id = await session.scalar(select(KnowledgeDocumentRow.knowledge_base_id).where(KnowledgeDocumentRow.id == task.resource_id, KnowledgeDocumentRow.project_id == task.project_id))
    base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.id == base_id, KnowledgeBaseRow.project_id == task.project_id).with_for_update())
    document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == task.resource_id, KnowledgeDocumentRow.project_id == task.project_id).with_for_update().execution_options(populate_existing=True))
    if base is None or base.status == "deleting" or document is None or document.status == "deleting" or document.knowledge_base_id != base.id or document.version != claim.target_version or task.kind not in VERSIONED_TASK_KINDS:
        raise KnowledgeError(KNOWLEDGE_CONFLICT, "Document 或版本已变更")
    moment = await session.scalar(select(func.clock_timestamp()))
    if task.lease_until is None or task.lease_until <= moment:
        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")
    return task, document


TASK_OPEN_STATUSES = ("queued", "running", "retry_wait")

# Kinds whose permanent failure must surface on the document itself: the
# document was taken out of ``ready`` for this work, so an exhausted task
# marks it ``failed`` (rows and published_version stay untouched).
INDEXING_TASK_KINDS = ("ingest_document", "reembed_document")
# Summaries and lexical re-derivation share generation admission/lease
# fencing, but never take a published Document out of ready or mark it failed
# when attempts expire.
VERSIONED_TASK_KINDS = (*INDEXING_TASK_KINDS, "summarize_document", "relex_document")

_CLAIMABLE_STATUSES = ("queued", "retry_wait")
_EXPIRED_LEASE_MESSAGE = "任务租约到期，Worker 可能已中断"
DEFAULT_INACTIVE_PROJECT_PAUSE_SECONDS = 60


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
            extraction_id=None,
            lease_until=moment + timedelta(seconds=lease_seconds),
            attempt_count=KnowledgeTaskRow.attempt_count + 1,
            # A new attempt starts from zero: stale progress of a lost or
            # failed attempt never accumulates into this one.
            stage="queued",
            completed_units=0,
            total_units=None,
            progress_updated_at=moment,
            updated_at=moment,
        )
    )
    # The row was never in the identity map, so this SELECT already sees the
    # transaction's own UPDATE; no refresh needed.
    row = await session.get(KnowledgeTaskRow, task_id)
    if row is None:  # pragma: no cover - row is locked by this transaction
        return None
    return row


async def _lock_live_task_claim(
    session: AsyncSession,
    task_id: UUID,
    claim_token: UUID,
    *,
    now: datetime | None = None,
) -> tuple[KnowledgeTaskRow, datetime] | None:
    """Lock the exact claim, then check its deadline against current time.

    A lease predicate in the locking SELECT or UPDATE is evaluated before a
    pure row-lock wait. Read the database clock only after acquiring the lock,
    so a claim that expired during that wait cannot be revived or settled.
    Explicit ``now`` remains the deterministic clock for repository tests.
    """

    row = await session.scalar(
        select(KnowledgeTaskRow)
        .where(
            KnowledgeTaskRow.id == task_id,
            KnowledgeTaskRow.claim_token == claim_token,
            KnowledgeTaskRow.status == "running",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    moment = now if now is not None else await session.scalar(select(func.clock_timestamp()))
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise RuntimeError("database task clock is unavailable")
    if row.lease_until is None or row.lease_until <= moment:
        return None
    return row, moment


async def extend_task_lease(
    session: AsyncSession,
    task_id: UUID,
    claim_token: UUID,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Extend the lease of a running claim; False when the claim was lost."""

    locked = await _lock_live_task_claim(session, task_id, claim_token, now=now)
    if locked is None:
        return False
    row, moment = locked
    row.lease_until = moment + timedelta(seconds=lease_seconds)
    row.updated_at = moment
    return True


async def defer_task_claim_for_inactive_project(
    session: AsyncSession,
    row: KnowledgeTaskRow,
    *,
    pause_seconds: int = DEFAULT_INACTIVE_PROJECT_PAUSE_SECONDS,
) -> bool:
    """Return an inactive Project's task without spending its current attempt.

    The caller owns ``row`` through the claim transaction and has already
    established that its Project is not active. PostgreSQL stamps the bounded
    pause consistently with the durable queue state.
    """

    if type(pause_seconds) is not int or not 1 <= pause_seconds <= 3600:
        raise ValueError("inactive Project pause must be between 1 and 3600 seconds")
    if row.status != "running" or row.claim_token is None or row.lease_until is None or row.attempt_count < 1:
        raise RuntimeError("inactive Project deferral requires a freshly claimed task")
    database_now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(database_now, datetime) or database_now.tzinfo is None:
        raise RuntimeError("database task clock is unavailable")
    if row.lease_until <= database_now:
        return False
    row.status = "retry_wait"
    row.extraction_id = None
    row.attempt_count -= 1
    row.available_at = database_now + timedelta(seconds=pause_seconds)
    row.claim_token = None
    row.lease_until = None
    row.finished_at = None
    row.updated_at = database_now
    return True


async def defer_running_task_for_inactive_project(session: AsyncSession, task_id: UUID, claim_token: UUID) -> bool:
    """Pause an exact live claim after its handler has finished draining."""

    locked = await _lock_live_task_claim(session, task_id, claim_token)
    if locked is None:
        return False
    row, _moment = locked
    return await defer_task_claim_for_inactive_project(session, row)


def settle_task_row_success(row: KnowledgeTaskRow, *, now: datetime) -> None:
    """Flip a locked, still-owned ``running`` row to ``succeeded`` in place.

    For handlers that settle inside their own publish transaction; the caller
    must have loaded ``row`` FOR UPDATE with the claim-token guard. ``done``
    is stamped here — with the publish commit — and nowhere earlier.
    """

    row.status = "succeeded"
    row.extraction_id = None
    row.stage = "done"
    row.claim_token = None
    row.lease_until = None
    row.error_message = None
    row.finished_at = now
    row.progress_updated_at = now
    row.updated_at = now


async def settle_task_success(
    session: AsyncSession,
    task_id: UUID,
    claim_token: UUID,
    *,
    now: datetime | None = None,
) -> bool:
    """Settle a running claim as succeeded; False when the claim was lost."""

    locked = await _lock_live_task_claim(session, task_id, claim_token, now=now)
    if locked is None:
        return False
    row, moment = locked
    settle_task_row_success(row, now=moment)
    return True


async def update_task_progress(
    session: AsyncSession,
    *,
    task_id: UUID,
    claim_token: UUID,
    attempt_count: int,
    target_version: int | None,
    stage: str,
    completed_units: int,
    total_units: int | None,
    now: datetime | None = None,
) -> bool:
    """Persist verified progress of the current attempt; False when stale.

    The update matches only the row still owned by this exact claim, attempt,
    and target version, so progress arriving after the lease was re-claimed
    (or after a retry started a newer attempt) can never overwrite the
    current attempt's counters.
    """

    locked = await _lock_live_task_claim(session, task_id, claim_token, now=now)
    if locked is None:
        return False
    row, moment = locked
    if row.attempt_count != attempt_count or row.target_version != target_version:
        return False
    row.stage = stage
    row.completed_units = completed_units
    row.total_units = total_units
    row.progress_updated_at = moment
    row.updated_at = moment
    return True


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
    lost. A finally-failed indexing task (ingest or re-embed) also marks its
    document ``failed`` so the failure is explainable in document views.
    """

    locked = await _lock_live_task_claim(session, task_id, claim_token, now=now)
    if locked is None:
        return None
    row, moment = locked
    row.claim_token = None
    row.extraction_id = None
    row.lease_until = None
    row.error_message = error_message
    row.updated_at = moment
    if row.attempt_count >= row.max_attempts:
        row.status = "failed"
        row.finished_at = moment
        if row.kind in INDEXING_TASK_KINDS:
            await _mark_indexed_document_failed(
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
    project_id: UUID | None = None,
    now: datetime | None = None,
) -> int:
    """Settle expired ``running`` leases left behind by interrupted Workers.

    Rows with attempts remaining return to ``retry_wait`` (immediately due);
    exhausted rows become ``failed``, and an exhausted ingest additionally
    marks its document ``failed``. Returns the number of rows recovered.
    """

    moment = now or datetime.now(UTC)
    filters = [
        KnowledgeTaskRow.status == "running",
        KnowledgeTaskRow.lease_until <= moment,
    ]
    if project_id is not None:
        filters.append(KnowledgeTaskRow.project_id == project_id)
    expired = (await session.execute(select(KnowledgeTaskRow).where(*filters).with_for_update(skip_locked=True))).scalars().all()
    for row in expired:
        row.claim_token = None
        row.extraction_id = None
        row.lease_until = None
        row.updated_at = moment
        if row.attempt_count >= row.max_attempts:
            row.status = "failed"
            # The expiry is what terminated the final attempt; a message kept
            # from an earlier retry would misattribute the permanent failure.
            row.error_message = _EXPIRED_LEASE_MESSAGE
            row.finished_at = moment
            if row.kind in INDEXING_TASK_KINDS:
                await _mark_indexed_document_failed(
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


async def _mark_indexed_document_failed(
    session: AsyncSession,
    *,
    document_id: UUID,
    target_version: int | None,
    error_message: str,
    now: datetime,
) -> None:
    """Mark the indexed document failed if it still matches the task version."""

    await session.execute(
        update(KnowledgeDocumentRow)
        .where(
            KnowledgeDocumentRow.id == document_id,
            KnowledgeDocumentRow.version == target_version,
            KnowledgeDocumentRow.status.in_(("queued", "processing")),
        )
        .values(status="failed", error_message=error_message, updated_at=now)
    )


def validated_reparse_settings(value: dict) -> dict:
    """Project the exact frozen profile, rejecting legacy/inconsistent task data."""
    import re

    from ..extraction.contracts import ExtractionError, ProcessingProfile
    from ..ingestion.profiles import ProcessingParameters, chunk_settings

    try:
        profile = ProcessingProfile.model_validate(value["processing_profile"])
        ProcessingParameters(**profile.chunk.model_dump(exclude={"tokenizer_profile_id", "tokenizer_digest", "cleaner_version", "splitter_version"}), header_rules=profile.parse.header_rules)
        projected = chunk_settings(profile)
        revision = value["capability_revision"]
        if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
            raise ValueError
        if set(value) != set(projected) | {"processing_profile", "capability_revision"}:
            raise ValueError
        if any(type(value[key]) is not type(expected) or value[key] != expected for key, expected in projected.items()):
            raise ValueError
        return {**projected, "processing_profile": profile.model_dump(mode="json"), "capability_revision": revision}
    except (KeyError, TypeError, ValueError):
        raise ExtractionError("PROCESSING_PROFILE_UNAVAILABLE", "原解析配置已不可用，请显式重新解析") from None
