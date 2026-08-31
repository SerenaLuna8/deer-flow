"""The ``reembed_document`` task handler: new vectors for existing rows.

Re-embedding reads the rows the project actually has — including manual edits,
manually added segments, and disabled segments — and publishes new vectors
under the claim's target version. It never downloads the original file, never
re-parses, and never deletes or recreates content rows: UUIDs, text, order,
enabled state, source positions, and hit counts all survive. General mode
embeds the parent segments; parent_child mode embeds only the child chunks
(parents keep their NULL vector and roll up at recall time). An initialized
document with zero rows publishes successfully with zero vectors.

A missing, deleting, version-mismatched, or rebound-model document makes the
claim a successful no-op — late results are never published.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import (
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KNOWLEDGE_TASK_FAILED,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeModelPort,
)
from ..models import KnowledgeModelClient
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from ..persistence.tasks import settle_task_row_success
from ..tasks.worker import KnowledgeTaskClaim
from .progress import KnowledgeTaskProgressReporter

logger = logging.getLogger(__name__)


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any."""

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


@dataclass(frozen=True, slots=True)
class _PreparedReembed:
    """Frozen pre-embedding snapshot of the content rows and the binding."""

    embedding_model_id: UUID
    material: KnowledgeEmbeddingMaterial
    published_version: int
    # (row id, content) pairs of what gets embedded: segments in general
    # mode, children in parent_child mode.
    entries: tuple[tuple[UUID, str], ...]
    parent_child: bool


class KnowledgeReembedHandler:
    """Process one ``reembed_document`` claim end to end."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        model_client: KnowledgeModelClient,
        model_port: KnowledgeModelPort,
    ) -> None:
        self._session_factory = session_factory
        self._model_client = model_client
        self._model_port = model_port

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        # Loading spans the snapshot transaction below; reporting it first
        # also verifies the claim before any row is read.
        progress = KnowledgeTaskProgressReporter(self._session_factory, claim)
        await progress.advance_stage("loading_segments")
        prepared = await self._begin_processing(claim)
        if prepared is None:
            return
        vectors: list[list[float]] = []
        await progress.begin_embedding(len(prepared.entries))
        if prepared.entries:
            contents = [content for _row_id, content in prepared.entries]
            vectors = await self._model_client.embed(
                prepared.material,
                contents,
                batch_guard=progress.ensure_claim_alive,
                on_batch_verified=progress.add_verified_units,
            )
            if len(vectors) != len(contents):  # the client validates per batch; belt and braces
                raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 返回数量与内容行数量不一致")
        await progress.advance_stage("publishing")
        await self._publish(claim, prepared, vectors)

    async def _begin_processing(self, claim: KnowledgeTaskClaim) -> _PreparedReembed | None:
        """Flip the matching document to ``processing`` and snapshot its rows.

        Returns ``None`` when the document is gone, deleting, or on another
        version — the claim then settles as a successful no-op.
        """

        try:
            async with self._session_factory() as session, session.begin():
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == claim.resource_id).with_for_update())
                if document is None or document.version != claim.target_version or document.status not in ("queued", "processing"):
                    return None
                if document.published_version is None:
                    # Admission never queues a never-published document; a row
                    # like this is corrupt state, not a late result.
                    raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "文档从未成功发布，无法重嵌入")
                embedding_model_id = await session.scalar(select(KnowledgeBaseRow.embedding_model_id).where(KnowledgeBaseRow.id == document.knowledge_base_id))
                if embedding_model_id is None:  # pragma: no cover - FK keeps the base alive
                    raise _storage_unavailable()
                # The port validates type and active status inside this locked
                # transaction; a disabled model halts provider usage.
                material = await self._model_port.embedding_material(session, embedding_model_id)
                parent_child = document.chunking_mode == "parent_child"
                if parent_child:
                    rows = (
                        await session.execute(
                            select(KnowledgeSegmentChildRow.id, KnowledgeSegmentChildRow.content)
                            .where(
                                KnowledgeSegmentChildRow.knowledge_document_id == document.id,
                                KnowledgeSegmentChildRow.document_version == document.published_version,
                            )
                            .order_by(KnowledgeSegmentChildRow.knowledge_segment_id, KnowledgeSegmentChildRow.position)
                        )
                    ).all()
                else:
                    rows = (
                        await session.execute(
                            select(KnowledgeSegmentRow.id, KnowledgeSegmentRow.content)
                            .where(
                                KnowledgeSegmentRow.knowledge_document_id == document.id,
                                KnowledgeSegmentRow.document_version == document.published_version,
                            )
                            .order_by(KnowledgeSegmentRow.position)
                        )
                    ).all()
                document.status = "processing"
                document.updated_at = datetime.now(UTC)
                return _PreparedReembed(
                    embedding_model_id=embedding_model_id,
                    material=material,
                    published_version=document.published_version,
                    entries=tuple((row_id, content) for row_id, content in rows),
                    parent_child=parent_child,
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _publish(
        self,
        claim: KnowledgeTaskClaim,
        prepared: _PreparedReembed,
        vectors: list[list[float]],
    ) -> None:
        """Swap vectors and generations in place, mark ready, settle the task.

        One transaction re-validates the claim token, the target version, and
        the model binding; any mismatch is a late result and settles as a
        no-op without touching rows.
        """

        try:
            async with self._session_factory() as session, session.begin():
                task = await session.scalar(
                    select(KnowledgeTaskRow)
                    .where(
                        KnowledgeTaskRow.id == claim.id,
                        KnowledgeTaskRow.claim_token == claim.claim_token,
                        KnowledgeTaskRow.status == "running",
                    )
                    .with_for_update()
                )
                if task is None:
                    # The lease was re-claimed; the new owner publishes instead.
                    return
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == claim.resource_id).with_for_update())
                moment = datetime.now(UTC)
                if document is None or document.status != "processing" or document.version != claim.target_version:
                    # Late result: never publish, settle the claim as a no-op.
                    settle_task_row_success(task, now=moment)
                    return
                current_model_id = await session.scalar(select(KnowledgeBaseRow.embedding_model_id).where(KnowledgeBaseRow.id == document.knowledge_base_id))
                if current_model_id != prepared.embedding_model_id:
                    # The base was rebound after this attempt started; these
                    # vectors belong to the wrong space and must never land.
                    settle_task_row_success(task, now=moment)
                    return

                if prepared.entries:
                    embedded_table = KnowledgeSegmentChildRow if prepared.parent_child else KnowledgeSegmentRow
                    await session.execute(
                        update(embedded_table),
                        [{"id": row_id, "embedding": vector} for (row_id, _content), vector in zip(prepared.entries, vectors, strict=True)],
                    )
                # Flip the generation of every published row — parents and
                # children in both modes — so recall's version filter accepts
                # exactly the rows this publish certifies.
                segment_flip = await session.execute(
                    update(KnowledgeSegmentRow)
                    .where(
                        KnowledgeSegmentRow.knowledge_document_id == document.id,
                        KnowledgeSegmentRow.document_version == prepared.published_version,
                    )
                    .values(document_version=document.version)
                )
                child_flip = await session.execute(
                    update(KnowledgeSegmentChildRow)
                    .where(
                        KnowledgeSegmentChildRow.knowledge_document_id == document.id,
                        KnowledgeSegmentChildRow.document_version == prepared.published_version,
                    )
                    .values(document_version=document.version)
                )
                embedded_count = len(prepared.entries)
                flipped = child_flip.rowcount if prepared.parent_child else segment_flip.rowcount
                if flipped != embedded_count:  # pragma: no cover - edits require ready
                    raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "重嵌入期间内容行发生了变化")
                document.status = "ready"
                document.published_version = document.version
                document.error_message = None
                document.updated_at = moment
                settle_task_row_success(task, now=moment)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None
