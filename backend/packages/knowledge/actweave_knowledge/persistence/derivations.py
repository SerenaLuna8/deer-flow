"""Shared SQL expressions deriving view fields from ``knowledge_*`` rows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import case, func, null, select
from sqlalchemy.sql.elements import ColumnElement

from ..contracts import KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError
from ..extraction.contracts import ProcessingProfile
from .models import KnowledgeDocumentRow, KnowledgeTaskRow

OPEN_TASK_STATUSES = ("queued", "running", "retry_wait")


def stored_model_text(
    *,
    content: str,
    index_text: str,
    parsing_profile: ProcessingProfile | dict[str, object] | None,
) -> str:
    """Return the persisted model input, with one narrow legacy fallback.

    Token-era rows must carry their derived ``index_text``.  Only rows from
    before that contract (a NULL profile or an explicit character profile)
    may deterministically rebuild model text from their already stored
    Markdown.  This adapter never reads the original file or invents a parser
    identity.
    """

    if isinstance(index_text, str) and index_text.strip():
        return index_text

    legacy = parsing_profile is None
    if parsing_profile is not None:
        try:
            profile = parsing_profile if isinstance(parsing_profile, ProcessingProfile) else ProcessingProfile.model_validate(parsing_profile)
        except (TypeError, ValueError):
            profile = None
        legacy = profile is not None and profile.chunk.unit == "character"

    if legacy:
        # Kept lazy because importing the ``ingestion`` package while the
        # persistence helpers initialize would pull DocumentService back into
        # this module through the preview exports.
        from ..ingestion.index_text import build_index_text

        derived = build_index_text(content)
        if derived:
            return derived

    raise KnowledgeError(
        KNOWLEDGE_STORAGE_UNAVAILABLE,
        "已发布索引文本不可用",
    )


def delete_error_expression(
    kind: str | Sequence[str],
    resource_id_column: ColumnElement[Any],
) -> ColumnElement[str | None]:
    """Latest failed delete-task message, hidden while a retry is open.

    While an open delete task exists the deletion is still in progress, so the
    view shows no error; otherwise the most recently finished failed task of
    ``kind`` explains why the resource is stuck.
    """

    kinds = (kind,) if isinstance(kind, str) else tuple(kind)
    kind_filter = KnowledgeTaskRow.kind.in_(kinds)
    open_delete_exists = (
        select(KnowledgeTaskRow.id)
        .where(
            kind_filter,
            KnowledgeTaskRow.resource_id == resource_id_column,
            KnowledgeTaskRow.status.in_(OPEN_TASK_STATUSES),
        )
        .exists()
    )
    latest_failed_error = (
        select(KnowledgeTaskRow.error_message)
        .where(
            kind_filter,
            KnowledgeTaskRow.resource_id == resource_id_column,
            KnowledgeTaskRow.status == "failed",
        )
        .order_by(KnowledgeTaskRow.finished_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    return case((open_delete_exists, null()), else_=latest_failed_error)


def document_delete_error_expression(
    resource_id_column: ColumnElement[Any],
) -> ColumnElement[str | None]:
    """Derive one Document's error across normal and orphan-object cleanup."""

    return delete_error_expression(
        ("delete_document", "delete_document_object"),
        resource_id_column,
    )


def document_count_expression(base_id_column: ColumnElement[Any]) -> ColumnElement[int]:
    """Number of document rows currently attached to the knowledge base."""

    return select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id_column).scalar_subquery()


def live_document_count_expression(base_id_column: ColumnElement[Any]) -> ColumnElement[int]:
    """Attached documents not being deleted: the rows that hold the base's chunking mode.

    While this is zero the base's stored ``chunking_mode`` is undetermined
    again — the next upload fixes it — so views project it as ``NULL``.
    """

    return (
        select(func.count())
        .select_from(KnowledgeDocumentRow)
        .where(
            KnowledgeDocumentRow.knowledge_base_id == base_id_column,
            KnowledgeDocumentRow.status != "deleting",
        )
        .scalar_subquery()
    )
