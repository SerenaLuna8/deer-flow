"""The ``ingest_document`` task handler: queued Document to ready Segments.

Pipeline per claim: flip the matching-version document to ``processing``,
download the object into a per-task temporary directory, extract and split in
a worker thread, embed through the Base's bound embedding model, then publish
in one transaction (delete old segments, insert new ones, document ``ready``,
task ``succeeded``). A missing, deleting, or version-mismatched document makes
the claim a successful no-op — late results are never published.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..asyncio_utils import run_sync_to_completion
from ..contracts import (
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_LEXICAL_VERSION,
    KNOWLEDGE_PARSE_FAILED,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeModelPort,
    KnowledgeSettings,
)
from ..models import KnowledgeModelClient
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
)
from ..persistence.tasks import settle_task_row_success
from ..retrieval.lexical import lexical_index_input
from ..storage import MinioObjectStore
from ..tasks.worker import KnowledgeTaskClaim, ProjectActiveCheck
from .preview import extract_clean_split
from .progress import KnowledgeTaskProgressReporter, ensure_locked_task_lease, lock_indexing_claim
from .splitter import SegmentDraft
from .summary_admission import enqueue_summary_refresh

logger = logging.getLogger(__name__)


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any."""

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


@dataclass(frozen=True, slots=True)
class _PreparedIngest:
    storage_key: str
    original_name: str
    chunk_size: int
    chunk_overlap: int
    chunk_separator: str
    remove_extra_spaces: bool
    remove_urls_emails: bool
    chunking_mode: str
    child_chunk_size: int
    child_chunk_separator: str
    material: KnowledgeEmbeddingMaterial
    # True when the parameters above come from the task's frozen
    # reparse_settings: the publish transaction then swaps the document's
    # stored parameter columns together with the rows.
    from_reparse: bool = False


def _extract_and_split(
    source_path: Path,
    prepared: _PreparedIngest,
    *,
    max_total_chars: int,
) -> list[SegmentDraft]:
    """Blocking extraction, cleaning, and splitting; runs off the event loop.

    Parameters come from the document row, so retries and the chunk preview
    reproduce the first ingestion exactly.
    """

    drafts = extract_clean_split(
        source_path,
        prepared.original_name,
        chunk_size=prepared.chunk_size,
        chunk_overlap=prepared.chunk_overlap,
        chunk_separator=prepared.chunk_separator,
        remove_extra_spaces=prepared.remove_extra_spaces,
        remove_urls_emails=prepared.remove_urls_emails,
        max_total_chars=max_total_chars,
        chunking_mode=prepared.chunking_mode,  # type: ignore[arg-type]
        child_chunk_size=prepared.child_chunk_size,
        child_chunk_separator=prepared.child_chunk_separator,
    )
    if not drafts:
        raise KnowledgeError(KNOWLEDGE_PARSE_FAILED, "文件没有可提取的文本")
    if prepared.chunking_mode == "parent_child" and any(not draft.children for draft in drafts):
        # Structurally impossible for non-empty parents; a parent without
        # children would silently never be recalled, so fail loudly instead.
        raise KnowledgeError(KNOWLEDGE_PARSE_FAILED, "父子分块未能生成子块")
    return drafts


def _remove_temp_dir(path: str | Path) -> None:
    """Best-effort cleanup for a directory created during cancellation."""

    shutil.rmtree(path, ignore_errors=True)


class KnowledgeIngestionHandler:
    """Process one ``ingest_document`` claim end to end."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: KnowledgeSettings,
        object_store: MinioObjectStore,
        model_client: KnowledgeModelClient,
        model_port: KnowledgeModelPort,
        project_active_check: ProjectActiveCheck | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._object_store = object_store
        self._model_client = model_client
        self._model_port = model_port
        self._project_active_check = project_active_check

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        prepared = await self._begin_processing(claim)
        if prepared is None:
            return
        # Stage and batch progress live in short claim-guarded transactions;
        # a no-op claim above never reports a stage.
        progress = KnowledgeTaskProgressReporter(self._session_factory, claim, project_active_check=self._project_active_check)
        await progress.advance_stage("reading_source")
        temp_dir = Path(
            await run_sync_to_completion(
                tempfile.mkdtemp,
                prefix="actweave-knowledge-ingest-",
                cleanup_on_cancel=_remove_temp_dir,
            )
        )
        try:
            source_path = temp_dir / f"source{Path(prepared.original_name).suffix.lower()}"
            await self._object_store.download_to(prepared.storage_key, source_path)
            await progress.advance_stage("extracting_splitting")
            drafts = await run_sync_to_completion(
                _extract_and_split,
                source_path,
                prepared,
                # Text beyond segment-quota × chunk-size can never publish, so
                # extraction aborts there instead of materializing a bomb.
                max_total_chars=self._settings.max_segments_per_document * prepared.chunk_size,
            )
            if len(drafts) > self._settings.max_segments_per_document:
                raise KnowledgeError(
                    KNOWLEDGE_QUOTA_EXCEEDED,
                    f"切分产生 {len(drafts)} 个分段，超过上限 {self._settings.max_segments_per_document}",
                )
            if prepared.chunking_mode == "parent_child":
                # Only children carry vectors; parents publish with NULL
                # embedding and retrieval rolls child hits up to them.
                child_count = sum(len(draft.children) for draft in drafts)
                if child_count > self._settings.max_segments_per_document:
                    raise KnowledgeError(
                        KNOWLEDGE_QUOTA_EXCEEDED,
                        f"父子分块产生 {child_count} 个子块向量条目，超过上限 {self._settings.max_segments_per_document}",
                    )
                child_contents = [content for draft in drafts for content in draft.children]
                await progress.begin_embedding(len(child_contents))
                child_vectors = await self._model_client.embed(
                    prepared.material,
                    child_contents,
                    batch_guard=progress.ensure_claim_alive,
                    on_batch_verified=progress.add_verified_units,
                )
                if len(child_vectors) != len(child_contents):  # the client validates per batch; belt and braces
                    raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 返回数量与子块数量不一致")
                await progress.advance_stage("publishing")
                await self._publish(claim, prepared, drafts, parent_vectors=None, child_vectors=child_vectors)
            else:
                await progress.begin_embedding(len(drafts))
                vectors = await self._model_client.embed(
                    prepared.material,
                    [draft.content for draft in drafts],
                    batch_guard=progress.ensure_claim_alive,
                    on_batch_verified=progress.add_verified_units,
                )
                if len(vectors) != len(drafts):  # the client validates per batch; belt and braces
                    raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 返回数量与分段数量不一致")
                await progress.advance_stage("publishing")
                await self._publish(claim, prepared, drafts, parent_vectors=vectors, child_vectors=None)
        finally:
            # Blocking adapters join started work before propagating
            # cancellation, so cleanup cannot race a thread that is still
            # using this directory.
            await run_sync_to_completion(shutil.rmtree, temp_dir, ignore_errors=True)

    async def _begin_processing(self, claim: KnowledgeTaskClaim) -> _PreparedIngest | None:
        """Flip the matching document to ``processing`` and snapshot its inputs.

        Returns ``None`` when the document is gone, deleting, or on another
        version — the claim then settles as a successful no-op.
        """

        try:
            async with self._session_factory() as session, session.begin():
                await lock_indexing_claim(session, claim, project_active_check=self._project_active_check)
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == claim.resource_id).with_for_update())
                if document is None or document.version != claim.target_version or document.status not in ("queued", "processing"):
                    return None
                embedding_model_id = await session.scalar(select(KnowledgeBaseRow.embedding_model_id).where(KnowledgeBaseRow.id == document.knowledge_base_id))
                if embedding_model_id is None:  # pragma: no cover - upload admission requires a configured base
                    raise _storage_unavailable()
                # The port validates type and active status inside this locked
                # transaction; a disabled model halts provider usage, not just
                # new bases — re-enable plus user retry resumes ingestion.
                material = await self._model_port.embedding_material(session, embedding_model_id)
                document.status = "processing"
                document.updated_at = func.now()  # type: ignore[assignment]
                # An explicit re-parse carries its confirmed parameters on the
                # task; the stored columns keep describing the published rows
                # until this attempt actually publishes.
                frozen = claim.reparse_settings
                if frozen is not None:
                    return _PreparedIngest(
                        storage_key=document.storage_key,
                        original_name=document.original_name,
                        chunk_size=frozen["chunk_size"],
                        chunk_overlap=frozen["chunk_overlap"],
                        chunk_separator=frozen["chunk_separator"],
                        remove_extra_spaces=frozen["remove_extra_spaces"],
                        remove_urls_emails=frozen["remove_urls_emails"],
                        chunking_mode=frozen["chunking_mode"],
                        child_chunk_size=frozen["child_chunk_size"],
                        child_chunk_separator=frozen["child_chunk_separator"],
                        material=material,
                        from_reparse=True,
                    )
                return _PreparedIngest(
                    storage_key=document.storage_key,
                    original_name=document.original_name,
                    chunk_size=document.chunk_size,
                    chunk_overlap=document.chunk_overlap,
                    chunk_separator=document.chunk_separator,
                    remove_extra_spaces=document.remove_extra_spaces,
                    remove_urls_emails=document.remove_urls_emails,
                    chunking_mode=document.chunking_mode,
                    child_chunk_size=document.child_chunk_size,
                    child_chunk_separator=document.child_chunk_separator,
                    material=material,
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _publish(
        self,
        claim: KnowledgeTaskClaim,
        prepared: _PreparedIngest,
        drafts: list[SegmentDraft],
        *,
        parent_vectors: list[list[float]] | None,
        child_vectors: list[list[float]] | None,
    ) -> None:
        """Replace segments (and children), mark the document ready, settle the task.

        Exactly one of ``parent_vectors`` (general mode) and ``child_vectors``
        (parent_child mode, flattened in draft order) is provided. Deleting the
        old segments cascades to their child rows in the database. A re-parse
        publish also swaps the document's stored parameter columns, in the
        same transaction as the rows they describe.
        """

        try:
            async with self._session_factory() as session, session.begin():
                task = await lock_indexing_claim(session, claim, project_active_check=self._project_active_check)
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == claim.resource_id).with_for_update())
                await ensure_locked_task_lease(session, task)
                moment = datetime.now(UTC)
                if document is None or document.status != "processing" or document.version != claim.target_version:
                    # Late result: never publish, settle the claim as a no-op.
                    settle_task_row_success(task, now=moment)
                    return
                await session.execute(delete(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document.id))
                segment_ids = [uuid4() for _ in drafts]
                session.add_all(
                    [
                        KnowledgeSegmentRow(
                            id=segment_id,
                            project_id=document.project_id,
                            knowledge_base_id=document.knowledge_base_id,
                            knowledge_document_id=document.id,
                            document_version=document.version,
                            position=draft.position,
                            content=draft.content,
                            # 字数按字符计，与分段编辑/新增保持同一口径。
                            word_count=len(draft.content),
                            source_position=draft.source_position,
                            embedding=(parent_vectors[index] if parent_vectors is not None else None),
                            # lexical_v1 is derived from the text inside the
                            # publish transaction: the lexical route never
                            # sees a published row without current tokens.
                            lexical_tsv=func.to_tsvector("simple", lexical_index_input(draft.content)),
                            lexical_version=KNOWLEDGE_LEXICAL_VERSION,
                        )
                        for index, (segment_id, draft) in enumerate(zip(segment_ids, drafts, strict=True))
                    ]
                )
                if child_vectors is not None:
                    # Parents must hit the database before their children: the
                    # unit of work does not order inserts across mappers that
                    # are linked only by a ForeignKeyConstraint.
                    await session.flush()
                    child_rows = []
                    cursor = 0
                    for segment_id, draft in zip(segment_ids, drafts, strict=True):
                        for child_position, content in enumerate(draft.children, start=1):
                            child_rows.append(
                                KnowledgeSegmentChildRow(
                                    id=uuid4(),
                                    project_id=document.project_id,
                                    knowledge_base_id=document.knowledge_base_id,
                                    knowledge_document_id=document.id,
                                    knowledge_segment_id=segment_id,
                                    document_version=document.version,
                                    position=child_position,
                                    content=content,
                                    word_count=len(content),
                                    embedding=child_vectors[cursor],
                                    lexical_tsv=func.to_tsvector("simple", lexical_index_input(content)),
                                    lexical_version=KNOWLEDGE_LEXICAL_VERSION,
                                )
                            )
                            cursor += 1
                    if cursor != len(child_vectors):  # pragma: no cover - embed step sized the list
                        raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "子块向量数量与子块数量不一致")
                    session.add_all(child_rows)
                if prepared.from_reparse:
                    document.chunk_size = prepared.chunk_size
                    document.chunk_overlap = prepared.chunk_overlap
                    document.chunk_separator = prepared.chunk_separator
                    document.remove_extra_spaces = prepared.remove_extra_spaces
                    document.remove_urls_emails = prepared.remove_urls_emails
                    document.chunking_mode = prepared.chunking_mode
                    document.child_chunk_size = prepared.child_chunk_size
                    document.child_chunk_separator = prepared.child_chunk_separator
                document.status = "ready"
                document.segment_count = len(drafts)
                document.word_count = sum(len(draft.content) for draft in drafts)
                # The successful publish records its execution generation;
                # ``content_initialized`` in DTOs derives from this value.
                document.published_version = document.version
                document.error_message = None
                document.updated_at = moment
                settle_task_row_success(task, now=moment)
                await session.flush()
                await enqueue_summary_refresh(session, document, self._model_port)
        except SQLAlchemyError:
            raise _storage_unavailable() from None
