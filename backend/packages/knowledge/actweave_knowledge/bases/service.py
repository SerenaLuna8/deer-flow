"""Knowledge Base CRUD scoped to one project.

Every query filters on the host-provided ``project_id``; a base belonging to
another project behaves exactly like a missing base (``KNOWLEDGE_NOT_FOUND``).
Base deletion is asynchronous: this service marks the base ``deleting`` and
creates a ``delete_knowledge_base`` task; the Worker performs the deletion.
"""

from __future__ import annotations

import logging
import sys
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_LEXICAL_VERSION,
    KNOWLEDGE_MAX_TOP_K,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseUpdateResult,
    KnowledgeBaseView,
    KnowledgeError,
    KnowledgeModelPort,
    KnowledgeRebuildResult,
    KnowledgeRelexResult,
    KnowledgeSettings,
    KnowledgeSummaryBackfill,
)
from ..persistence.derivations import delete_error_expression, document_count_expression, live_document_count_expression
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from ..persistence.tasks import TASK_OPEN_STATUSES, VERSIONED_TASK_KINDS

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100

_MAX_NAME_LENGTH = 120
_MAX_DESCRIPTION_LENGTH = 500

# Serializes per-project base creation so the quota check cannot be raced past
# by concurrent requests. The namespace only has to avoid colliding with other
# advisory-lock users of this database.
_BASE_QUOTA_LOCK_NAMESPACE = 0x4B_42_51  # "KBQ"


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any."""

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


def _validated_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        raise _invalid(f"name 必须是 1-{_MAX_NAME_LENGTH} 个字符的非空文本")
    return cleaned


def _validated_description(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) > _MAX_DESCRIPTION_LENGTH:
        raise _invalid(f"description 最多 {_MAX_DESCRIPTION_LENGTH} 个字符")
    return cleaned


def _validated_page(page: int, page_size: int) -> tuple[int, int]:
    if type(page) is not int or page < 1:
        raise _invalid("page 必须是不小于 1 的整数")
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise _invalid(f"page_size 必须是 1-{MAX_PAGE_SIZE} 之间的整数")
    return page, page_size


def _view(row: KnowledgeBaseRow, *, document_count: int, delete_error: str | None, live_document_count: int) -> KnowledgeBaseView:
    return KnowledgeBaseView(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        description=row.description,
        embedding_model_id=row.embedding_model_id,
        reranker_model_id=row.reranker_model_id,
        retrieval_mode=row.retrieval_mode,  # type: ignore[arg-type]
        summary_index_enabled=row.summary_index_enabled,
        status=row.status,  # type: ignore[arg-type]
        document_count=document_count,
        default_top_k=row.default_top_k,
        default_score_threshold=row.default_score_threshold,
        delete_error=delete_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        default_relative_cutoff=row.default_relative_cutoff,
        # A stored mode only binds while documents hold it; an emptied base is
        # undetermined again and its next upload chooses freely.
        chunking_mode=row.chunking_mode if live_document_count > 0 else None,  # type: ignore[arg-type]
    )


def _base_with_derivations(project_id: UUID):  # noqa: ANN202 - SQLAlchemy select
    return select(
        KnowledgeBaseRow,
        document_count_expression(KnowledgeBaseRow.id).label("document_count"),
        delete_error_expression("delete_knowledge_base", KnowledgeBaseRow.id).label("delete_error"),
        live_document_count_expression(KnowledgeBaseRow.id).label("live_document_count"),
    ).where(KnowledgeBaseRow.project_id == project_id)


async def _view_with_derivations(session: AsyncSession, row: KnowledgeBaseRow) -> KnowledgeBaseView:
    """Project one locked/refreshed base row with its derived counts."""

    document_count, delete_error, live_document_count = (
        await session.execute(
            select(
                document_count_expression(KnowledgeBaseRow.id),
                delete_error_expression("delete_knowledge_base", KnowledgeBaseRow.id),
                live_document_count_expression(KnowledgeBaseRow.id),
            ).where(KnowledgeBaseRow.id == row.id)
        )
    ).one()
    return _view(row, document_count=int(document_count), delete_error=delete_error, live_document_count=int(live_document_count))


class KnowledgeBaseService:
    """Create, list, read, and update Knowledge Bases of one project."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: KnowledgeSettings,
        model_port: KnowledgeModelPort,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._model_port = model_port

    async def create_knowledge_base(
        self,
        project_id: UUID,
        create: KnowledgeBaseCreate,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeBaseView:
        name = _validated_name(create.name)
        description = _validated_description(create.description)
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                await session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            _BASE_QUOTA_LOCK_NAMESPACE,
                            func.hashtext(str(project_id)),
                        )
                    )
                )
                base_count = await session.scalar(select(func.count()).select_from(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
                if int(base_count or 0) >= self._settings.max_knowledge_bases_per_project:
                    raise KnowledgeError(
                        KNOWLEDGE_QUOTA_EXCEEDED,
                        f"Project 内 Knowledge Base 数量已达上限 {self._settings.max_knowledge_bases_per_project}",
                    )
                if create.embedding_model_id is not None and not isinstance(create.embedding_model_id, UUID):
                    raise _invalid("embedding_model_id 必须是 UUID 或 null")
                if create.reranker_model_id is not None and not isinstance(create.reranker_model_id, UUID):
                    raise _invalid("reranker_model_id 必须是 UUID 或 null")
                if create.embedding_model_id is None and create.reranker_model_id is not None:
                    raise _invalid("请先配置 Embedding 模型，再绑定 Reranker 模型")
                # FOR SHARE (Provider then Model, inside the port) serializes
                # with the registry's FOR UPDATE disable/delete paths, so the
                # active check can never pass on a stale snapshot.
                if create.embedding_model_id is not None:
                    await self._model_port.lock_model_for_binding(session, create.embedding_model_id, "embedding")
                reranker_model_id = create.reranker_model_id
                if reranker_model_id is not None:
                    await self._model_port.lock_model_for_binding(session, reranker_model_id, "rerank")
                if create.retrieval_mode not in ("semantic", "hybrid"):
                    raise _invalid("retrieval_mode 只能是 semantic 或 hybrid")
                row = KnowledgeBaseRow(
                    id=uuid4(),
                    project_id=project_id,
                    name=name,
                    description=description,
                    embedding_model_id=create.embedding_model_id,
                    reranker_model_id=reranker_model_id,
                    status="active",
                    retrieval_mode=create.retrieval_mode,
                )
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return _view(row, document_count=0, delete_error=None, live_document_count=0)
        except IntegrityError as exc:
            if "uq_knowledge_bases_project_name" in str(exc):
                raise KnowledgeError(KNOWLEDGE_NAME_CONFLICT, "同一 Project 内已存在同名 Knowledge Base") from None
            # The RESTRICT foreign key caught a model deleted between the
            # binding lock and the insert.
            raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "检索模型不存在或已停用") from None
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def list_knowledge_bases(
        self,
        project_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[list[KnowledgeBaseView], int]:
        page, page_size = _validated_page(page, page_size)
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                total = await session.scalar(select(func.count()).select_from(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
                rows = await session.execute(_base_with_derivations(project_id).order_by(KnowledgeBaseRow.updated_at.desc(), KnowledgeBaseRow.id.desc()).offset((page - 1) * page_size).limit(page_size))
                views = [_view(row, document_count=int(document_count), delete_error=delete_error, live_document_count=int(live_document_count)) for row, document_count, delete_error, live_document_count in rows.all()]
                return views, int(total or 0)
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def get_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeBaseView:
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                result = (await session.execute(_base_with_derivations(project_id).where(KnowledgeBaseRow.id == base_id))).one_or_none()
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if result is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
        row, document_count, delete_error, live_document_count = result
        return _view(row, document_count=int(document_count), delete_error=delete_error, live_document_count=int(live_document_count))

    async def update_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        update: KnowledgeBaseUpdate,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeBaseUpdateResult:
        changes: dict[str, str | int | float | UUID | None] = {}
        if update.name is not None:
            changes["name"] = _validated_name(update.name)
        if update.summary_index_enabled is not None:
            if type(update.summary_index_enabled) is not bool:
                raise _invalid("summary_index_enabled 必须是布尔值")
            changes["summary_index_enabled"] = update.summary_index_enabled
        if update.description is not None:
            changes["description"] = _validated_description(update.description)
        if update.status is not None:
            if update.status not in ("active", "disabled"):
                raise _invalid("status 只能是 active 或 disabled")
            changes["status"] = update.status
        if update.default_top_k is not None:
            # type() checks reject bool, which is an int subclass.
            if type(update.default_top_k) is not int or not 1 <= update.default_top_k <= KNOWLEDGE_MAX_TOP_K:
                raise _invalid(f"default_top_k 必须是 1..{KNOWLEDGE_MAX_TOP_K} 的整数")
            changes["default_top_k"] = update.default_top_k
        if update.default_score_threshold is not None:
            threshold = update.default_score_threshold
            if type(threshold) not in (int, float) or not 0 <= float(threshold) <= 1:
                raise _invalid("default_score_threshold 必须在 0..1 之间")
            changes["default_score_threshold"] = float(threshold)
        if update.default_relative_cutoff is not None and update.clear_relative_cutoff:
            raise _invalid("default_relative_cutoff 与 clear_relative_cutoff 不能同时设置")
        if update.default_relative_cutoff is not None:
            cutoff = update.default_relative_cutoff
            if type(cutoff) not in (int, float) or not 0 < float(cutoff) <= 1:
                raise _invalid("default_relative_cutoff 必须在 (0, 1] 之间")
            changes["default_relative_cutoff"] = float(cutoff)
        elif update.clear_relative_cutoff:
            changes["default_relative_cutoff"] = None
        if update.retrieval_mode is not None:
            if update.retrieval_mode not in ("semantic", "hybrid"):
                raise _invalid("retrieval_mode 只能是 semantic 或 hybrid")
            changes["retrieval_mode"] = update.retrieval_mode
        if update.reranker_model_id is not None and update.clear_reranker_model:
            raise _invalid("reranker_model_id 与 clear_reranker_model 不能同时设置")
        if update.embedding_model_id is not None and not isinstance(update.embedding_model_id, UUID):
            raise _invalid("embedding_model_id 必须是 UUID")
        if update.reranker_model_id is not None and not isinstance(update.reranker_model_id, UUID):
            raise _invalid("reranker_model_id 必须是 UUID")
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                if row.status == "deleting":
                    raise _invalid("Knowledge Base 正在删除，不能修改")
                summary_backfill = None
                enabling_summary = update.summary_index_enabled is True and not row.summary_index_enabled
                if enabling_summary:
                    try:
                        summary_model = await self._model_port.resolve_summary_model(session)
                    except KnowledgeError as exc:
                        if exc.code != KNOWLEDGE_MODEL_UNAVAILABLE:
                            raise
                        summary_model = None
                    if summary_model is None:
                        raise _invalid("请先在系统设置 → 知识库配置中选择可用的摘要模型")
                effective_embedding_model_id = update.embedding_model_id or row.embedding_model_id
                if effective_embedding_model_id is None and update.reranker_model_id is not None:
                    raise _invalid("请先配置 Embedding 模型，再绑定 Reranker 模型")
                effective = {name: value for name, value in changes.items() if value != getattr(row, name)}
                if update.embedding_model_id is not None:
                    if row.embedding_model_id is not None:
                        raise _invalid("Embedding 模型已配置，请通过重嵌入更换模型")
                    document_count = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id))
                    if document_count:
                        raise _invalid("仅没有文档的 Knowledge Base 支持首次配置 Embedding 模型")
                    # The base lock also serializes upload admission and other
                    # first configurations; all settings commit together.
                    await self._model_port.lock_model_for_binding(session, update.embedding_model_id, "embedding")
                    effective["embedding_model_id"] = update.embedding_model_id
                # Rerank rebinding never rebuilds and never bumps document
                # versions: only which reranker (if any) scores recall changes.
                if update.reranker_model_id is not None and update.reranker_model_id != row.reranker_model_id:
                    await self._model_port.lock_model_for_binding(session, update.reranker_model_id, "rerank")
                    effective["reranker_model_id"] = update.reranker_model_id
                if update.clear_reranker_model and row.reranker_model_id is not None:
                    effective["reranker_model_id"] = None
                if effective:
                    for name, value in effective.items():
                        setattr(row, name, value)
                    row.updated_at = func.now()  # type: ignore[assignment]
                    await session.flush()
                    await session.refresh(row)
                if enabling_summary:
                    documents = (await session.scalars(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id).order_by(KnowledgeDocumentRow.id).with_for_update())).all()
                    open_ids = set(
                        await session.scalars(
                            select(KnowledgeTaskRow.resource_id).where(
                                KnowledgeTaskRow.resource_id.in_([document.id for document in documents]), KnowledgeTaskRow.kind.in_(VERSIONED_TASK_KINDS), KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES)
                            )
                        )
                    )
                    accepted = 0
                    skipped = []
                    for document in documents:
                        if document.status != "ready" or document.published_version != document.version or document.id in open_ids:
                            skipped.append(document.id)
                            continue
                        session.add(KnowledgeTaskRow(id=uuid4(), project_id=project_id, resource_id=document.id, kind="summarize_document", target_version=document.version))
                        accepted += 1
                    summary_backfill = KnowledgeSummaryBackfill(accepted_document_count=accepted, skipped_document_ids=tuple(skipped))
                return KnowledgeBaseUpdateResult(base=await _view_with_derivations(session, row), summary_backfill=summary_backfill)
        except IntegrityError:
            raise KnowledgeError(KNOWLEDGE_NAME_CONFLICT, "同一 Project 内已存在同名 Knowledge Base") from None
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def rebuild_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        embedding_model_id: UUID,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeRebuildResult:
        """Rebind the embedding model and re-embed the current content.

        One transaction locks the base, then every document in UUID order.
        Any in-flight document (uploading, queued, processing, deleting) or
        open indexing task rejects the whole operation — changing the vector
        space must not race an upload or another (re)build. Initialized
        ready/failed documents bump their version to ``queued`` with one
        ``reembed_document`` task each, keeping rows and counters; documents
        that never published are skipped and stay failed (re-parsing the
        original file is a separate, explicit decision). Re-running with the
        same model is allowed — it is a plain re-embed of the whole base.
        """

        if not isinstance(embedding_model_id, UUID):
            raise _invalid("embedding_model_id 必须是 UUID")
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                if row.status == "deleting":
                    raise _invalid("Knowledge Base 正在删除，不能重建")
                documents = (await session.scalars(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id).order_by(KnowledgeDocumentRow.id).with_for_update())).all()
                blocking = sorted({document.status for document in documents if document.status not in ("ready", "failed")})
                if blocking:
                    raise _invalid(f"存在 {'/'.join(blocking)} 状态的文档，请先等待或处理后再重嵌入")
                open_indexing = (
                    await session.scalar(
                        select(func.count())
                        .select_from(KnowledgeTaskRow)
                        .where(
                            KnowledgeTaskRow.resource_id.in_([document.id for document in documents]),
                            KnowledgeTaskRow.kind.in_(VERSIONED_TASK_KINDS),
                            KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
                        )
                    )
                    if documents
                    else 0
                )
                if open_indexing:
                    raise _invalid("存在未完成的索引任务，请先等待或处理后再重嵌入")
                await self._model_port.lock_model_for_binding(session, embedding_model_id, "embedding")
                row.embedding_model_id = embedding_model_id
                row.updated_at = func.now()  # type: ignore[assignment]
                accepted = 0
                skipped: list[UUID] = []
                for document in documents:
                    if document.published_version is None:
                        # Never published: there is no current content to
                        # re-embed, and re-parsing must stay explicit.
                        skipped.append(document.id)
                        continue
                    document.version = document.version + 1
                    document.status = "queued"
                    # Rows survive; the counters keep describing them.
                    document.error_message = None
                    document.updated_at = func.now()  # type: ignore[assignment]
                    session.add(
                        KnowledgeTaskRow(
                            id=uuid4(),
                            project_id=project_id,
                            resource_id=document.id,
                            kind="reembed_document",
                            target_version=document.version,
                            status="queued",
                        )
                    )
                    accepted += 1
                await session.flush()
                await session.refresh(row)
                return KnowledgeRebuildResult(
                    base=await _view_with_derivations(session, row),
                    accepted_document_count=accepted,
                    skipped_document_ids=tuple(skipped),
                )
        except IntegrityError:
            # The RESTRICT foreign key caught a model deleted between the
            # binding lock and the commit.
            raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "检索模型不存在或已停用") from None
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def relex_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeRelexResult:
        """Queue lexical re-derivation for every stale published document.

        Unlike rebuild, this touches neither models nor vectors and never
        takes a document out of ``ready``: the Worker's ``relex_document``
        handler rewrites ``lexical_tsv``/``lexical_version`` from the stored
        model text under the document's current version. Documents already on
        the current derivation are counted, not queued; documents that are not
        ready/published or already hold an open indexing task are skipped and
        reported so the caller can re-run later.
        """

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id).with_for_update(read=True))
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                if row.status == "deleting":
                    raise _invalid("Knowledge Base 正在删除，不能重建词法索引")
                documents = (await session.scalars(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id).order_by(KnowledgeDocumentRow.id).with_for_update())).all()
                document_ids = [document.id for document in documents]
                busy: set[UUID] = set()
                stale: set[UUID] = set()
                if document_ids:
                    busy = set(
                        (
                            await session.scalars(
                                select(KnowledgeTaskRow.resource_id).where(
                                    KnowledgeTaskRow.resource_id.in_(document_ids),
                                    KnowledgeTaskRow.kind.in_(VERSIONED_TASK_KINDS),
                                    KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
                                )
                            )
                        ).all()
                    )
                    # Only current-generation rows matter: older generations
                    # are maintenance evidence and never enter recall.
                    stale_segments = (
                        select(KnowledgeSegmentRow.knowledge_document_id)
                        .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
                        .where(
                            KnowledgeSegmentRow.knowledge_document_id.in_(document_ids),
                            KnowledgeSegmentRow.document_version == KnowledgeDocumentRow.version,
                            KnowledgeSegmentRow.lexical_version != KNOWLEDGE_LEXICAL_VERSION,
                        )
                    )
                    stale_children = (
                        select(KnowledgeSegmentChildRow.knowledge_document_id)
                        .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentChildRow.knowledge_document_id)
                        .where(
                            KnowledgeSegmentChildRow.knowledge_document_id.in_(document_ids),
                            KnowledgeSegmentChildRow.document_version == KnowledgeDocumentRow.version,
                            KnowledgeSegmentChildRow.lexical_version != KNOWLEDGE_LEXICAL_VERSION,
                        )
                    )
                    stale = set((await session.scalars(stale_segments.union(stale_children))).all())
                accepted = 0
                up_to_date = 0
                skipped: list[UUID] = []
                for document in documents:
                    if document.status != "ready" or document.published_version != document.version or document.id in busy:
                        skipped.append(document.id)
                        continue
                    if document.id not in stale:
                        up_to_date += 1
                        continue
                    session.add(
                        KnowledgeTaskRow(
                            id=uuid4(),
                            project_id=project_id,
                            resource_id=document.id,
                            kind="relex_document",
                            target_version=document.version,
                            status="queued",
                        )
                    )
                    accepted += 1
                await session.flush()
                return KnowledgeRelexResult(
                    accepted_document_count=accepted,
                    up_to_date_document_count=up_to_date,
                    skipped_document_ids=tuple(skipped),
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def delete_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeBaseView:
        """Mark the base ``deleting`` and ensure one open delete task exists.

        The Worker deletes every document object and row, then the base row.
        Calling delete again after a finally-failed deletion creates a fresh
        task; while a delete task is open the view's ``delete_error`` is null.
        """

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                if row.status != "deleting":
                    row.status = "deleting"
                    row.updated_at = func.now()  # type: ignore[assignment]
                    session.add(_base_delete_task(project_id, row.id))
                else:
                    open_task = await session.scalar(
                        select(KnowledgeTaskRow.id).where(
                            KnowledgeTaskRow.kind == "delete_knowledge_base",
                            KnowledgeTaskRow.resource_id == row.id,
                            KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
                        )
                    )
                    if open_task is None:
                        session.add(_base_delete_task(project_id, row.id))
                await session.flush()
                await session.refresh(row)
                return await _view_with_derivations(session, row)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None


def _base_delete_task(project_id: UUID, base_id: UUID) -> KnowledgeTaskRow:
    return KnowledgeTaskRow(
        id=uuid4(),
        project_id=project_id,
        resource_id=base_id,
        kind="delete_knowledge_base",
        target_version=None,
        status="queued",
    )
