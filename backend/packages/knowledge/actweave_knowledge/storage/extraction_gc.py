"""Bounded database-clock admission for durable Extraction cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..persistence.models import KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeExtractionRow, KnowledgeTaskRow
from ..persistence.tasks import TASK_OPEN_STATUSES, recover_expired_tasks

if TYPE_CHECKING:
    from ..tasks.worker import ProjectActiveCheck

_GC_LIMIT = 100


async def _is_pinned(session: AsyncSession, extraction_id: UUID) -> bool:
    return (
        await session.scalar(
            select(KnowledgeTaskRow.id)
            .where(
                KnowledgeTaskRow.extraction_id == extraction_id,
                KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
            )
            .limit(1)
        )
        is not None
    )


async def _has_live_creator(
    session: AsyncSession,
    row: KnowledgeExtractionRow,
    *,
    now: datetime,
) -> bool:
    return (
        await session.scalar(
            select(KnowledgeTaskRow.id)
            .where(
                KnowledgeTaskRow.id == row.created_task_id,
                KnowledgeTaskRow.project_id == row.project_id,
                KnowledgeTaskRow.resource_id == row.knowledge_document_id,
                KnowledgeTaskRow.status == "running",
                KnowledgeTaskRow.claim_token == row.created_claim_token,
                KnowledgeTaskRow.attempt_count == row.created_attempt,
                KnowledgeTaskRow.lease_until > now,
            )
            .limit(1)
        )
        is not None
    )


async def _admit(session: AsyncSession, row: KnowledgeExtractionRow) -> bool:
    row.state = "deleting"
    result = await session.execute(
        pg_insert(KnowledgeTaskRow)
        .values(
            id=uuid4(),
            project_id=row.project_id,
            resource_id=row.id,
            kind="delete_extraction",
            target_version=None,
            storage_key=None,
            status="queued",
        )
        .on_conflict_do_nothing(
            index_elements=[KnowledgeTaskRow.resource_id],
            index_where=text("kind = 'delete_extraction' AND status IN ('queued', 'running', 'retry_wait')"),
        )
    )
    return result.rowcount == 1


async def enqueue_extraction_gc(
    session: AsyncSession,
    *,
    project_active_check: ProjectActiveCheck,
    project_id: UUID | None = None,
    limit: int = _GC_LIMIT,
) -> int:
    """Admit at most ``limit`` cleanup tasks without performing object I/O."""

    if type(limit) is not int or not 1 <= limit <= _GC_LIMIT:
        raise ValueError("Extraction GC limit must be between 1 and 100")
    now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise RuntimeError("database Extraction GC clock is unavailable")
    stale_before = now - timedelta(days=1)
    latest = aliased(KnowledgeExtractionRow)
    pin = aliased(KnowledgeTaskRow)
    creator = aliased(KnowledgeTaskRow)
    latest_unpublished_id = (
        select(latest.id)
        .where(
            latest.knowledge_document_id == KnowledgeExtractionRow.knowledge_document_id,
            latest.state == "ready",
            or_(
                KnowledgeDocumentRow.published_extraction_id.is_(None),
                latest.id != KnowledgeDocumentRow.published_extraction_id,
            ),
        )
        .order_by(latest.completed_at.desc(), latest.id.desc())
        .limit(1)
        .correlate(KnowledgeExtractionRow, KnowledgeDocumentRow)
        .scalar_subquery()
    )
    has_live_pin = exists(
        select(pin.id).where(
            pin.extraction_id == KnowledgeExtractionRow.id,
            or_(
                pin.status.in_(("queued", "retry_wait")),
                (pin.status == "running") & (pin.lease_until > now),
            ),
        )
    )
    has_live_creator = exists(
        select(creator.id).where(
            creator.id == KnowledgeExtractionRow.created_task_id,
            creator.project_id == KnowledgeExtractionRow.project_id,
            creator.resource_id == KnowledgeExtractionRow.knowledge_document_id,
            creator.status == "running",
            creator.claim_token == KnowledgeExtractionRow.created_claim_token,
            creator.attempt_count == KnowledgeExtractionRow.created_attempt,
            creator.lease_until > now,
        )
    )
    candidate_filter = (
        or_(
            KnowledgeDocumentRow.published_extraction_id.is_(None),
            KnowledgeExtractionRow.id != KnowledgeDocumentRow.published_extraction_id,
        )
        & ~has_live_pin
        & or_(
            KnowledgeExtractionRow.state == "deleting",
            (KnowledgeExtractionRow.state == "staging") & (KnowledgeExtractionRow.created_at <= stale_before) & ~has_live_creator,
            (KnowledgeExtractionRow.state == "ready")
            & or_(
                KnowledgeExtractionRow.unpublished_expires_at <= now,
                KnowledgeExtractionRow.id != latest_unpublished_id,
            ),
        )
    )
    if project_id is None:
        project_ids = list(
            (
                await session.scalars(
                    select(KnowledgeExtractionRow.project_id)
                    .join(
                        KnowledgeDocumentRow,
                        KnowledgeDocumentRow.id == KnowledgeExtractionRow.knowledge_document_id,
                    )
                    .where(candidate_filter)
                    .distinct()
                    .order_by(KnowledgeExtractionRow.project_id)
                    .limit(limit)
                )
            ).all()
        )
    else:
        project_ids = [project_id]

    admitted = 0
    for candidate_project_id in sorted(project_ids):
        if admitted >= limit:
            break
        if not await project_active_check(session, candidate_project_id):
            continue
        await recover_expired_tasks(session, project_id=candidate_project_id, now=now)
        candidates = (
            await session.execute(
                select(
                    KnowledgeExtractionRow.id,
                    KnowledgeExtractionRow.knowledge_base_id,
                    KnowledgeExtractionRow.knowledge_document_id,
                )
                .join(
                    KnowledgeDocumentRow,
                    KnowledgeDocumentRow.id == KnowledgeExtractionRow.knowledge_document_id,
                )
                .where(
                    KnowledgeExtractionRow.project_id == candidate_project_id,
                    candidate_filter,
                )
                .order_by(
                    KnowledgeExtractionRow.knowledge_document_id,
                    KnowledgeExtractionRow.id,
                )
                .limit(limit - admitted)
            )
        ).all()
        for extraction_id, base_id, document_id in candidates:
            if admitted >= limit:
                break
            base = await session.scalar(
                select(KnowledgeBaseRow)
                .where(
                    KnowledgeBaseRow.id == base_id,
                    KnowledgeBaseRow.project_id == candidate_project_id,
                )
                .with_for_update(skip_locked=True)
            )
            if base is None:
                continue
            document = await session.scalar(
                select(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.id == document_id,
                    KnowledgeDocumentRow.project_id == candidate_project_id,
                    KnowledgeDocumentRow.knowledge_base_id == base_id,
                )
                .with_for_update(skip_locked=True)
            )
            if document is None:
                continue
            row = await session.scalar(
                select(KnowledgeExtractionRow)
                .where(
                    KnowledgeExtractionRow.id == extraction_id,
                    KnowledgeExtractionRow.project_id == candidate_project_id,
                    KnowledgeExtractionRow.knowledge_base_id == base_id,
                    KnowledgeExtractionRow.knowledge_document_id == document_id,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if row is None or document.published_extraction_id == row.id or await _is_pinned(session, row.id):
                continue
            latest_ready_id = await session.scalar(
                select(KnowledgeExtractionRow.id)
                .where(
                    KnowledgeExtractionRow.knowledge_document_id == document_id,
                    KnowledgeExtractionRow.state == "ready",
                    KnowledgeExtractionRow.id != document.published_extraction_id,
                )
                .order_by(
                    KnowledgeExtractionRow.completed_at.desc(),
                    KnowledgeExtractionRow.id.desc(),
                )
                .limit(1)
            )
            eligible = (
                row.state == "deleting"
                or (row.state == "staging" and row.created_at <= stale_before and not await _has_live_creator(session, row, now=now))
                or (row.state == "ready" and ((row.unpublished_expires_at is not None and row.unpublished_expires_at <= now) or row.id != latest_ready_id))
            )
            if not eligible:
                continue
            if await _admit(session, row):
                admitted += 1
    return admitted
