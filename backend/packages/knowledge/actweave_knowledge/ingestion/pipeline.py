"""The ``ingest_document`` handler: frozen source to atomic publication.

The Worker reuses or creates one complete Knowledge Extraction under the
exact Task claim, derives Segment drafts through the same P3 splitter used by
preview, embeds only ``index_text``, and publishes every row, attachment
binding, Document pointer, and Task settlement in one transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
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
    KNOWLEDGE_TASK_FAILED,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeModelPort,
    KnowledgeSettings,
)
from ..extraction.contracts import (
    ExtractionError,
    ExtractionLimits,
    ExtractSetting,
    LocalAttachment,
    ParseWarning,
    ProcessingProfile,
)
from ..extraction.registry import ExtractorRegistry, default_registry
from ..extraction.runtime import run_extraction
from ..models import KnowledgeModelClient
from ..persistence.models import (
    KnowledgeAttachmentRow,
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeExtractionRow,
    KnowledgeSegmentAttachmentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
)
from ..persistence.tasks import settle_task_row_success, validated_reparse_settings
from ..retrieval.lexical import lexical_index_input
from ..storage import MinioObjectStore
from ..storage.extractions import ExtractionStore, StoredExtraction
from ..storage.quota import KnowledgeStorageQuotaPort
from ..tasks.worker import KnowledgeTaskClaim, ProjectActiveCheck
from .profiles import chunk_settings, validate_frozen_processing_profile
from .progress import KnowledgeTaskProgressReporter, ensure_locked_task_lease, lock_indexing_claim
from .splitter import SegmentDraft, split_documents
from .summary_admission import enqueue_summary_refresh

logger = logging.getLogger(__name__)


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any."""

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


async def _ensure_original_claim_deadline(
    session: AsyncSession,
    deadline: datetime,
) -> None:
    """Final DB-clock fence after every wait-capable publication operation."""

    now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime) or now.tzinfo is None or deadline <= now:
        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")


@dataclass(frozen=True, slots=True)
class _PreparedIngest:
    storage_key: str
    original_name: str
    source_sha256: str
    profile: ProcessingProfile
    capability_revision: str
    material: KnowledgeEmbeddingMaterial


def _remove_temp_dir(path: str | Path) -> None:
    """Best-effort cleanup after the isolated parser and callbacks settle."""

    shutil.rmtree(path, ignore_errors=True)


def _source_sha256(path: Path, *, max_bytes: int) -> str:
    """Hash one bounded downloaded original before any parser work."""

    digest = hashlib.sha256()
    consumed = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            consumed += len(block)
            if consumed > max_bytes:
                raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "原文件大小超过限制")
            digest.update(block)
    return digest.hexdigest()


def _warnings(stored: StoredExtraction) -> list[dict[str, object]]:
    """Project Extraction and per-Document warnings with preview ordering."""

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    candidates: tuple[ParseWarning, ...] = (
        *stored.result.warnings,
        *(warning for document in stored.result.documents for warning in document.warnings),
    )
    for warning in candidates:
        identity = warning.model_dump_json()
        if identity not in seen:
            seen.add(identity)
            result.append(warning.model_dump(mode="json"))
    return result


async def _settle_abort(operation: Awaitable[None]) -> None:
    """Drain an already-started abort despite repeated caller cancellation."""

    task = asyncio.create_task(operation)
    interrupted: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            interrupted = interrupted or exc
    if interrupted is not None:
        raise interrupted


class KnowledgeIngestionHandler:
    """Process one ``ingest_document`` claim end to end."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: KnowledgeSettings,
        object_store: MinioObjectStore,
        quota: KnowledgeStorageQuotaPort,
        model_client: KnowledgeModelClient,
        model_port: KnowledgeModelPort,
        project_active_check: ProjectActiveCheck,
        registry: ExtractorRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._object_store = object_store
        self._model_client = model_client
        self._model_port = model_port
        self._project_active_check = project_active_check
        self._registry = registry or default_registry()
        self._extractions = ExtractionStore(
            session_factory=session_factory,
            object_store=object_store,
            quota=quota,
            project_active_check=project_active_check,
            cache_enabled=settings.extraction_cache_enabled,
        )

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        prepared = await self._begin_processing(claim)
        if prepared is None:
            return
        progress = KnowledgeTaskProgressReporter(
            self._session_factory,
            claim,
            project_active_check=self._project_active_check,
        )
        limits = ExtractionLimits(max_source_bytes=self._settings.upload_max_bytes)
        await progress.advance_stage("reading_source")
        await progress.ensure_claim_alive()
        stored = await self._extractions.find_ready(
            claim,
            source_sha256=prepared.source_sha256,
            profile=prepared.profile.parse,
            limits=limits,
        )
        await progress.ensure_claim_alive()
        await progress.advance_stage("extracting_splitting")
        if stored is None:
            stored = await self._extract_to_store(
                claim,
                prepared,
                limits=limits,
                guard=progress.ensure_claim_alive,
            )
        await progress.ensure_claim_alive()
        drafts = await run_sync_to_completion(
            split_documents,
            stored.result.documents,
            profile=prepared.profile.chunk,
        )
        if not drafts:
            raise ExtractionError("NO_INDEXABLE_TEXT", "文件没有可提取的文本")
        if len(drafts) > self._settings.max_segments_per_document:
            raise KnowledgeError(
                KNOWLEDGE_QUOTA_EXCEEDED,
                f"切分产生 {len(drafts)} 个分段，超过上限 {self._settings.max_segments_per_document}",
            )
        if prepared.profile.chunk.mode == "parent_child":
            child_count = sum(len(draft.children) for draft in drafts)
            if child_count > self._settings.max_segments_per_document:
                raise KnowledgeError(
                    KNOWLEDGE_QUOTA_EXCEEDED,
                    f"父子分块产生 {child_count} 个子块向量条目，超过上限 {self._settings.max_segments_per_document}",
                )
            if any(not draft.children for draft in drafts):
                raise KnowledgeError(KNOWLEDGE_PARSE_FAILED, "父子分块未能生成子块")
            inputs = [child.index_text for draft in drafts for child in draft.children]
            await progress.begin_embedding(len(inputs))
            vectors = await self._model_client.embed(
                prepared.material,
                inputs,
                batch_guard=progress.ensure_claim_alive,
                on_batch_verified=progress.add_verified_units,
            )
            if len(vectors) != len(inputs):
                raise KnowledgeError(
                    KNOWLEDGE_EMBEDDING_FAILED,
                    "Embedding 返回数量与子块数量不一致",
                )
            await progress.advance_stage("publishing")
            await self._publish(
                claim,
                prepared,
                stored,
                drafts,
                parent_vectors=None,
                child_vectors=vectors,
            )
            return

        inputs = [draft.index_text for draft in drafts]
        await progress.begin_embedding(len(inputs))
        vectors = await self._model_client.embed(
            prepared.material,
            inputs,
            batch_guard=progress.ensure_claim_alive,
            on_batch_verified=progress.add_verified_units,
        )
        if len(vectors) != len(inputs):
            raise KnowledgeError(
                KNOWLEDGE_EMBEDDING_FAILED,
                "Embedding 返回数量与分段数量不一致",
            )
        await progress.advance_stage("publishing")
        await self._publish(
            claim,
            prepared,
            stored,
            drafts,
            parent_vectors=vectors,
            child_vectors=None,
        )

    async def _extract_to_store(
        self,
        claim: KnowledgeTaskClaim,
        prepared: _PreparedIngest,
        *,
        limits: ExtractionLimits,
        guard: Callable[[], Awaitable[None]],
    ) -> StoredExtraction:
        """Create one complete Extraction without overlapping temp budgets."""

        reservation = await self._extractions.begin(
            claim,
            source_sha256=prepared.source_sha256,
            profile=prepared.profile.parse,
        )
        work_dir: Path | None = None
        try:
            work_dir = Path(
                await run_sync_to_completion(
                    tempfile.mkdtemp,
                    prefix="actweave-knowledge-ingest-",
                    cleanup_on_cancel=_remove_temp_dir,
                )
            )
            source_path = work_dir / f"source{Path(prepared.original_name).suffix.lower()}"
            await guard()
            await self._object_store.download_to(
                prepared.storage_key,
                source_path,
                max_bytes=limits.max_source_bytes,
            )
            await guard()
            actual_source_sha256 = await run_sync_to_completion(
                _source_sha256,
                source_path,
                max_bytes=limits.max_source_bytes,
            )
            if actual_source_sha256 != prepared.source_sha256:
                raise KnowledgeError(
                    KNOWLEDGE_STORAGE_UNAVAILABLE,
                    "原文件完整性校验失败",
                )
            await guard()

            async def on_asset(asset: LocalAttachment) -> None:
                assert work_dir is not None
                await self._extractions.persist_attachment(
                    reservation,
                    asset,
                    work_dir=work_dir,
                )

            result = await run_extraction(
                ExtractSetting(
                    source_path=source_path,
                    original_name=prepared.original_name,
                    datasource_type="file",
                    profile=prepared.profile.parse,
                ),
                work_dir=work_dir,
                limits=limits,
                timeout_seconds=self._settings.task_timeout_seconds,
                on_asset=on_asset,
                guard=guard,
            )
            await guard()
        except BaseException:
            await _settle_abort(self._extractions.abort(claim, reservation))
            raise
        finally:
            if work_dir is not None:
                await run_sync_to_completion(shutil.rmtree, work_dir, ignore_errors=True)

        # ``run_extraction`` has joined every callback and the host parser
        # directory is gone before the Store creates its private manifest
        # directory.  The two independent disk budgets never overlap.
        try:
            return await self._extractions.complete(reservation, result)
        except BaseException:
            await _settle_abort(self._extractions.abort(claim, reservation))
            raise

    async def _begin_processing(
        self,
        claim: KnowledgeTaskClaim,
    ) -> _PreparedIngest | None:
        """Lock the exact claim and snapshot admission-frozen inputs."""

        try:
            async with self._session_factory() as session, session.begin():
                await lock_indexing_claim(
                    session,
                    claim,
                    project_active_check=self._project_active_check,
                )
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == claim.resource_id).with_for_update())
                if document is None or document.version != claim.target_version or document.status not in ("queued", "processing"):
                    return None
                embedding_model_id = await session.scalar(select(KnowledgeBaseRow.embedding_model_id).where(KnowledgeBaseRow.id == document.knowledge_base_id))
                if embedding_model_id is None:
                    raise _storage_unavailable()
                material = await self._model_port.embedding_material(
                    session,
                    embedding_model_id,
                )
                if claim.reparse_settings is not None:
                    frozen = validated_reparse_settings(claim.reparse_settings)
                    profile_value = frozen["processing_profile"]
                    capability_revision = frozen["capability_revision"]
                else:
                    profile_value = document.parsing_profile
                    capability_revision = document.capability_revision
                snapshot = (
                    document.storage_key,
                    document.original_name,
                    document.source_sha256,
                    profile_value,
                    capability_revision,
                    {
                        "chunk_size": document.chunk_size,
                        "chunk_overlap": document.chunk_overlap,
                        "chunk_separator": document.chunk_separator,
                        "chunking_mode": document.chunking_mode,
                        "child_chunk_size": document.child_chunk_size,
                        "child_chunk_separator": document.child_chunk_separator,
                        "remove_extra_spaces": document.remove_extra_spaces,
                        "remove_urls_emails": document.remove_urls_emails,
                    },
                )
                document.status = "processing"
                document.updated_at = func.now()  # type: ignore[assignment]
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

        storage_key, original_name, source_sha256, profile_value, capability_revision, projected = snapshot
        if not isinstance(source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None or not isinstance(capability_revision, str) or re.fullmatch(r"[0-9a-f]{64}", capability_revision) is None:
            raise ExtractionError(
                "PROCESSING_PROFILE_UNAVAILABLE",
                "原解析配置已不可用，请显式重新解析",
            )
        profile = await run_sync_to_completion(
            validate_frozen_processing_profile,
            profile_value,
            extension=Path(original_name).suffix,
            registry=self._registry,
        )
        if claim.reparse_settings is None and projected != chunk_settings(profile):
            raise ExtractionError(
                "PROCESSING_PROFILE_UNAVAILABLE",
                "原解析配置已不可用，请显式重新解析",
            )
        return _PreparedIngest(
            storage_key=storage_key,
            original_name=original_name,
            source_sha256=source_sha256,
            profile=profile,
            capability_revision=capability_revision,
            material=material,
        )

    async def _publish(
        self,
        claim: KnowledgeTaskClaim,
        prepared: _PreparedIngest,
        stored: StoredExtraction,
        drafts: list[SegmentDraft],
        *,
        parent_vectors: list[list[float]] | None,
        child_vectors: list[list[float]] | None,
    ) -> None:
        """Atomically replace content, bindings, publication pointer and Task."""

        try:
            async with self._session_factory() as session, session.begin():
                task = await lock_indexing_claim(
                    session,
                    claim,
                    project_active_check=self._project_active_check,
                )
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == claim.resource_id).with_for_update())
                await ensure_locked_task_lease(session, task)
                claim_deadline = task.lease_until
                if claim_deadline is None:
                    raise KnowledgeError(
                        KNOWLEDGE_TASK_FAILED,
                        "Knowledge 任务租约已失效",
                    )
                moment = datetime.now(UTC)
                if document is None or document.status != "processing" or document.version != claim.target_version:
                    settle_task_row_success(task, now=moment)
                    return
                extraction = await session.get(
                    KnowledgeExtractionRow,
                    stored.extraction_id,
                    with_for_update=True,
                )
                if (
                    stored.document_id != document.id
                    or task.extraction_id != stored.extraction_id
                    or extraction is None
                    or extraction.state != "ready"
                    or (
                        extraction.project_id,
                        extraction.knowledge_base_id,
                        extraction.knowledge_document_id,
                        extraction.source_sha256,
                    )
                    != (
                        document.project_id,
                        document.knowledge_base_id,
                        document.id,
                        prepared.source_sha256,
                    )
                ):
                    raise KnowledgeError(
                        KNOWLEDGE_STORAGE_UNAVAILABLE,
                        "提取结果或任务身份已变更",
                    )
                attachment_rows = list((await session.scalars(select(KnowledgeAttachmentRow).where(KnowledgeAttachmentRow.extraction_id == stored.extraction_id).with_for_update())).all())
                attachments = {row.sha256: row for row in attachment_rows}
                expected = {item.ref: item for item in stored.result.attachments}
                if (
                    len(attachments) != len(attachment_rows)
                    or len(expected) != len(stored.result.attachments)
                    or set(attachments) != set(expected)
                    or any(
                        row.state != "ready"
                        or row.upload_state != "stored"
                        or row.quota_state != "committed"
                        or (
                            row.project_id,
                            row.knowledge_base_id,
                            row.knowledge_document_id,
                            row.extraction_id,
                            row.media_type,
                            row.size_bytes,
                            row.width,
                            row.height,
                        )
                        != (
                            document.project_id,
                            document.knowledge_base_id,
                            document.id,
                            stored.extraction_id,
                            expected[row.sha256].media_type,
                            expected[row.sha256].size_bytes,
                            expected[row.sha256].width,
                            expected[row.sha256].height,
                        )
                        for row in attachment_rows
                    )
                ):
                    raise KnowledgeError(
                        KNOWLEDGE_STORAGE_UNAVAILABLE,
                        "提取结果附件关系不完整",
                    )

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
                            extraction_id=stored.extraction_id,
                            content=draft.content,
                            index_text=draft.index_text,
                            token_count=draft.token_count,
                            word_count=len(draft.content),
                            source_position=draft.source_position,
                            source_spans=[span.model_dump(mode="json") for span in draft.source_spans],
                            embedding=(parent_vectors[index] if parent_vectors is not None else None),
                            lexical_tsv=func.to_tsvector(
                                "simple",
                                lexical_index_input(draft.index_text),
                            ),
                            lexical_version=KNOWLEDGE_LEXICAL_VERSION,
                        )
                        for index, (segment_id, draft) in enumerate(zip(segment_ids, drafts, strict=True))
                    ]
                )
                await session.flush()

                bindings = []
                for segment_id, draft in zip(segment_ids, drafts, strict=True):
                    for position, occurrence in enumerate(
                        draft.attachments,
                        start=1,
                    ):
                        attachment = attachments.get(occurrence.ref)
                        if attachment is None:
                            raise KnowledgeError(
                                KNOWLEDGE_STORAGE_UNAVAILABLE,
                                "分段引用了未登记的附件",
                            )
                        bindings.append(
                            KnowledgeSegmentAttachmentRow(
                                project_id=document.project_id,
                                knowledge_base_id=document.knowledge_base_id,
                                knowledge_document_id=document.id,
                                extraction_id=stored.extraction_id,
                                segment_id=segment_id,
                                attachment_id=attachment.id,
                                position=position,
                                alt_text=occurrence.alt_text,
                            )
                        )
                session.add_all(bindings)

                if child_vectors is not None:
                    child_rows = []
                    cursor = 0
                    for segment_id, draft in zip(segment_ids, drafts, strict=True):
                        for child_position, child in enumerate(
                            draft.children,
                            start=1,
                        ):
                            child_rows.append(
                                KnowledgeSegmentChildRow(
                                    id=uuid4(),
                                    project_id=document.project_id,
                                    knowledge_base_id=document.knowledge_base_id,
                                    knowledge_document_id=document.id,
                                    knowledge_segment_id=segment_id,
                                    document_version=document.version,
                                    position=child_position,
                                    content=child.content,
                                    index_text=child.index_text,
                                    token_count=child.token_count,
                                    source_spans=[span.model_dump(mode="json") for span in child.source_spans],
                                    word_count=len(child.content),
                                    embedding=child_vectors[cursor],
                                    lexical_tsv=func.to_tsvector(
                                        "simple",
                                        lexical_index_input(child.index_text),
                                    ),
                                    lexical_version=KNOWLEDGE_LEXICAL_VERSION,
                                )
                            )
                            cursor += 1
                    if cursor != len(child_vectors):
                        raise KnowledgeError(
                            KNOWLEDGE_EMBEDDING_FAILED,
                            "子块向量数量与子块数量不一致",
                        )
                    session.add_all(child_rows)

                chunk = prepared.profile.chunk
                document.chunk_size = chunk.size
                document.chunk_overlap = chunk.overlap
                document.chunk_separator = chunk.separator
                document.chunking_mode = chunk.mode
                document.child_chunk_size = chunk.child_size
                document.child_chunk_separator = chunk.child_separator
                document.remove_extra_spaces = chunk.remove_extra_spaces
                document.remove_urls_emails = chunk.remove_urls_emails
                document.parsing_profile = prepared.profile.model_dump(mode="json")
                document.capability_revision = prepared.capability_revision
                document.parse_warnings = _warnings(stored)
                document.published_extraction_id = stored.extraction_id
                document.status = "ready"
                document.segment_count = len(drafts)
                document.word_count = sum(len(draft.content) for draft in drafts)
                document.published_version = document.version
                document.error_message = None
                document.updated_at = moment
                await session.flush()
                settle_task_row_success(task, now=moment)
                # Flush the current Task update by itself so the partial
                # unique open-indexing slot is released inside this
                # transaction before admitting its successor summary Task.
                # A later deadline failure rolls this settlement back.
                await session.flush([task])
                await enqueue_summary_refresh(session, document, self._model_port)
                await session.flush()
                # Nothing after this check may acquire another business-row
                # lock or flush.  The transaction already owns the exact Task
                # row, so comparing the original deadline to PostgreSQL time
                # is the final authority boundary before commit.
                await _ensure_original_claim_deadline(session, claim_deadline)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None
