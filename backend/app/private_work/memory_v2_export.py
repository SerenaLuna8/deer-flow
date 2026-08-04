"""Allowlisted streaming export records for owner-private Memory v2."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.private_work.memory_v2_model import (
    MemoryCandidateRow,
    MemoryConsolidationGenerationRow,
    MemoryContextSummaryRow,
    MemoryExtractionGenerationRow,
    MemoryFactEvidenceRow,
    MemoryFactRevisionRow,
    MemoryFactRow,
    MemorySourceBatchRow,
    MemorySourceItemRow,
    MemorySuppressionRow,
    RunMemoryContextItemRow,
    RunMemoryContextSnapshotRow,
)

_STREAM_BATCH_SIZE = 100


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Memory export timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


async def _rows(
    session: AsyncSession,
    row_type,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    namespace: str | None,
):
    statement = select(row_type).where(
        row_type.project_id == project_id,
        row_type.owner_user_id == owner_user_id,
    )
    if namespace is not None:
        statement = statement.where(row_type.namespace == namespace)
    statement = statement.order_by(row_type.created_at, row_type.id)
    stream = await session.stream_scalars(statement.execution_options(yield_per=_STREAM_BATCH_SIZE))
    try:
        async for row in stream:
            yield row
    finally:
        await stream.close()


def _source_batch(row: MemorySourceBatchRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "thread_id": row.thread_id,
        "run_id": row.run_id,
        "pipeline_mode": row.pipeline_mode,
        "source_item_count": row.source_item_count,
        "suppressed": row.suppressed_at is not None,
        "suppression_reason": row.suppression_reason,
        "created_at": _iso(row.created_at),
    }


def _source_item(row: MemorySourceItemRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "source_batch_id": str(row.source_batch_id),
        "ordinal": row.ordinal,
        "source_message_id": row.source_message_id,
        "run_event_sequence": row.run_event_sequence,
        "role": row.role,
        "content": row.content,
        "source_erased_at": _iso(row.source_erased_at),
        "created_at": _iso(row.created_at),
    }


def _extraction(row: MemoryExtractionGenerationRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "source_batch_id": str(row.source_batch_id),
        "candidate_committed_at": _iso(row.candidate_committed_at),
        "created_at": _iso(row.created_at),
    }


def _consolidation(row: MemoryConsolidationGenerationRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "candidate_count": row.candidate_count,
        "fact_committed_at": _iso(row.fact_committed_at),
        "created_at": _iso(row.created_at),
    }


def _candidate(row: MemoryCandidateRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "source_batch_id": str(row.source_batch_id),
        "source_item_id": (None if row.source_item_id is None else str(row.source_item_id)),
        "candidate_type": row.candidate_type,
        "content": row.content,
        "confidence": row.confidence,
        "retention_class": row.retention_class,
        "sensitivity": row.sensitivity,
        "status": row.status,
        "decision_reason": row.decision_reason,
        "decided_at": _iso(row.decided_at),
        "content_erased_at": _iso(row.content_erased_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _fact(row: MemoryFactRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "fact_kind": row.fact_kind,
        "status": row.status,
        "current_revision_id": str(row.current_revision_id),
        "version": row.version,
        "disabled_at": _iso(row.disabled_at),
        "superseded_at": _iso(row.superseded_at),
        "deleted_at": _iso(row.deleted_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _revision(row: MemoryFactRevisionRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "fact_id": str(row.fact_id),
        "revision_number": row.revision_number,
        "revision_sequence": row.revision_sequence,
        "content": row.content,
        "category": row.category,
        "confidence": row.confidence,
        "valid_from": _iso(row.valid_from),
        "valid_to": _iso(row.valid_to),
        "last_confirmed_at": _iso(row.last_confirmed_at),
        "changed_by": row.changed_by,
        "source_candidate_id": (None if row.source_candidate_id is None else str(row.source_candidate_id)),
        "supersedes_revision_id": (None if row.supersedes_revision_id is None else str(row.supersedes_revision_id)),
        "change_reason": row.change_reason,
        "content_erased_at": _iso(row.content_erased_at),
        "created_at": _iso(row.created_at),
    }


def _evidence(row: MemoryFactEvidenceRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "fact_id": str(row.fact_id),
        "revision_id": str(row.revision_id),
        "source_candidate_id": (None if row.source_candidate_id is None else str(row.source_candidate_id)),
        "source_item_id": (None if row.source_item_id is None else str(row.source_item_id)),
        "thread_id": row.thread_id,
        "run_id": row.run_id,
        "run_event_sequence": row.run_event_sequence,
        "evidence_excerpt": row.evidence_excerpt,
        "trust_class": row.trust_class,
        "source_erased": row.source_erased_at is not None,
        "source_erased_at": _iso(row.source_erased_at),
        "created_at": _iso(row.created_at),
    }


def _summary(row: MemoryContextSummaryRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "summary_revision": row.summary_revision,
        "fact_revision_ceiling": row.fact_revision_ceiling,
        "source_revision_ids": list(row.source_revision_ids),
        "summary_text": row.summary_text,
        "content_erased_at": _iso(row.content_erased_at),
        "created_at": _iso(row.created_at),
    }


def _suppression(row: MemorySuppressionRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "suppression_kind": row.suppression_kind,
        "reason": row.reason,
        "created_at": _iso(row.created_at),
    }


def _snapshot(row: RunMemoryContextSnapshotRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "thread_id": row.thread_id,
        "run_id": row.run_id,
        "pipeline_mode": row.pipeline_mode,
        "fact_revision_ceiling": row.fact_revision_ceiling,
        "summary_id": None if row.summary_id is None else str(row.summary_id),
        "summary_revision": row.summary_revision,
        "selection_version": row.selection_version,
        "renderer_version": row.renderer_version,
        "prompt_version": row.prompt_version,
        "token_budget": row.token_budget,
        "rendered_content": row.rendered_content,
        "content_erased_at": _iso(row.content_erased_at),
        "created_at": _iso(row.created_at),
    }


def _snapshot_item(row: RunMemoryContextItemRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "namespace": row.namespace,
        "snapshot_id": str(row.snapshot_id),
        "ordinal": row.ordinal,
        "fact_id": str(row.fact_id),
        "revision_id": str(row.revision_id),
        "rank_score": row.rank_score,
        "selection_reason": row.selection_reason,
        "created_at": _iso(row.created_at),
    }


_EXPORT_TYPES: tuple[tuple[str, object, Callable[[object], dict[str, object]]], ...] = (
    ("memory_v2_source_batch", MemorySourceBatchRow, _source_batch),
    ("memory_v2_source_item", MemorySourceItemRow, _source_item),
    ("memory_v2_extraction_generation", MemoryExtractionGenerationRow, _extraction),
    (
        "memory_v2_consolidation_generation",
        MemoryConsolidationGenerationRow,
        _consolidation,
    ),
    ("memory_v2_candidate", MemoryCandidateRow, _candidate),
    ("memory_v2_fact", MemoryFactRow, _fact),
    ("memory_v2_fact_revision", MemoryFactRevisionRow, _revision),
    ("memory_v2_fact_evidence", MemoryFactEvidenceRow, _evidence),
    ("memory_v2_context_summary", MemoryContextSummaryRow, _summary),
    ("memory_v2_suppression", MemorySuppressionRow, _suppression),
    ("memory_v2_run_context_snapshot", RunMemoryContextSnapshotRow, _snapshot),
    ("memory_v2_run_context_item", RunMemoryContextItemRow, _snapshot_item),
)


async def iter_memory_v2_export_records(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    namespace: str | None,
) -> AsyncIterator[tuple[str, dict[str, object]]]:
    """Yield only the user-facing allowlist for one exact private scope."""

    for record_type, row_type, serializer in _EXPORT_TYPES:
        async for row in _rows(
            session,
            row_type,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
        ):
            yield record_type, serializer(row)


__all__ = ["iter_memory_v2_export_records"]
