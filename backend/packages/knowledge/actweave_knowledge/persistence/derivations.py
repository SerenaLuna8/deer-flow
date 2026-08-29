"""Shared SQL expressions deriving view fields from ``knowledge_*`` rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, null, select
from sqlalchemy.sql.elements import ColumnElement

from .models import KnowledgeDocumentRow, KnowledgeTaskRow

OPEN_TASK_STATUSES = ("queued", "running", "retry_wait")


def delete_error_expression(kind: str, resource_id_column: ColumnElement[Any]) -> ColumnElement[str | None]:
    """Latest failed delete-task message, hidden while a retry is open.

    While an open delete task exists the deletion is still in progress, so the
    view shows no error; otherwise the most recently finished failed task of
    ``kind`` explains why the resource is stuck.
    """

    open_delete_exists = (
        select(KnowledgeTaskRow.id)
        .where(
            KnowledgeTaskRow.kind == kind,
            KnowledgeTaskRow.resource_id == resource_id_column,
            KnowledgeTaskRow.status.in_(OPEN_TASK_STATUSES),
        )
        .exists()
    )
    latest_failed_error = (
        select(KnowledgeTaskRow.error_message)
        .where(
            KnowledgeTaskRow.kind == kind,
            KnowledgeTaskRow.resource_id == resource_id_column,
            KnowledgeTaskRow.status == "failed",
        )
        .order_by(KnowledgeTaskRow.finished_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    return case((open_delete_exists, null()), else_=latest_failed_error)


def document_count_expression(base_id_column: ColumnElement[Any]) -> ColumnElement[int]:
    """Number of document rows currently attached to the knowledge base."""

    return select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id_column).scalar_subquery()
