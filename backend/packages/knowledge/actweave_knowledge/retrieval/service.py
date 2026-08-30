"""Semantic retrieval: exact cosine recall, then optional reranker ordering.

Bases are grouped by their ``(embedding_model_id, reranker_model_id)`` pair —
a ``NULL`` reranker forms its own group — so vectors of different dimensions
never enter the same distance computation and each group spends its own
``candidate_k`` recall budget. Groups sharing an embedding model reuse one
query embedding per search. In a reranked group the reranker scores every
recalled candidate (``top_n = len(candidates)``) and its ``relevance_score``
(``[0,1]``) becomes the final score; a reranker failure fails the whole
search — never a silent cosine-only result. In a rerank-free group the final
score is the raw cosine similarity (``[-1,1]``). Groups apply their
thresholds first, then merge into one stable global ordering cut at
``top_k``; mixed rerank/cosine scores are deliberately comparable only as an
accepted quality limitation, not a calibration promise.

Recall runs two paths per group: general-mode segments carry their own
vectors, parent_child-mode documents recall through child chunks whose best
score rolls up to the parent segment (one candidate per parent). The reranker
always scores the text a citation would return — the parent content.

``top_k``/``score_threshold`` omitted by the caller resolve to the per-base
defaults stored on the knowledge bases; a positive threshold filters final
scores below it and ``0`` disables filtering entirely (negative cosine scores
pass). Every completed search appends one ``knowledge_queries`` row and
increments segment/document hit counters. A database-only metrics failure
stays best-effort, but a final authority revalidation failure aborts the
search so revoked callers never receive the already-computed citations.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Numeric, case, cast, func, null, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MAX_METADATA_FILTERS,
    KNOWLEDGE_MAX_METADATA_NAME_LENGTH,
    KNOWLEDGE_MAX_METADATA_STRING_LENGTH,
    KNOWLEDGE_MAX_TOP_K,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_SEARCH_FAILED,
    KnowledgeCitation,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeMetadataFilter,
    KnowledgeModelPort,
    KnowledgeQueryView,
    KnowledgeRerankMaterial,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
)
from ..models.client import KnowledgeModelClient
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeQueryRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
)

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 4
MAX_TOP_K = KNOWLEDGE_MAX_TOP_K
MAX_QUERY_CHARS = 2000
# Calibrated once against the real provider during M2 integration; 0 disables
# the filter entirely. Kept as the ultimate fallback; per-base defaults are
# the normal source when the request omits the value.
DEFAULT_SCORE_THRESHOLD = 0.2
SNIPPET_MAX_CHARS = 320

MAX_QUERY_PAGE_SIZE = 100

_CANDIDATE_K_FLOOR = 20
_CANDIDATE_K_CEILING = 100


def calculate_candidate_k(top_k: int) -> int:
    """Per-group recall size: ``min(100, max(20, top_k * 5))``."""

    return min(_CANDIDATE_K_CEILING, max(_CANDIDATE_K_FLOOR, top_k * 5))


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


# The retrieval contract surfaces database faults as search failures
# (KNOWLEDGE_SEARCH_FAILED), never as zero hits and never as the object-store
# code used by upload/download paths.
def _search_failed() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_SEARCH_FAILED, "检索暂时不可用，请稍后重试")


@dataclass(frozen=True, slots=True)
class _ValidatedSearch:
    """Range-checked request values; ``None`` means "use the base defaults"."""

    query: str
    top_k: int | None
    score_threshold: float | None
    metadata_filters: tuple[KnowledgeMetadataFilter, ...]


def _validated_metadata_filters(
    filters: tuple[KnowledgeMetadataFilter, ...] | None,
) -> tuple[KnowledgeMetadataFilter, ...]:
    """Bound and type-check manual metadata conditions (AND semantics).

    Field names are not resolved against definitions here: a condition on a
    name no targeted base defines simply matches no document of that base,
    mirroring the "missing key never matches" rule.
    """

    if filters is None:
        return ()
    if not isinstance(filters, (tuple, list)):
        raise _invalid("metadata_filters 必须是条件数组")
    if len(filters) > KNOWLEDGE_MAX_METADATA_FILTERS:
        raise _invalid(f"metadata_filters 最多 {KNOWLEDGE_MAX_METADATA_FILTERS} 个条件")
    validated: list[KnowledgeMetadataFilter] = []
    for item in filters:
        name = item.name.strip() if isinstance(item.name, str) else ""
        if not name or len(name) > KNOWLEDGE_MAX_METADATA_NAME_LENGTH:
            raise _invalid(f"过滤条件的 name 必须是 1-{KNOWLEDGE_MAX_METADATA_NAME_LENGTH} 个字符的非空文本")
        if item.operator not in ("eq", "contains", "gte", "lte"):
            raise _invalid("过滤条件的 operator 只能是 eq、contains、gte 或 lte")
        value = item.value
        if item.operator == "contains":
            if not isinstance(value, str) or not 1 <= len(value) <= KNOWLEDGE_MAX_METADATA_STRING_LENGTH:
                raise _invalid("contains 条件的 value 必须是非空字符串")
        elif item.operator in ("gte", "lte"):
            if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
                raise _invalid(f"{item.operator} 条件的 value 必须是有限数字")
        elif isinstance(value, str):
            if len(value) > KNOWLEDGE_MAX_METADATA_STRING_LENGTH:
                raise _invalid(f"eq 条件的字符串 value 最多 {KNOWLEDGE_MAX_METADATA_STRING_LENGTH} 个字符")
        elif type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
            raise _invalid("eq 条件的 value 必须是字符串或有限数字")
        validated.append(KnowledgeMetadataFilter(name=name, operator=item.operator, value=value))
    return tuple(validated)


def _metadata_filter_conditions(filters: tuple[KnowledgeMetadataFilter, ...]) -> tuple[Any, ...]:
    """Translate validated conditions into document-row SQL predicates.

    ``eq`` uses GIN-indexable JSONB containment (type-exact). ``contains``
    and the range operators guard on ``jsonb_typeof`` first — inside CASE so
    a string value can never reach the numeric cast — making a mismatched
    type a non-match instead of a query error.
    """

    conditions: list[Any] = []
    for item in filters:
        value_json = KnowledgeDocumentRow.doc_metadata[item.name]
        if item.operator == "eq":
            conditions.append(KnowledgeDocumentRow.doc_metadata.contains(func.jsonb_build_object(item.name, item.value)))
        elif item.operator == "contains":
            conditions.append(func.jsonb_typeof(value_json) == "string")
            conditions.append(func.strpos(value_json.astext, item.value) > 0)
        else:
            numeric_value = case(
                (func.jsonb_typeof(value_json) == "number", cast(value_json.astext, Numeric)),
                else_=null(),
            )
            if item.operator == "gte":
                conditions.append(numeric_value >= item.value)
            else:
                conditions.append(numeric_value <= item.value)
    return tuple(conditions)


def _validated_search(request: KnowledgeSearchRequest) -> _ValidatedSearch:
    query = request.query.strip()
    if not query:
        raise _invalid("query 不能为空")
    if len(query) > MAX_QUERY_CHARS:
        raise _invalid(f"query 不能超过 {MAX_QUERY_CHARS} 字符")
    top_k = request.top_k
    # type() checks reject bool, which is an int subclass.
    if top_k is not None and (type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K):
        raise _invalid(f"top_k 必须是 1..{MAX_TOP_K} 的整数")
    threshold = request.score_threshold
    if threshold is not None:
        if type(threshold) not in (int, float) or not 0 <= float(threshold) <= 1:
            raise _invalid("score_threshold 必须在 0..1 之间")
        threshold = float(threshold)
    if request.source not in ("agent", "retrieval_test"):
        raise _invalid("source 只能是 agent 或 retrieval_test")
    return _ValidatedSearch(
        query=query,
        top_k=top_k,
        score_threshold=threshold,
        metadata_filters=_validated_metadata_filters(request.metadata_filters),
    )


@dataclass(frozen=True, slots=True)
class _BaseDefaults:
    top_k: int
    score_threshold: float


@dataclass(frozen=True, slots=True)
class _SearchGroup:
    """Bases sharing one ``(embedding model, reranker model)`` pair.

    ``rerank`` is ``None`` for the NULL-reranker group: its candidates keep
    their cosine similarity as the final score.
    """

    embedding: KnowledgeEmbeddingMaterial
    rerank: KnowledgeRerankMaterial | None
    base_ids: list[UUID]


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One recalled segment with its display fields and recall-stage score.

    For parent_child documents this is the parent segment and
    ``vector_score`` is the best of its child-chunk scores.
    """

    segment_id: UUID
    position: int
    content: str
    source_position: dict[str, Any]
    document_id: UUID
    document_name: str
    knowledge_base_id: UUID
    knowledge_base_name: str
    vector_score: float


@dataclass(frozen=True, slots=True)
class _Ranked:
    """One candidate with its final score (rerank relevance or raw cosine)."""

    final_score: float
    candidate: _Candidate


def _stable_sort_key(item: _Ranked) -> tuple[float, float, UUID, UUID, int, UUID]:
    candidate = item.candidate
    return (
        -item.final_score,
        -candidate.vector_score,
        candidate.knowledge_base_id,
        candidate.document_id,
        candidate.position,
        candidate.segment_id,
    )


def _candidate_sort_key(candidate: _Candidate) -> tuple[float, UUID, UUID, int, UUID]:
    return (
        -candidate.vector_score,
        candidate.knowledge_base_id,
        candidate.document_id,
        candidate.position,
        candidate.segment_id,
    )


class KnowledgeSearchService:
    """Reusable search pipeline shared by the HTTP API and the Agent tool."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: KnowledgeModelClient,
        model_port: KnowledgeModelPort,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._model_port = model_port

    async def search(
        self,
        request: KnowledgeSearchRequest,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeSearchResult:
        if authority is not None and (authority.project_id != request.project_id or authority.actor_user_id != request.owner_user_id):
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")
        validated = _validated_search(request)
        groups, defaults = await self._searchable_groups(
            request.project_id,
            request.knowledge_base_ids,
            authority=authority,
        )
        if not groups:
            return KnowledgeSearchResult(citations=())

        # An omitted top_k widens to the largest per-base default among the
        # targeted bases, so no base's configured expectation is truncated.
        top_k = validated.top_k if validated.top_k is not None else max(item.top_k for item in defaults.values())
        candidate_k = calculate_candidate_k(top_k)
        # Groups sharing an embedding model reuse one query embedding per
        # search while keeping their own candidate_k recall budgets.
        query_vectors: dict[UUID, list[float]] = {}
        ranked: list[_Ranked] = []
        for group in groups:
            embedding = group.embedding
            if embedding.model_id not in query_vectors:
                # Each model may target a different Provider endpoint and
                # incur a separate request. Revalidate before every provider
                # call so revocation after an earlier group cannot cause
                # later spend.
                await self._revalidate_authority(
                    project_id=request.project_id,
                    authority=authority,
                )
                # Provider failures surface as bare KnowledgeError to callers,
                # so log the stage and code here (never the query or provider
                # body) — otherwise a production embedding/reranker outage is
                # invisible.
                try:
                    query_vectors[embedding.model_id] = (await self._client.embed(embedding, [validated.query]))[0]
                except KnowledgeError as error:
                    logger.warning(
                        "knowledge search embed failed for model %s: %s",
                        embedding.model_id,
                        error.code,
                    )
                    raise
            candidates = await self._recalled_candidates(
                project_id=request.project_id,
                embedding_model_id=embedding.model_id,
                base_ids=group.base_ids,
                query_vector=query_vectors[embedding.model_id],
                candidate_k=candidate_k,
                metadata_filters=validated.metadata_filters,
                authority=authority,
            )
            if not candidates:
                continue
            group_ranked: list[_Ranked] = []
            if group.rerank is None:
                # Rerank-free group: the final score stays the raw cosine
                # similarity in [-1,1]; a 0 threshold filters nothing.
                for candidate in candidates:
                    group_ranked.append(_Ranked(final_score=candidate.vector_score, candidate=candidate))
            else:
                # Recall freezes the exact Segment text under one
                # authority-checked database snapshot. A second short guard
                # immediately before the external Reranker narrows the
                # remaining handoff window without holding a database
                # transaction across Provider I/O.
                await self._revalidate_authority(
                    project_id=request.project_id,
                    authority=authority,
                )
                try:
                    # Score every candidate: per-base thresholds must filter
                    # before any top_k truncation, or a qualified candidate of
                    # a stricter base could be cut by a laxer base's hits.
                    scores = await self._client.rerank(
                        group.rerank,
                        validated.query,
                        [candidate.content for candidate in candidates],
                        top_n=len(candidates),
                    )
                except KnowledgeError as error:
                    logger.warning(
                        "knowledge search rerank failed for model %s: %s",
                        group.rerank.model_id,
                        error.code,
                    )
                    raise
                for score in scores:
                    group_ranked.append(_Ranked(final_score=score.score, candidate=candidates[score.index]))
            filtered = []
            for item in group_ranked:
                threshold = validated.score_threshold if validated.score_threshold is not None else defaults[item.candidate.knowledge_base_id].score_threshold
                if threshold > 0 and item.final_score < threshold:
                    continue
                filtered.append(item)
            # Thresholds are applied above on the full candidate set; the cap
            # only bounds what this group can contribute to the global top_k
            # (both paths are already sorted by final score descending).
            ranked.extend(filtered[:top_k])

        ranked.sort(key=_stable_sort_key)
        citations: list[KnowledgeCitation] = []
        seen_segments: set[UUID] = set()
        for item in ranked:
            candidate = item.candidate
            if candidate.segment_id in seen_segments:
                continue
            seen_segments.add(candidate.segment_id)
            citations.append(
                KnowledgeCitation(
                    knowledge_base_id=candidate.knowledge_base_id,
                    knowledge_base_name=candidate.knowledge_base_name,
                    document_id=candidate.document_id,
                    document_name=candidate.document_name,
                    segment_id=candidate.segment_id,
                    segment_position=candidate.position,
                    snippet=candidate.content[:SNIPPET_MAX_CHARS],
                    score=item.final_score,
                    source_position=dict(candidate.source_position),
                )
            )
            if len(citations) == top_k:
                break
        result = KnowledgeSearchResult(citations=tuple(citations))
        await self._record_search(
            project_id=request.project_id,
            owner_user_id=request.owner_user_id,
            searched_base_ids=sorted(base_id for group in groups for base_id in group.base_ids),
            query=validated.query,
            source=request.source,
            citations=result.citations,
            authority=authority,
        )
        return result

    async def list_recent_queries(
        self,
        project_id: UUID,
        owner_user_id: UUID,
        base_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[list[KnowledgeQueryView], int]:
        """Latest query-log rows that targeted ``base_id``, newest first."""

        if type(page) is not int or page < 1:
            raise _invalid("page 必须是不小于 1 的整数")
        if type(page_size) is not int or not 1 <= page_size <= MAX_QUERY_PAGE_SIZE:
            raise _invalid(f"page_size 必须是 1-{MAX_QUERY_PAGE_SIZE} 之间的整数")
        if authority is not None and (authority.project_id != project_id or authority.actor_user_id != owner_user_id):
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")
        # JSONB containment: the row's base-id array holds this base's id.
        base_filter = (
            KnowledgeQueryRow.project_id == project_id,
            KnowledgeQueryRow.owner_user_id == str(owner_user_id),
            KnowledgeQueryRow.knowledge_base_ids.contains([str(base_id)]),
        )
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                exists = await session.scalar(select(KnowledgeBaseRow.id).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id))
                if exists is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                total = await session.scalar(select(func.count()).select_from(KnowledgeQueryRow).where(*base_filter))
                rows = (await session.scalars(select(KnowledgeQueryRow).where(*base_filter).order_by(KnowledgeQueryRow.created_at.desc(), KnowledgeQueryRow.id.desc()).offset((page - 1) * page_size).limit(page_size))).all()
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            logger.warning("knowledge recent-query listing failed", exc_info=True)
            raise _search_failed() from None
        views = [
            KnowledgeQueryView(
                id=row.id,
                knowledge_base_ids=tuple(UUID(value) for value in row.knowledge_base_ids),
                query=row.query,
                source=row.source,  # type: ignore[arg-type]
                result_count=row.result_count,
                top_score=row.top_score,
                created_at=row.created_at,
            )
            for row in rows
        ]
        return views, int(total or 0)

    async def _record_search(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        searched_base_ids: list[UUID],
        query: str,
        source: str,
        citations: tuple[KnowledgeCitation, ...],
        authority: KnowledgeProjectAuthority | None = None,
    ) -> None:
        """Append query history and counters after the final authority check.

        Database failures remain best-effort observability failures. Authority
        failures deliberately propagate so a caller revoked during provider
        work does not receive the computed citations.
        """

        authority_checked = authority is None
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                authority_checked = True
                session.add(
                    KnowledgeQueryRow(
                        id=uuid4(),
                        project_id=project_id,
                        owner_user_id=str(owner_user_id),
                        knowledge_base_ids=[str(base_id) for base_id in searched_base_ids],
                        query=query,
                        source=source,
                        result_count=len(citations),
                        top_score=(max(citation.score for citation in citations) if citations else None),
                    )
                )
                if citations:
                    segment_ids = [citation.segment_id for citation in citations]
                    await session.execute(update(KnowledgeSegmentRow).where(KnowledgeSegmentRow.id.in_(segment_ids)).values(hit_count=KnowledgeSegmentRow.hit_count + 1))
                    hits_per_document: dict[UUID, int] = {}
                    for citation in citations:
                        hits_per_document[citation.document_id] = hits_per_document.get(citation.document_id, 0) + 1
                    for document_id, hits in hits_per_document.items():
                        await session.execute(update(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document_id).values(hit_count=KnowledgeDocumentRow.hit_count + hits))
        except SQLAlchemyError:
            if not authority_checked:
                logger.warning("knowledge final authority revalidation failed", exc_info=True)
                raise _search_failed() from None
            logger.warning("knowledge query log write failed", exc_info=True)

    async def _searchable_groups(
        self,
        project_id: UUID,
        base_ids: tuple[UUID, ...] | None,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[list[_SearchGroup], dict[UUID, _BaseDefaults]]:
        """Group the project's searchable bases by (embedding, reranker) pair.

        Explicit base ids narrow to their active subset; an explicitly empty
        selection searches nothing. An unresolvable bound model — missing,
        disabled, or with undecryptable material — fails the whole request
        (``KNOWLEDGE_MODEL_UNAVAILABLE`` from the port): its bases cannot
        silently vanish from results. The second return value carries each
        base's retrieval defaults.
        """

        if base_ids is not None and len(base_ids) == 0:
            await self._revalidate_authority(
                project_id=project_id,
                authority=authority,
            )
            return [], {}
        statement = select(
            KnowledgeBaseRow.id,
            KnowledgeBaseRow.embedding_model_id,
            KnowledgeBaseRow.reranker_model_id,
            KnowledgeBaseRow.default_top_k,
            KnowledgeBaseRow.default_score_threshold,
        ).where(
            KnowledgeBaseRow.project_id == project_id,
            KnowledgeBaseRow.status == "active",
        )
        if base_ids is not None:
            statement = statement.where(KnowledgeBaseRow.id.in_(base_ids))
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                rows = (await session.execute(statement.order_by(KnowledgeBaseRow.id))).all()
                if not rows:
                    return [], {}
                bases_by_pair: dict[tuple[UUID, UUID | None], list[UUID]] = {}
                defaults: dict[UUID, _BaseDefaults] = {}
                for base_id, embedding_model_id, reranker_model_id, default_top_k, default_score_threshold in rows:
                    bases_by_pair.setdefault((embedding_model_id, reranker_model_id), []).append(base_id)
                    defaults[base_id] = _BaseDefaults(top_k=default_top_k, score_threshold=float(default_score_threshold))
                # Materialize each distinct model once through the host port,
                # inside this authority-checked transaction. The port owns
                # type/active validation and decryption; every unresolvable
                # model raises KNOWLEDGE_MODEL_UNAVAILABLE.
                embedding_materials: dict[UUID, KnowledgeEmbeddingMaterial] = {}
                rerank_materials: dict[UUID, KnowledgeRerankMaterial] = {}
                for embedding_model_id, reranker_model_id in bases_by_pair:
                    if embedding_model_id not in embedding_materials:
                        embedding_materials[embedding_model_id] = await self._model_port.embedding_material(session, embedding_model_id)
                    if reranker_model_id is not None and reranker_model_id not in rerank_materials:
                        rerank_materials[reranker_model_id] = await self._model_port.rerank_material(session, reranker_model_id)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            logger.warning("knowledge search failed to load searchable bases", exc_info=True)
            raise _search_failed() from None
        groups = [
            _SearchGroup(
                embedding=embedding_materials[embedding_model_id],
                rerank=rerank_materials[reranker_model_id] if reranker_model_id is not None else None,
                base_ids=pair_base_ids,
            )
            for (embedding_model_id, reranker_model_id), pair_base_ids in sorted(
                bases_by_pair.items(),
                key=lambda entry: (entry[0][0], entry[0][1] or UUID(int=0)),
            )
        ]
        return groups, defaults

    async def _recalled_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        query_vector: list[float],
        candidate_k: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        authority: KnowledgeProjectAuthority | None = None,
    ) -> list[_Candidate]:
        """Merge general-segment recall with parent_child child-chunk rollup.

        Both paths run per group; the merged pool is re-sorted by vector score
        and capped at ``candidate_k`` so mixed-mode bases compete fairly.
        """

        # Both recall paths share the same short transaction. Authority is
        # revalidated before either query can load Segment content, so a
        # revocation during query embedding prevents that content from being
        # handed to the external Reranker.
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                general = await self._general_candidates(
                    project_id=project_id,
                    embedding_model_id=embedding_model_id,
                    base_ids=base_ids,
                    query_vector=query_vector,
                    candidate_k=candidate_k,
                    metadata_filters=metadata_filters,
                    session=session,
                )
                parents = await self._parent_child_candidates(
                    project_id=project_id,
                    embedding_model_id=embedding_model_id,
                    base_ids=base_ids,
                    query_vector=query_vector,
                    candidate_k=candidate_k,
                    metadata_filters=metadata_filters,
                    session=session,
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            logger.warning("knowledge cosine recall failed", exc_info=True)
            raise _search_failed() from None
        merged = sorted(general + parents, key=_candidate_sort_key)
        return merged[:candidate_k]

    async def _revalidate_authority(
        self,
        *,
        project_id: UUID,
        authority: KnowledgeProjectAuthority | None,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            logger.warning("knowledge search authority revalidation failed", exc_info=True)
            raise _search_failed() from None

    def _candidate_filters(
        self,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
    ) -> tuple[Any, ...]:
        return (
            KnowledgeBaseRow.project_id == project_id,
            KnowledgeBaseRow.id.in_(base_ids),
            KnowledgeBaseRow.status == "active",
            KnowledgeDocumentRow.status == "ready",
            # Governance switches: a disabled document or segment keeps its
            # vectors but never enters recall (nor Agent citations).
            KnowledgeDocumentRow.enabled.is_(True),
            KnowledgeSegmentRow.enabled.is_(True),
            KnowledgeSegmentRow.document_version == KnowledgeDocumentRow.version,
            # A base rebound to another embedding model between group load and
            # recall drops out here: its vectors no longer match this query
            # embedding's dimension/space.
            KnowledgeBaseRow.embedding_model_id == embedding_model_id,
            # Manual metadata conditions AND onto both recall paths, so a
            # non-matching document never reaches the reranker on either.
            *_metadata_filter_conditions(metadata_filters),
        )

    async def _general_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        query_vector: list[float],
        candidate_k: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession | None = None,
    ) -> list[_Candidate]:
        """Exact cosine recall over segments that carry their own vectors."""

        distance = KnowledgeSegmentRow.embedding.cosine_distance(query_vector)
        vector_score = (1 - distance).label("vector_score")
        statement = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.source_position,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                vector_score,
            )
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(
                *self._candidate_filters(project_id, embedding_model_id, base_ids, metadata_filters),
                # parent_child parents store NULL embeddings and are recalled
                # through their children instead.
                KnowledgeSegmentRow.embedding.is_not(None),
            )
            .order_by(
                vector_score.desc(),
                KnowledgeBaseRow.id.asc(),
                KnowledgeDocumentRow.id.asc(),
                KnowledgeSegmentRow.position.asc(),
                KnowledgeSegmentRow.id.asc(),
            )
            .limit(candidate_k)
        )
        return await self._execute_recall(statement, session=session)

    async def _parent_child_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        query_vector: list[float],
        candidate_k: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession | None = None,
    ) -> list[_Candidate]:
        """Child-chunk recall rolled up to parents before the reranker.

        The best child score inside each parent becomes the parent's recall
        score (the plan's 回卷 rule), so a parent never appears twice no
        matter how many of its children match.
        """

        child_distance = KnowledgeSegmentChildRow.embedding.cosine_distance(query_vector)
        vector_score = func.max(1 - child_distance).label("vector_score")
        statement = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.source_position,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                vector_score,
            )
            .select_from(KnowledgeSegmentChildRow)
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentChildRow.knowledge_segment_id)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(*self._candidate_filters(project_id, embedding_model_id, base_ids, metadata_filters))
            .group_by(
                KnowledgeSegmentRow.id,
                KnowledgeDocumentRow.id,
                KnowledgeBaseRow.id,
            )
            .order_by(
                vector_score.desc(),
                KnowledgeBaseRow.id.asc(),
                KnowledgeDocumentRow.id.asc(),
                KnowledgeSegmentRow.position.asc(),
                KnowledgeSegmentRow.id.asc(),
            )
            .limit(candidate_k)
        )
        return await self._execute_recall(statement, session=session)

    async def _execute_recall(
        self,
        statement: Any,
        *,
        session: AsyncSession | None = None,
    ) -> list[_Candidate]:
        try:
            if session is None:
                async with self._session_factory() as owned_session:
                    rows = (await owned_session.execute(statement)).all()
            else:
                rows = (await session.execute(statement)).all()
        except SQLAlchemyError:
            logger.warning("knowledge cosine recall failed", exc_info=True)
            raise _search_failed() from None
        return [
            _Candidate(
                segment_id=row.id,
                position=row.position,
                content=row.content,
                source_position=dict(row.source_position),
                document_id=row.document_id,
                document_name=row.document_name,
                knowledge_base_id=row.knowledge_base_id,
                knowledge_base_name=row.knowledge_base_name,
                vector_score=float(row.vector_score),
            )
            for row in rows
        ]
