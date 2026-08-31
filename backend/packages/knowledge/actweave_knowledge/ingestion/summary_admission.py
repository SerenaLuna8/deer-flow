"""Small transaction-owned admission seam shared by summary refresh triggers."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts import KNOWLEDGE_MODEL_UNAVAILABLE, KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS, KnowledgeError, KnowledgeModelPort
from ..persistence.models import KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeSegmentRow, KnowledgeTaskRow
from ..persistence.tasks import TASK_OPEN_STATUSES, VERSIONED_TASK_KINDS


async def enqueue_summary_refresh(session: AsyncSession, document: KnowledgeDocumentRow, model_port: KnowledgeModelPort) -> bool:
    """Enqueue at most one refresh while the caller owns the Document lock.

    A configured model that has since become unavailable is still admitted:
    its summary task fails visibly without rolling back source publication.
    An entirely unconfigured model does not start background work.
    """
    base = await session.get(KnowledgeBaseRow, document.knowledge_base_id)
    if base is None or not base.summary_index_enabled or base.status != "active" or document.status != "ready":
        return False
    if await session.scalar(select(KnowledgeTaskRow.id).where(KnowledgeTaskRow.resource_id == document.id, KnowledgeTaskRow.kind.in_(VERSIONED_TASK_KINDS), KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES)).limit(1)):
        return False
    if not await session.scalar(
        select(KnowledgeSegmentRow.id)
        .where(KnowledgeSegmentRow.knowledge_document_id == document.id, KnowledgeSegmentRow.document_version == document.version, func.length(KnowledgeSegmentRow.content) >= KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS)
        .limit(1)
    ):
        return False
    try:
        if await model_port.resolve_summary_model(session) is None:
            return False
    except KnowledgeError as exc:
        if exc.code != KNOWLEDGE_MODEL_UNAVAILABLE:
            raise
    session.add(KnowledgeTaskRow(id=uuid4(), project_id=document.project_id, resource_id=document.id, kind="summarize_document", target_version=document.version))
    return True
