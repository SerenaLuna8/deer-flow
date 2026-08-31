"""Two-route retrieval: exact cosine recall, an explicit lexical route, and
the fixed three-branch final ordering.

Bases are grouped by their ``(embedding_model_id, reranker_model_id)`` pair —
a ``NULL`` reranker forms its own group — so vectors of different dimensions
never enter the same distance computation. Every targeted base gets its own
per-route recall budget ``C = min(B, floor(G/N))`` with ``B = min(100,
max(20, 5*top_k))`` and the global parent budget ``G = 400`` (design §8.2), so
a large base cannot starve a small one out of the reranker; ``C < 1`` (more
than 400 target bases) is an explicit refusal to narrow. Groups sharing an
embedding model reuse one query embedding per search. In a reranked group the
reranker scores every recalled candidate (``top_n = len(candidates)``) and
its ``relevance_score`` (``[0,1]``) is the native score; a reranker failure
fails the whole search — never a silent cosine-only result. In a rerank-free
group the native score is the raw cosine similarity (``[-1,1]``).

A base in ``retrieval_mode='hybrid'`` (or a one-call request override, never
persisted) adds the lexical route: a parameterized OR tsquery of the query's
lexical_v1 tokens (at most 128 deduplicated tokens with a hybrid target;
none at all without one), scored by ``ts_rank_cd(..., 2)`` — general mode on
segment rows, parent_child on child rows rolled up to their parent's best
score. The two routes merge per base by ``Σ 1/(60+rank)`` before the cap
``C``, and every lexical-only candidate still gets its real cosine
(parent_child: the max over all current children), because per-base
thresholds act on native scores only. Rows whose ``lexical_version`` does
not match the fixed derivation version fail the search loudly — the lexical
route never silently skips or backfills them.

The final ordering then branches on the strategy the targeted bases bind
(design §8.3): one shared non-null reranker keeps native rerank ordering
(the lexical route only widens recall); an all-semantic single score domain
keeps native ordering; anything else — heterogeneous domains, or hybrid
without a unified reranker — ranks candidates inside each domain (``RANK``
semantics: equal native scores share a place), scores every shortlisted
parent with the same lexical query (semantic bases included, positive
scores building one global lexical ranking), and fuses with ``61/2 *
(1/(60+domain_rank) + 1/(60+lexical_rank))`` where a missing term is 0.
Fused scores are ordering evidence in ``[0,1]``, never calibrated
confidence; identity only breaks fused ties. Fusion without any lexical
evidence reports ``heterogeneous_without_lexical_evidence``.

Recall runs per group: general-mode segments carry their own vectors,
parent_child-mode documents recall through child chunks whose best score
rolls up to the parent segment. Enabled summary indexes add a third semantic
source; a Segment's maximum source cosine wins before its base's semantic
cap. Summaries are never citation text: the reranker always scores the real
Segment content.

The effective strategy snapshot (every targeted base's model bindings and
resolved retrieval settings) is
re-checked before every provider dispatch and inside the final review: a
mid-search rebinding of any targeted base raises ``KNOWLEDGE_CONFLICT``
instead of mixing two strategies into one result. Every completed search
appends one ``knowledge_queries`` row (``top_score``/``top_score_kind`` come
from the same returned hit) and increments segment/document hit counters; a
final authority revalidation failure aborts the search so revoked callers
never receive the already-computed citations. ``debug=true`` responses carry
bounded safe diagnostics — actual counts, monotonic stage timings and the
empty reason — never passages, child text or losing candidates.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Numeric, case, cast, func, literal, null, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import (
    KNOWLEDGE_BUILTIN_FILTER_FIELD_TYPES,
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_FILTER_OPERATORS_BY_TYPE,
    KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_LEXICAL_VERSION,
    KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS,
    KNOWLEDGE_MAX_MATCHED_CHILDREN,
    KNOWLEDGE_MAX_METADATA_FILTERS,
    KNOWLEDGE_MAX_METADATA_NAME_LENGTH,
    KNOWLEDGE_MAX_METADATA_STRING_LENGTH,
    KNOWLEDGE_MAX_TOP_K,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_SEARCH_FAILED,
    KNOWLEDGE_STRATEGY_VERSION,
    KnowledgeCitation,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeHitDiagnostics,
    KnowledgeLocalScoreKind,
    KnowledgeMatchedChild,
    KnowledgeMatchedVia,
    KnowledgeMetadataFilter,
    KnowledgeModelPort,
    KnowledgeQueryView,
    KnowledgeRerankMaterial,
    KnowledgeRetrievalMode,
    KnowledgeRouteCounts,
    KnowledgeScoreKind,
    KnowledgeSearchDiagnostics,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeSearchTimings,
)
from ..models.client import KnowledgeModelClient
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeQueryRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeSegmentSummaryRow,
)
from .lexical import lexical_query_input
from .query_cache import KnowledgeQueryEmbeddingCache

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
    """Recall scale ``B = min(100, max(20, top_k * 5))`` (design §8.2)."""

    return min(_CANDIDATE_K_CEILING, max(_CANDIDATE_K_FLOOR, top_k * 5))


def calculate_per_base_budget(top_k: int, target_base_count: int) -> int:
    """Per-base per-route budget ``C = min(B, floor(G/N))`` (design §8.2).

    ``0`` (more target bases than the global parent budget) means the search
    must be rejected with an explicit narrowing hint, never silently truncated.
    """

    return min(
        calculate_candidate_k(top_k),
        KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET // target_base_count,
    )


def _rank_fusion_score(domain_rank: int, lexical_rank: int | None = None) -> float:
    """Design §8.3: ``61/2 * (1/(60+domain_rank) + 1/(60+lexical_rank))``.

    A candidate without a positive lexical score has no lexical rank and its
    second term is 0, capping the fused score at 0.5; both ranks at 1 give
    exactly 1.0.
    """

    lexical_term = 1.0 / (60.0 + lexical_rank) if lexical_rank is not None else 0.0
    return 61.0 / 2.0 * (1.0 / (60.0 + domain_rank) + lexical_term)


def _route_rrf_value(rank: int) -> float:
    """One route's contribution to the per-base recall merge (design §8.2)."""

    return 1.0 / (60.0 + rank)


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


# The retrieval contract surfaces database faults as search failures
# (KNOWLEDGE_SEARCH_FAILED), never as zero hits and never as the object-store
# code used by upload/download paths.
def _search_failed() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_SEARCH_FAILED, "检索暂时不可用，请稍后重试")


def _lexical_stale_conflict() -> KnowledgeError:
    """A row on another lexical_version: fail loudly, never skip or backfill."""

    return KnowledgeError(
        KNOWLEDGE_CONFLICT,
        "检索范围内存在词法索引版本不一致的内容，请重新解析相关文档后重试，或改用 semantic 检索",
    )


@dataclass(frozen=True, slots=True)
class _ValidatedSearch:
    """Range-checked request values; ``None`` means "use the base defaults"."""

    query: str
    top_k: int | None
    score_threshold: float | None
    metadata_filters: tuple[KnowledgeMetadataFilter, ...]
    retrieval_mode: KnowledgeRetrievalMode | None


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
        if item.field_kind not in ("custom", "builtin"):
            raise _invalid("过滤条件的 field_kind 只能是 custom 或 builtin")
        if item.operator not in ("eq", "contains", "gte", "lte"):
            raise _invalid("过滤条件的 operator 只能是 eq、contains、gte 或 lte")
        value = item.value
        if item.field_kind == "builtin":
            # Builtin fields are a frozen vocabulary with known types, so a
            # condition that could never match is a client error, not a
            # silent non-match.
            field_type = KNOWLEDGE_BUILTIN_FILTER_FIELD_TYPES.get(name)
            if field_type is None:
                raise _invalid(f"未知的内建过滤字段 {name}")
            if item.operator not in KNOWLEDGE_FILTER_OPERATORS_BY_TYPE[field_type]:
                raise _invalid(f"内建字段 {name} 不支持 {item.operator} 条件")
            if field_type == "string":
                if not isinstance(value, str) or not 1 <= len(value) <= KNOWLEDGE_MAX_METADATA_STRING_LENGTH:
                    raise _invalid(f"内建字段 {name} 的 value 必须是字符串")
            elif type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
                raise _invalid(f"内建字段 {name} 的 value 必须是有限数字（epoch 秒）")
        elif item.operator == "contains":
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
        validated.append(KnowledgeMetadataFilter(name=name, operator=item.operator, value=value, field_kind=item.field_kind))
    return tuple(validated)


def _builtin_filter_conditions(item: KnowledgeMetadataFilter) -> tuple[Any, ...]:
    """One builtin condition against the live document authority columns.

    ``document_name`` reads the display name, ``uploaded_at`` compares
    ``created_at`` as epoch seconds, ``file_type`` derives the lowercased
    original-file extension (no extension never matches), and
    ``source_type`` is the fixed ingestion channel ``file_upload``.
    """

    if item.name == "document_name":
        if item.operator == "eq":
            return (KnowledgeDocumentRow.name == item.value,)
        return (func.strpos(KnowledgeDocumentRow.name, item.value) > 0,)
    if item.name == "uploaded_at":
        epoch = func.extract("epoch", KnowledgeDocumentRow.created_at)
        if item.operator == "eq":
            return (epoch == item.value,)
        if item.operator == "gte":
            return (epoch >= item.value,)
        return (epoch <= item.value,)
    if item.name == "file_type":
        extension = func.lower(func.substring(KnowledgeDocumentRow.original_name, r"\.([^.]+)$"))
        needle = str(item.value).lower()
        if item.operator == "eq":
            return (extension == needle,)
        return (func.strpos(extension, needle) > 0,)
    # source_type: every stored document came through file upload.
    if item.operator == "eq":
        return (literal("file_upload") == item.value,)
    return (func.strpos(literal("file_upload"), item.value) > 0,)


def _metadata_filter_conditions(filters: tuple[KnowledgeMetadataFilter, ...]) -> tuple[Any, ...]:
    """Translate validated conditions into document-row SQL predicates.

    Custom ``eq`` uses GIN-indexable JSONB containment (type-exact);
    ``contains`` and the range operators guard on ``jsonb_typeof`` first —
    inside CASE so a string value can never reach the numeric cast — making
    a mismatched type a non-match instead of a query error. Builtin
    conditions read authority columns instead of ``doc_metadata``. Both
    recall and the final review build their predicates through here, so a
    scope change mid-search can never leak past the review.
    """

    conditions: list[Any] = []
    for item in filters:
        if item.field_kind == "builtin":
            conditions.extend(_builtin_filter_conditions(item))
            continue
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
    if request.retrieval_mode not in (None, "semantic", "hybrid"):
        raise _invalid("retrieval_mode 只能是 semantic 或 hybrid")
    return _ValidatedSearch(
        query=query,
        top_k=top_k,
        score_threshold=threshold,
        metadata_filters=_validated_metadata_filters(request.metadata_filters),
        retrieval_mode=request.retrieval_mode,
    )


@dataclass(frozen=True, slots=True)
class _BaseDefaults:
    top_k: int
    score_threshold: float
    retrieval_mode: KnowledgeRetrievalMode
    summary_index_enabled: bool


def _effective_defaults(defaults: _BaseDefaults, overrides: _ValidatedSearch) -> _BaseDefaults:
    return _BaseDefaults(
        top_k=overrides.top_k if overrides.top_k is not None else defaults.top_k,
        score_threshold=overrides.score_threshold if overrides.score_threshold is not None else defaults.score_threshold,
        retrieval_mode=overrides.retrieval_mode if overrides.retrieval_mode is not None else defaults.retrieval_mode,
        summary_index_enabled=defaults.summary_index_enabled,
    )


@dataclass(frozen=True, slots=True)
class _SearchSnapshot:
    model_bindings: dict[UUID, tuple[UUID, UUID | None]]
    effective_defaults: dict[UUID, _BaseDefaults]
    overrides: _ValidatedSearch


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

    ``vector_score`` is the maximum Segment/Child/Summary source cosine;
    ``matched_via`` identifies its source. ``content`` is
    the complete parent text frozen by the recall snapshot; hits carry it as
    the passage while citations only quote its head. ``matched_children``
    are the really-recalled child chunks, carried by the recall transaction
    itself (empty for general-mode segments).
    """

    segment_id: UUID
    position: int
    content: str
    source_position: dict[str, Any]
    document_id: UUID
    document_name: str
    document_version: int
    knowledge_base_id: UUID
    knowledge_base_name: str
    vector_score: float
    matched_children: tuple[KnowledgeMatchedChild, ...] = ()
    matched_via: KnowledgeMatchedVia = "segment"


@dataclass(frozen=True, slots=True)
class _Ranked:
    """One candidate with its native score and score-domain provenance."""

    final_score: float
    local_score_kind: KnowledgeLocalScoreKind
    score_domain: str
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


def _rank_fused(ranked: list[_Ranked], lexical_ranks: dict[UUID, int]) -> list[tuple[_Ranked, float]]:
    """Fusion branch: RANK inside each domain, plus the global lexical rank.

    Equal native scores share a place (``1, 1, 3`` — never row_number), so
    equal evidence keeps an equal fused score; resource identity only orders
    fused ties, it never manufactures a difference. The vector score is
    deliberately absent from the final key: comparing raw scores across
    domains is exactly what fusion avoids. ``lexical_ranks`` carries the
    shared-place rank of every shortlisted parent with a positive lexical
    score (empty without lexical evidence, making the second term 0).
    """

    by_domain: dict[str, list[_Ranked]] = {}
    for item in ranked:
        by_domain.setdefault(item.score_domain, []).append(item)
    fused: list[tuple[_Ranked, float]] = []
    for items in by_domain.values():
        items.sort(key=_stable_sort_key)
        rank = 0
        previous_score: float | None = None
        for index, item in enumerate(items, start=1):
            if previous_score is None or item.final_score != previous_score:
                rank = index
                previous_score = item.final_score
            fused.append((item, _rank_fusion_score(rank, lexical_ranks.get(item.candidate.segment_id))))

    def _fused_key(entry: tuple[_Ranked, float]) -> tuple[float, UUID, UUID, int, UUID]:
        candidate = entry[0].candidate
        return (
            -entry[1],
            candidate.knowledge_base_id,
            candidate.document_id,
            candidate.position,
            candidate.segment_id,
        )

    fused.sort(key=_fused_key)
    return fused


async def _assert_snapshot_strategy(
    session: AsyncSession,
    project_id: UUID,
    snapshot: _SearchSnapshot,
) -> None:
    """Every targeted base still has the effective search strategy, or conflict.

    A deleted base is not a rebinding: its rows simply leave retrieval scope
    and any of its hits drop at the final review. A request override shields
    that setting from unrelated changes to the base's unused default.
    """

    rows = (
        await session.execute(
            select(
                KnowledgeBaseRow.id,
                KnowledgeBaseRow.embedding_model_id,
                KnowledgeBaseRow.reranker_model_id,
                KnowledgeBaseRow.default_top_k,
                KnowledgeBaseRow.default_score_threshold,
                KnowledgeBaseRow.retrieval_mode,
                KnowledgeBaseRow.summary_index_enabled,
            ).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id.in_(snapshot.model_bindings))
        )
    ).all()
    current_defaults = dict(snapshot.effective_defaults)
    for row in rows:
        actual = _effective_defaults(
            _BaseDefaults(
                top_k=row.default_top_k,
                score_threshold=float(row.default_score_threshold),
                retrieval_mode=row.retrieval_mode,
                summary_index_enabled=row.summary_index_enabled,
            ),
            snapshot.overrides,
        )
        expected = snapshot.effective_defaults[row.id]
        if (
            snapshot.model_bindings[row.id] != (row.embedding_model_id, row.reranker_model_id)
            or expected.score_threshold != actual.score_threshold
            or expected.retrieval_mode != actual.retrieval_mode
            or expected.summary_index_enabled != actual.summary_index_enabled
        ):
            raise KnowledgeError(KNOWLEDGE_CONFLICT, "检索策略已变更，请重新检索")
        current_defaults[row.id] = actual
    # top_k is one global limit: a smaller base default changing without
    # changing the maximum cannot alter this search's budget or result cap.
    # Missing bases keep their snapshot settings; deletion only drops hits.
    if max(item.top_k for item in current_defaults.values()) != max(item.top_k for item in snapshot.effective_defaults.values()):
        raise KnowledgeError(KNOWLEDGE_CONFLICT, "检索策略已变更，请重新检索")


def _candidate_sort_key(candidate: _Candidate) -> tuple[float, UUID, UUID, int, UUID]:
    return (
        -candidate.vector_score,
        candidate.knowledge_base_id,
        candidate.document_id,
        candidate.position,
        candidate.segment_id,
    )


def _identity_key(candidate: _Candidate) -> tuple[UUID, UUID, int, UUID]:
    return (
        candidate.knowledge_base_id,
        candidate.document_id,
        candidate.position,
        candidate.segment_id,
    )


def _merge_recall_routes(
    semantic_pool: list[_Candidate],
    lexical_pool: list[tuple[_Candidate, float]],
    cap: int,
) -> list[_Candidate]:
    """One hybrid base's recall merge: ``Σ 1/(60+rank)``, then keep ``C``.

    Each route ranks with shared places (``RANK``: equal scores share, never
    row_number) — semantic by cosine, lexical by ``ts_rank_cd`` — and a parent
    missing from a route simply contributes 0 for it. Identity only breaks
    ties in the final merged order.
    """

    rrf: dict[UUID, float] = {}
    candidates: dict[UUID, _Candidate] = {}

    def _accumulate(entries: list[tuple[float, _Candidate]]) -> None:
        rank = 0
        previous: float | None = None
        for index, (score, candidate) in enumerate(entries, start=1):
            if previous is None or score != previous:
                rank = index
                previous = score
            rrf[candidate.segment_id] = rrf.get(candidate.segment_id, 0.0) + _route_rrf_value(rank)
            candidates.setdefault(candidate.segment_id, candidate)

    _accumulate(sorted(((candidate.vector_score, candidate) for candidate in semantic_pool), key=lambda entry: (-entry[0], *_identity_key(entry[1]))))
    _accumulate(sorted(((score, candidate) for candidate, score in lexical_pool), key=lambda entry: (-entry[0], *_identity_key(entry[1]))))

    ordered = sorted(candidates.values(), key=lambda candidate: (-rrf[candidate.segment_id], *_identity_key(candidate)))
    return ordered[:cap]


def _empty_scope_diagnostics(validated: _ValidatedSearch) -> KnowledgeSearchDiagnostics:
    """Debug shape when nothing is searchable: zero targets, ``not_ready``."""

    return KnowledgeSearchDiagnostics(
        strategy_version=KNOWLEDGE_STRATEGY_VERSION,
        lexical_version=KNOWLEDGE_LEXICAL_VERSION,
        target_base_count=0,
        effective_top_k=validated.top_k if validated.top_k is not None else DEFAULT_TOP_K,
        per_base_route_budget=0,
        retrieval_mode="semantic",
        counts=KnowledgeRouteCounts(),
        timings=KnowledgeSearchTimings(),
        empty_reason="not_ready",
    )


def _current_scope_filters(
    project_id: UUID,
    metadata_filters: tuple[KnowledgeMetadataFilter, ...],
) -> tuple[Any, ...]:
    """Rows currently inside retrieval scope; recall and the final review share it.

    Governance switches: a disabled document or segment keeps its vectors but
    never enters recall (nor Agent citations). Manual metadata conditions AND
    onto every path, so a non-matching document neither reaches the reranker
    nor survives the final review.
    """

    return (
        KnowledgeBaseRow.project_id == project_id,
        KnowledgeBaseRow.status == "active",
        KnowledgeDocumentRow.status == "ready",
        KnowledgeDocumentRow.enabled.is_(True),
        KnowledgeSegmentRow.enabled.is_(True),
        KnowledgeSegmentRow.document_version == KnowledgeDocumentRow.version,
        *_metadata_filter_conditions(metadata_filters),
    )


class KnowledgeSearchService:
    """Reusable search pipeline shared by the HTTP API and the Agent tool."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: KnowledgeModelClient,
        model_port: KnowledgeModelPort,
        query_cache: KnowledgeQueryEmbeddingCache | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._model_port = model_port
        self._query_cache = query_cache

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
            # N = 0 searches nothing (§8.2); with debug the shape still says so.
            return KnowledgeSearchResult(diagnostics=_empty_scope_diagnostics(validated) if request.debug else None)

        defaults = {base_id: _effective_defaults(base_defaults, validated) for base_id, base_defaults in defaults.items()}
        # An omitted top_k widens to the largest per-base default among the
        # targeted bases, so no base's configured expectation is truncated.
        top_k = max(item.top_k for item in defaults.values())
        target_base_count = len(defaults)
        per_base_budget = calculate_per_base_budget(top_k, target_base_count)
        if per_base_budget < 1:
            raise _invalid(f"目标 Knowledge Base 数量（{target_base_count}）超过全局候选预算 {KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET}，请用 knowledge_base_ids 缩小检索范围")
        # A request override applies to every targeted base for this one
        # call; the per-base configuration is untouched. Without a hybrid
        # target no lexical query exists and no token cap applies.
        hybrid_base_ids = frozenset(base_id for base_id, base_defaults in defaults.items() if base_defaults.retrieval_mode == "hybrid")
        lexical_query: str | None = None
        if hybrid_base_ids:
            query_tokens = lexical_query_input(validated.query)
            if len(query_tokens) > KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS:
                raise _invalid(f"检索文本包含 {len(query_tokens)} 个去重词元，超过 hybrid 检索上限 {KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS}；请缩短检索文本，或改用 semantic 检索")
            # Zero tokens leave the lexical route empty; the vector route
            # still runs.
            lexical_query = " | ".join(query_tokens) if query_tokens else None
        # The effective strategy snapshot of this search: every targeted
        # base's model bindings and resolved settings, frozen by group load.
        snapshot = _SearchSnapshot(
            model_bindings={base_id: (group.embedding.model_id, group.rerank.model_id if group.rerank is not None else None) for group in groups for base_id in group.base_ids},
            effective_defaults=defaults,
            overrides=validated,
        )

        # Groups sharing an embedding model reuse one query embedding per
        # search while keeping their own per-base recall budgets.
        query_vectors: dict[UUID, list[float]] = {}
        ranked: list[_Ranked] = []
        semantic_candidates = 0
        lexical_candidates = 0
        summary_candidates = 0
        threshold_filtered = 0
        query_embedding_cache_hits = 0
        query_embedding_cache_misses = 0
        timings = {"query_embedding_ms": 0.0, "recall_ms": 0.0, "rerank_ms": 0.0, "final_validation_ms": 0.0}

        # Every provider dispatch — each batch and the client's internal
        # retry — re-checks authority and the strategy snapshot first, so a
        # revocation or rebinding between batches stops the undispatched
        # remainder instead of causing later spend under a stale strategy.
        async def _dispatch_guard() -> None:
            await self._revalidate_dispatch(
                project_id=request.project_id,
                authority=authority,
                snapshot=snapshot,
            )

        for group in groups:
            embedding = group.embedding
            if embedding.model_id not in query_vectors:
                started = time.monotonic()
                cached = self._query_cache.get(embedding.model_id, validated.query) if self._query_cache is not None else None
                if cached is not None:
                    query_embedding_cache_hits += 1
                    query_vectors[embedding.model_id] = list(cached)
                else:
                    query_embedding_cache_misses += 1
                    # Provider failures surface as bare KnowledgeError to
                    # callers. Log only the stage/code, never cached content,
                    # query text or the Provider response body.
                    try:
                        vector = (
                            await self._client.embed(
                                embedding,
                                [validated.query],
                                batch_guard=_dispatch_guard,
                            )
                        )[0]
                    except KnowledgeError as error:
                        logger.warning(
                            "knowledge search embed failed for model %s: %s",
                            embedding.model_id,
                            error.code,
                        )
                        raise
                    query_vectors[embedding.model_id] = vector
                    if self._query_cache is not None:
                        self._query_cache.put(embedding.model_id, validated.query, vector)
                timings["query_embedding_ms"] += (time.monotonic() - started) * 1000.0
            # Cache hits skip Provider dispatch only. Recall and final review
            # retain their transaction-bound live authorization checks.
            started = time.monotonic()
            candidates, group_semantic, group_lexical, group_summary = await self._recalled_candidates(
                project_id=request.project_id,
                embedding_model_id=embedding.model_id,
                base_ids=group.base_ids,
                hybrid_base_ids=hybrid_base_ids,
                query_vector=query_vectors[embedding.model_id],
                lexical_query=lexical_query,
                per_base_budget=per_base_budget,
                metadata_filters=validated.metadata_filters,
                authority=authority,
            )
            timings["recall_ms"] += (time.monotonic() - started) * 1000.0
            semantic_candidates += group_semantic
            lexical_candidates += group_lexical
            summary_candidates += group_summary
            if not candidates:
                continue
            group_ranked: list[_Ranked] = []
            if group.rerank is None:
                # Rerank-free group: the native score stays the raw cosine
                # similarity in [-1,1]; a 0 threshold filters nothing. The
                # score domain is the embedding model, not "the base".
                cosine_domain = f"cosine:{embedding.model_id}"
                for candidate in candidates:
                    group_ranked.append(
                        _Ranked(
                            final_score=candidate.vector_score,
                            local_score_kind="cosine",
                            score_domain=cosine_domain,
                            candidate=candidate,
                        )
                    )
            else:
                # Recall freezes the exact Segment text under one
                # authority-checked database snapshot. The per-batch guard
                # re-runs immediately before every Reranker dispatch, so a
                # revocation between candidate batches stops the remaining
                # batches without holding a database transaction across
                # Provider I/O.
                started = time.monotonic()
                try:
                    # Score every candidate: per-base thresholds must filter
                    # before any top_k truncation, or a qualified candidate of
                    # a stricter base could be cut by a laxer base's hits.
                    scores = await self._client.rerank(
                        group.rerank,
                        validated.query,
                        [candidate.content for candidate in candidates],
                        top_n=len(candidates),
                        batch_guard=_dispatch_guard,
                    )
                except KnowledgeError as error:
                    logger.warning(
                        "knowledge search rerank failed for model %s: %s",
                        group.rerank.model_id,
                        error.code,
                    )
                    raise
                timings["rerank_ms"] += (time.monotonic() - started) * 1000.0
                rerank_domain = f"rerank:{group.rerank.model_id}"
                for score in scores:
                    group_ranked.append(
                        _Ranked(
                            final_score=score.score,
                            local_score_kind="rerank",
                            score_domain=rerank_domain,
                            candidate=candidates[score.index],
                        )
                    )
            # Per-base thresholds act on native scores only — never on the
            # fused ranking score (§8.3).
            for item in group_ranked:
                threshold = defaults[item.candidate.knowledge_base_id].score_threshold
                if threshold > 0 and item.final_score < threshold:
                    threshold_filtered += 1
                    continue
                ranked.append(item)

        # §8.3 branches on the strategy the targeted bases bind — not on which
        # candidates happened to survive — so the ranking method is stable for
        # a given target set: one shared non-null reranker keeps native
        # ordering (the lexical route only widened recall), an all-semantic
        # single domain keeps native ordering, anything else fuses.
        domains = {("rerank", group.rerank.model_id) if group.rerank is not None else ("cosine", group.embedding.model_id) for group in groups}
        unified_reranker = len(domains) == 1 and next(iter(domains))[0] == "rerank"
        fusion = len(domains) > 1 or (bool(hybrid_base_ids) and not unified_reranker)
        lexical_ranks: dict[UUID, int] = {}
        if fusion:
            if lexical_query is not None and ranked:
                # Every shortlisted parent — semantic bases included — is
                # scored by the same lexical query; positive scores build one
                # global shared-place ranking (§8.3 branch 3).
                started = time.monotonic()
                lexical_ranks = await self._final_lexical_ranks(
                    project_id=request.project_id,
                    segment_ids=[item.candidate.segment_id for item in ranked],
                    lexical_query=lexical_query,
                )
                timings["recall_ms"] += (time.monotonic() - started) * 1000.0
            ordered = _rank_fused(ranked, lexical_ranks)
        else:
            ranked.sort(key=_stable_sort_key)
            ordered = [(item, item.final_score) for item in ranked]
        ranking_method: KnowledgeScoreKind = "rank_fusion" if fusion else next(iter(domains))[0]  # type: ignore[assignment]

        pending: list[KnowledgeSearchHit] = []
        matched_via_by_segment: dict[UUID, KnowledgeMatchedVia] = {}
        seen_segments: set[UUID] = set()
        parents_deduplicated = 0
        for item, ranking_score in ordered:
            candidate = item.candidate
            if candidate.segment_id in seen_segments:
                parents_deduplicated += 1
                continue
            seen_segments.add(candidate.segment_id)
            matched_via_by_segment[candidate.segment_id] = candidate.matched_via
            content_digest = hashlib.sha256(candidate.content.encode("utf-8")).hexdigest()
            citation = KnowledgeCitation(
                knowledge_base_id=candidate.knowledge_base_id,
                knowledge_base_name=candidate.knowledge_base_name,
                document_id=candidate.document_id,
                document_name=candidate.document_name,
                segment_id=candidate.segment_id,
                segment_position=candidate.position,
                snippet=candidate.content[:SNIPPET_MAX_CHARS],
                # The final ordering score: native in a single domain, fused
                # otherwise — and score_kind always travels on the same row.
                score=ranking_score,
                source_position=dict(candidate.source_position),
                document_version=candidate.document_version,
                content_digest=content_digest,
                score_kind=ranking_method,
            )
            pending.append(
                KnowledgeSearchHit(
                    citation=citation,
                    passage=candidate.content,
                    document_version=candidate.document_version,
                    content_digest=content_digest,
                    local_score=item.final_score,
                    local_score_kind=item.local_score_kind,
                    score_domain=item.score_domain,
                    ranking_method=ranking_method,
                    ranking_score=ranking_score,
                    matched_children=candidate.matched_children,
                )
            )
            if len(pending) == top_k:
                break
        # Hits dropped by the final review are never backfilled from the
        # remaining ranked pool: the pool was scored under the old snapshot.
        started = time.monotonic()
        hits = await self._review_and_record(
            project_id=request.project_id,
            owner_user_id=request.owner_user_id,
            snapshot=snapshot,
            query=validated.query,
            source=request.source,
            pending=pending,
            metadata_filters=validated.metadata_filters,
            authority=authority,
        )
        timings["final_validation_ms"] = (time.monotonic() - started) * 1000.0
        diagnostics: KnowledgeSearchDiagnostics | None = None
        if request.debug:
            model_ids: set[UUID] = set()
            for group in groups:
                model_ids.add(group.embedding.model_id)
                if group.rerank is not None:
                    model_ids.add(group.rerank.model_id)
            empty_reason = None
            if not hits:
                if semantic_candidates + lexical_candidates == 0:
                    empty_reason = "no_candidates"
                elif not ranked:
                    empty_reason = "filtered_out"
                else:
                    empty_reason = "stale_candidates"
            diagnostics = KnowledgeSearchDiagnostics(
                strategy_version=KNOWLEDGE_STRATEGY_VERSION,
                lexical_version=KNOWLEDGE_LEXICAL_VERSION,
                target_base_count=target_base_count,
                effective_top_k=top_k,
                per_base_route_budget=per_base_budget,
                retrieval_mode="hybrid" if hybrid_base_ids else "semantic",
                counts=KnowledgeRouteCounts(
                    semantic_candidates=semantic_candidates,
                    lexical_candidates=lexical_candidates,
                    summary_candidates=summary_candidates,
                    parents_deduplicated=parents_deduplicated,
                    threshold_filtered=threshold_filtered,
                    stale_filtered=len(pending) - len(hits),
                    returned=len(hits),
                    query_embedding_cache_hits=query_embedding_cache_hits,
                    query_embedding_cache_misses=query_embedding_cache_misses,
                ),
                timings=KnowledgeSearchTimings(
                    query_embedding_ms=timings["query_embedding_ms"],
                    recall_ms=timings["recall_ms"],
                    rerank_ms=timings["rerank_ms"],
                    final_validation_ms=timings["final_validation_ms"],
                ),
                model_ids=tuple(sorted(model_ids)),
                ranking_method=ranking_method,
                empty_reason=empty_reason,
                # Fusion that found no positive lexical score anywhere is the
                # documented fairness compromise (§8.3): only in-domain places
                # order the result, and the UI must say so.
                heterogeneous_without_lexical_evidence=fusion and not lexical_ranks,
                hit_diagnostics=tuple(
                    KnowledgeHitDiagnostics(
                        segment_id=hit.citation.segment_id,
                        local_score=hit.local_score,
                        local_score_kind=hit.local_score_kind,
                        score_domain=hit.score_domain,
                        ranking_method=hit.ranking_method,
                        ranking_score=hit.ranking_score,
                        matched_children=hit.matched_children,
                        matched_via=matched_via_by_segment[hit.citation.segment_id],
                    )
                    for hit in hits
                ),
            )
        return KnowledgeSearchResult(hits=hits, diagnostics=diagnostics)

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
                top_score_kind=row.top_score_kind,  # type: ignore[arg-type]
                strategy_version=row.strategy_version,
            )
            for row in rows
        ]
        return views, int(total or 0)

    async def _review_and_record(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        snapshot: _SearchSnapshot,
        query: str,
        source: str,
        pending: list[KnowledgeSearchHit],
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[KnowledgeSearchHit, ...]:
        """Final gate: re-verify every top_k hit, then record exactly what returns.

        One transaction revalidates authority, re-checks the strategy snapshot
        (every targeted base, hits or not), re-checks each hit against the
        current rows (status/enabled/version, content digest, child identities,
        and the complete hard filters), and only then appends the query-log
        row and hit counters. The optional statistics share a savepoint, so
        a write fault rolls them all back without discarding verified hits.
        Stale hits are dropped without backfill; a changed effective strategy
        is a conflict. Authority, content reads and the outer transaction
        commit must still succeed before any content can return.
        """

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                # A rebinding between recall and return means the scores were
                # computed under a strategy some base no longer has: conflict,
                # never a silent partial result — even when that base
                # contributed no hits.
                await _assert_snapshot_strategy(session, project_id, snapshot)
                hits = tuple(await self._reviewed_hits(session, project_id, pending, metadata_filters)) if pending else ()
                # top_score and its provenance come from the same returned hit
                # so the logged score can never claim a kind the citation did
                # not carry.
                top_hit = max(hits, key=lambda hit: hit.citation.score) if hits else None
                try:
                    # Keep Project/Membership authority locks in the outer
                    # transaction. A failed metric must not release those
                    # locks or leave partially recorded history/counters.
                    async with session.begin_nested():
                        session.add(
                            KnowledgeQueryRow(
                                id=uuid4(),
                                project_id=project_id,
                                owner_user_id=str(owner_user_id),
                                knowledge_base_ids=[str(base_id) for base_id in sorted(snapshot.model_bindings)],
                                query=query,
                                source=source,
                                result_count=len(hits),
                                top_score=top_hit.citation.score if top_hit is not None else None,
                                top_score_kind=(top_hit.citation.score_kind if top_hit is not None else None),
                                strategy_version=KNOWLEDGE_STRATEGY_VERSION,
                            )
                        )
                        if hits:
                            segment_ids = [hit.citation.segment_id for hit in hits]
                            await session.execute(update(KnowledgeSegmentRow).where(KnowledgeSegmentRow.id.in_(segment_ids)).values(hit_count=KnowledgeSegmentRow.hit_count + 1))
                            hits_per_document: dict[UUID, int] = {}
                            for hit in hits:
                                document_id = hit.citation.document_id
                                hits_per_document[document_id] = hits_per_document.get(document_id, 0) + 1
                            for document_id, hit_increment in hits_per_document.items():
                                await session.execute(update(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document_id).values(hit_count=KnowledgeDocumentRow.hit_count + hit_increment))
                except SQLAlchemyError as error:
                    logger.warning("knowledge search statistics write failed: %s", type(error).__name__)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            logger.warning("knowledge final review failed", exc_info=True)
            raise _search_failed() from None
        return hits

    async def _reviewed_hits(
        self,
        session: AsyncSession,
        project_id: UUID,
        pending: list[KnowledgeSearchHit],
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
    ) -> list[KnowledgeSearchHit]:
        """Re-verify pending hits against current rows inside the final transaction."""

        segment_ids = [hit.citation.segment_id for hit in pending]
        # Identity read without scope filters: distinguishes "row changed"
        # from "row left retrieval scope" — both drop, but only rows read
        # here can be digest-compared.
        identity_rows = (
            await session.execute(
                select(
                    KnowledgeSegmentRow.id,
                    KnowledgeSegmentRow.content,
                    KnowledgeSegmentRow.document_version,
                    KnowledgeDocumentRow.version.label("current_document_version"),
                )
                .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
                .where(KnowledgeSegmentRow.project_id == project_id, KnowledgeSegmentRow.id.in_(segment_ids))
            )
        ).all()
        identity = {row.id: row for row in identity_rows}
        # The complete hard filters re-applied on the current rows (same
        # construction recall uses, minus the per-group embedding binding):
        # reassignment or a definition change never bumps the content
        # generation, so version checks alone cannot catch it.
        still_matching = set(
            (
                await session.scalars(
                    select(KnowledgeSegmentRow.id)
                    .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
                    .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
                    .where(
                        KnowledgeSegmentRow.id.in_(segment_ids),
                        *_current_scope_filters(project_id, metadata_filters),
                    )
                )
            ).all()
        )
        matched_child_ids = [child.child_id for hit in pending for child in hit.matched_children]
        child_rows: dict[UUID, tuple[UUID, int]] = {}
        if matched_child_ids:
            child_rows = {
                row.id: (row.knowledge_segment_id, row.document_version)
                for row in (
                    await session.execute(
                        select(
                            KnowledgeSegmentChildRow.id,
                            KnowledgeSegmentChildRow.knowledge_segment_id,
                            KnowledgeSegmentChildRow.document_version,
                        ).where(KnowledgeSegmentChildRow.id.in_(matched_child_ids))
                    )
                ).all()
            }
        kept: list[KnowledgeSearchHit] = []
        for hit in pending:
            segment_id = hit.citation.segment_id
            row = identity.get(segment_id)
            if row is None or segment_id not in still_matching:
                continue
            if row.document_version != hit.document_version or row.current_document_version != hit.document_version:
                continue
            if hashlib.sha256(row.content.encode("utf-8")).hexdigest() != hit.content_digest:
                continue
            # Child identities must still exist under this segment on the same
            # generation; a replaced child means the recall evidence no longer
            # describes stored rows.
            if any(child_rows.get(child.child_id) != (segment_id, hit.document_version) for child in hit.matched_children):
                continue
            kept.append(hit)
        return kept

    async def _searchable_groups(
        self,
        project_id: UUID,
        base_ids: tuple[UUID, ...] | None,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[list[_SearchGroup], dict[UUID, _BaseDefaults]]:
        """Group the project's searchable bases by (embedding, reranker) pair.

        Explicit base ids narrow to their active, configured subset; an
        explicitly empty selection searches nothing. An unresolvable bound model — missing,
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
            KnowledgeBaseRow.retrieval_mode,
            KnowledgeBaseRow.summary_index_enabled,
        ).where(
            KnowledgeBaseRow.project_id == project_id,
            KnowledgeBaseRow.status == "active",
            KnowledgeBaseRow.embedding_model_id.is_not(None),
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
                for base_id, embedding_model_id, reranker_model_id, default_top_k, default_score_threshold, retrieval_mode, summary_index_enabled in rows:
                    bases_by_pair.setdefault((embedding_model_id, reranker_model_id), []).append(base_id)
                    defaults[base_id] = _BaseDefaults(
                        top_k=default_top_k,
                        score_threshold=float(default_score_threshold),
                        retrieval_mode=retrieval_mode,
                        summary_index_enabled=summary_index_enabled,
                    )
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
        hybrid_base_ids: frozenset[UUID],
        query_vector: list[float],
        lexical_query: str | None,
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[list[_Candidate], int, int, int]:
        """Per-base recall: the semantic route, the lexical route, their merge.

        Each route caps at ``per_base_budget`` parents per base (Segment,
        Child and Summary sources deduplicated by max cosine first). A hybrid base with
        lexical tokens then merges its two routes by ``Σ 1/(60+rank)`` and
        keeps ``C`` parents; a semantic base keeps its semantic route as-is,
        so no base can consume another base's slots (§8.2). Returns the
        merged candidates plus the per-route counts (after the per-route
        caps) for diagnostics.
        """

        # All recall queries share the same short transaction. Authority is
        # revalidated before any query can load Segment content, so a
        # revocation during query embedding prevents that content from being
        # handed to the external Reranker. Matched children are read under
        # the same snapshot: they are recall evidence, never reconstructed
        # by scanning children after the fact.
        group_hybrid_ids = [base_id for base_id in base_ids if base_id in hybrid_base_ids]
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
                    per_base_budget=per_base_budget,
                    metadata_filters=metadata_filters,
                    session=session,
                )
                parents = await self._parent_child_candidates(
                    project_id=project_id,
                    embedding_model_id=embedding_model_id,
                    base_ids=base_ids,
                    query_vector=query_vector,
                    per_base_budget=per_base_budget,
                    metadata_filters=metadata_filters,
                    session=session,
                )
                summaries = await self._summary_candidates(
                    project_id=project_id,
                    embedding_model_id=embedding_model_id,
                    base_ids=base_ids,
                    query_vector=query_vector,
                    per_base_budget=per_base_budget,
                    metadata_filters=metadata_filters,
                    session=session,
                )
                semantic_by_segment: dict[UUID, _Candidate] = {}
                for candidate in [*general, *parents, *summaries]:
                    previous = semantic_by_segment.get(candidate.segment_id)
                    if previous is None or candidate.vector_score > previous.vector_score:
                        semantic_by_segment[candidate.segment_id] = candidate
                semantic_by_base: dict[UUID, list[_Candidate]] = {}
                for candidate in semantic_by_segment.values():
                    semantic_by_base.setdefault(candidate.knowledge_base_id, []).append(candidate)
                for pool in semantic_by_base.values():
                    pool.sort(key=_candidate_sort_key)
                    del pool[per_base_budget:]

                lexical_by_base: dict[UUID, list[tuple[_Candidate, float]]] = {}
                lexical_rollup_ids: set[UUID] = set()
                if group_hybrid_ids and lexical_query is not None:
                    # The lexical route reads only current-version rows; any
                    # in-scope row still on another lexical_version makes the
                    # route lie by omission, so it fails loudly instead.
                    await self._assert_lexical_current(session, project_id, group_hybrid_ids, metadata_filters)
                    lexical_general = await self._lexical_general_candidates(
                        project_id=project_id,
                        embedding_model_id=embedding_model_id,
                        base_ids=group_hybrid_ids,
                        query_vector=query_vector,
                        lexical_query=lexical_query,
                        per_base_budget=per_base_budget,
                        metadata_filters=metadata_filters,
                        session=session,
                    )
                    lexical_parents = await self._lexical_parent_candidates(
                        project_id=project_id,
                        embedding_model_id=embedding_model_id,
                        base_ids=group_hybrid_ids,
                        query_vector=query_vector,
                        lexical_query=lexical_query,
                        per_base_budget=per_base_budget,
                        metadata_filters=metadata_filters,
                        session=session,
                    )
                    lexical_rollup_ids = {candidate.segment_id for candidate, _ in lexical_parents}
                    for candidate, lexical_score in [*lexical_general, *lexical_parents]:
                        lexical_by_base.setdefault(candidate.knowledge_base_id, []).append((candidate, lexical_score))
                    for lexical_pool in lexical_by_base.values():
                        lexical_pool.sort(key=lambda entry: (-entry[1], *_candidate_sort_key(entry[0])))
                        del lexical_pool[per_base_budget:]
                    lexical_ids = [candidate.segment_id for pool in lexical_by_base.values() for candidate, _ in pool]
                    if lexical_ids:
                        # Lexical-only parents may sit below every semantic
                        # source's cap. Their threshold still needs the real
                        # maximum cosine, including an enabled summary.
                        lexical_summaries = await self._summary_candidates(
                            project_id=project_id,
                            embedding_model_id=embedding_model_id,
                            base_ids=group_hybrid_ids,
                            query_vector=query_vector,
                            per_base_budget=per_base_budget,
                            metadata_filters=metadata_filters,
                            session=session,
                            segment_ids=lexical_ids,
                        )
                        summaries_by_segment = {candidate.segment_id: candidate for candidate in lexical_summaries}
                        for base_id, pool in lexical_by_base.items():
                            lexical_by_base[base_id] = [
                                (summaries_by_segment[candidate.segment_id], score) if candidate.segment_id in summaries_by_segment and summaries_by_segment[candidate.segment_id].vector_score > candidate.vector_score else (candidate, score)
                                for candidate, score in pool
                            ]

                merged: list[_Candidate] = []
                for base_id in {*semantic_by_base, *lexical_by_base}:
                    semantic_pool = semantic_by_base.get(base_id, [])
                    lexical_pool = lexical_by_base.get(base_id, [])
                    if not lexical_pool:
                        merged.extend(semantic_pool)
                        continue
                    merged.extend(_merge_recall_routes(semantic_pool, lexical_pool, per_base_budget))
                merged.sort(key=_candidate_sort_key)

                rollup_parent_ids = {candidate.segment_id for candidate in parents} | lexical_rollup_ids
                surviving_parent_ids = [candidate.segment_id for candidate in merged if candidate.segment_id in rollup_parent_ids]
                if surviving_parent_ids:
                    children_by_parent = await self._matched_children_by_parent(
                        session,
                        parent_ids=surviving_parent_ids,
                        query_vector=query_vector,
                    )
                    if lexical_rollup_ids and lexical_query is not None:
                        lexical_children = await self._lexical_matched_children(
                            session,
                            parent_ids=[parent_id for parent_id in surviving_parent_ids if parent_id in lexical_rollup_ids],
                            lexical_query=lexical_query,
                        )
                        # Lexical match evidence first — it is the scarcer,
                        # more explanatory signal — then the semantic top,
                        # inside the same per-hit projection cap.
                        children_by_parent = {parent_id: (*lexical_children.get(parent_id, ()), *children_by_parent.get(parent_id, ()))[:KNOWLEDGE_MAX_MATCHED_CHILDREN] for parent_id in {*children_by_parent, *lexical_children}}
                    merged = [replace(candidate, matched_children=children_by_parent.get(candidate.segment_id, ())) if candidate.segment_id in rollup_parent_ids else candidate for candidate in merged]
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            logger.warning("knowledge recall failed", exc_info=True)
            raise _search_failed() from None
        semantic_count = sum(len(pool) for pool in semantic_by_base.values())
        lexical_count = sum(len(pool) for pool in lexical_by_base.values())
        return merged, semantic_count, lexical_count, len(summaries)

    async def _matched_children_by_parent(
        self,
        session: AsyncSession,
        *,
        parent_ids: list[UUID],
        query_vector: list[float],
    ) -> dict[UUID, tuple[KnowledgeMatchedChild, ...]]:
        """Top recalled children per parent, best score first (semantic route).

        At most ``KNOWLEDGE_MAX_MATCHED_CHILDREN`` per parent are projected;
        ties break on position then id, mirroring the recall ordering.
        """

        child_score = 1 - KnowledgeSegmentChildRow.embedding.cosine_distance(query_vector)
        inner = (
            select(
                KnowledgeSegmentChildRow.id,
                KnowledgeSegmentChildRow.knowledge_segment_id,
                KnowledgeSegmentChildRow.position,
                child_score.label("child_score"),
                func.row_number()
                .over(
                    partition_by=KnowledgeSegmentChildRow.knowledge_segment_id,
                    order_by=(
                        child_score.desc(),
                        KnowledgeSegmentChildRow.position.asc(),
                        KnowledgeSegmentChildRow.id.asc(),
                    ),
                )
                .label("recall_rank"),
            )
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentChildRow.knowledge_segment_id)
            .where(
                KnowledgeSegmentChildRow.knowledge_segment_id.in_(parent_ids),
                KnowledgeSegmentChildRow.document_version == KnowledgeSegmentRow.document_version,
            )
            .subquery()
        )
        rows = (await session.execute(select(inner).where(inner.c.recall_rank <= KNOWLEDGE_MAX_MATCHED_CHILDREN).order_by(inner.c.knowledge_segment_id, inner.c.recall_rank))).all()
        children: dict[UUID, list[KnowledgeMatchedChild]] = {}
        for row in rows:
            children.setdefault(row.knowledge_segment_id, []).append(
                KnowledgeMatchedChild(
                    child_id=row.id,
                    position=row.position,
                    route="semantic",
                    score=float(row.child_score),
                )
            )
        return {parent_id: tuple(items) for parent_id, items in children.items()}

    async def _assert_lexical_current(
        self,
        session: AsyncSession,
        project_id: UUID,
        hybrid_base_ids: list[UUID],
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
    ) -> None:
        """Every in-scope row of a hybrid target must be on the current
        lexical_version — parents included, since fusion scores them all.

        A mismatch fails loudly (reparse the documents, or search semantic);
        the route never skips rows or backfills derivations at read time.
        """

        scope = (
            KnowledgeBaseRow.id.in_(hybrid_base_ids),
            *_current_scope_filters(project_id, metadata_filters),
        )
        stale_segment = (
            select(literal(1))
            .select_from(KnowledgeSegmentRow)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(*scope, KnowledgeSegmentRow.lexical_version != KNOWLEDGE_LEXICAL_VERSION)
            .limit(1)
        )
        stale_child = (
            select(literal(1))
            .select_from(KnowledgeSegmentChildRow)
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentChildRow.knowledge_segment_id)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(*scope, KnowledgeSegmentChildRow.lexical_version != KNOWLEDGE_LEXICAL_VERSION)
            .limit(1)
        )
        if await session.scalar(stale_segment) is not None or await session.scalar(stale_child) is not None:
            raise _lexical_stale_conflict()

    async def _lexical_general_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        query_vector: list[float],
        lexical_query: str,
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession,
    ) -> list[tuple[_Candidate, float]]:
        """Lexical recall over general-mode segments, capped per base.

        The row's real cosine travels along (``vector_score``), because the
        native threshold and the reranker act on native evidence even for
        candidates only the lexical route surfaced.
        """

        tsquery = func.to_tsquery("simple", lexical_query)
        rank_cd = func.ts_rank_cd(KnowledgeSegmentRow.lexical_tsv, tsquery, 2)
        vector_score = (1 - KnowledgeSegmentRow.embedding.cosine_distance(query_vector)).label("vector_score")
        per_base_rank = (
            func.row_number()
            .over(
                partition_by=KnowledgeBaseRow.id,
                order_by=(
                    rank_cd.desc(),
                    KnowledgeDocumentRow.id.asc(),
                    KnowledgeSegmentRow.position.asc(),
                    KnowledgeSegmentRow.id.asc(),
                ),
            )
            .label("per_base_rank")
        )
        inner = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                vector_score,
                rank_cd.label("lexical_score"),
                per_base_rank,
            )
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(
                *self._candidate_filters(project_id, embedding_model_id, base_ids, metadata_filters),
                KnowledgeSegmentRow.embedding.is_not(None),
                KnowledgeSegmentRow.lexical_tsv.op("@@")(tsquery),
            )
            .subquery()
        )
        statement = (
            select(inner)
            .where(inner.c.per_base_rank <= per_base_budget)
            .order_by(
                inner.c.lexical_score.desc(),
                inner.c.knowledge_base_id.asc(),
                inner.c.document_id.asc(),
                inner.c.position.asc(),
                inner.c.id.asc(),
            )
        )
        rows = (await session.execute(statement)).all()
        return [(self._candidate_from_row(row), float(row.lexical_score)) for row in rows]

    async def _lexical_parent_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        query_vector: list[float],
        lexical_query: str,
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession,
    ) -> list[tuple[_Candidate, float]]:
        """parent_child lexical recall: child hits roll up to their parent.

        The parent's lexical score is its best child ``ts_rank_cd``; its
        ``vector_score`` is the max cosine over ALL current children — never
        the lexically matched child alone and never a NULL parent vector —
        so native thresholds see the same evidence the semantic route would.
        """

        tsquery = func.to_tsquery("simple", lexical_query)
        child_rank_cd = func.ts_rank_cd(KnowledgeSegmentChildRow.lexical_tsv, tsquery, 2)
        hits = (
            select(
                KnowledgeSegmentChildRow.knowledge_segment_id.label("segment_id"),
                func.max(child_rank_cd).label("lexical_score"),
            )
            .select_from(KnowledgeSegmentChildRow)
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentChildRow.knowledge_segment_id)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(
                *self._candidate_filters(project_id, embedding_model_id, base_ids, metadata_filters),
                KnowledgeSegmentChildRow.lexical_tsv.op("@@")(tsquery),
            )
            .group_by(KnowledgeSegmentChildRow.knowledge_segment_id)
            .subquery()
        )
        all_children_cosine = func.max(1 - KnowledgeSegmentChildRow.embedding.cosine_distance(query_vector)).label("vector_score")
        per_base_rank = (
            func.row_number()
            .over(
                partition_by=KnowledgeBaseRow.id,
                order_by=(
                    hits.c.lexical_score.desc(),
                    KnowledgeDocumentRow.id.asc(),
                    KnowledgeSegmentRow.position.asc(),
                    KnowledgeSegmentRow.id.asc(),
                ),
            )
            .label("per_base_rank")
        )
        inner = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                all_children_cosine,
                hits.c.lexical_score,
                per_base_rank,
            )
            .select_from(hits)
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == hits.c.segment_id)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .join(KnowledgeSegmentChildRow, KnowledgeSegmentChildRow.knowledge_segment_id == KnowledgeSegmentRow.id)
            .group_by(KnowledgeSegmentRow.id, KnowledgeDocumentRow.id, KnowledgeBaseRow.id, hits.c.lexical_score)
            .subquery()
        )
        statement = (
            select(inner)
            .where(inner.c.per_base_rank <= per_base_budget)
            .order_by(
                inner.c.lexical_score.desc(),
                inner.c.knowledge_base_id.asc(),
                inner.c.document_id.asc(),
                inner.c.position.asc(),
                inner.c.id.asc(),
            )
        )
        rows = (await session.execute(statement)).all()
        return [(self._candidate_from_row(row, matched_via="child"), float(row.lexical_score)) for row in rows]

    def _candidate_from_row(self, row: Any, *, matched_via: KnowledgeMatchedVia = "segment") -> _Candidate:
        return _Candidate(
            segment_id=row.id,
            position=row.position,
            content=row.content,
            source_position=dict(row.source_position),
            document_id=row.document_id,
            document_name=row.document_name,
            document_version=row.document_version,
            knowledge_base_id=row.knowledge_base_id,
            knowledge_base_name=row.knowledge_base_name,
            vector_score=float(row.vector_score),
            matched_via=matched_via,
        )

    async def _lexical_matched_children(
        self,
        session: AsyncSession,
        *,
        parent_ids: list[UUID],
        lexical_query: str,
    ) -> dict[UUID, tuple[KnowledgeMatchedChild, ...]]:
        """The really lexically matched children per surviving rollup parent."""

        tsquery = func.to_tsquery("simple", lexical_query)
        child_rank_cd = func.ts_rank_cd(KnowledgeSegmentChildRow.lexical_tsv, tsquery, 2)
        inner = (
            select(
                KnowledgeSegmentChildRow.id,
                KnowledgeSegmentChildRow.knowledge_segment_id,
                KnowledgeSegmentChildRow.position,
                child_rank_cd.label("lexical_score"),
                func.row_number()
                .over(
                    partition_by=KnowledgeSegmentChildRow.knowledge_segment_id,
                    order_by=(
                        child_rank_cd.desc(),
                        KnowledgeSegmentChildRow.position.asc(),
                        KnowledgeSegmentChildRow.id.asc(),
                    ),
                )
                .label("recall_rank"),
            )
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentChildRow.knowledge_segment_id)
            .where(
                KnowledgeSegmentChildRow.knowledge_segment_id.in_(parent_ids),
                KnowledgeSegmentChildRow.document_version == KnowledgeSegmentRow.document_version,
                KnowledgeSegmentChildRow.lexical_tsv.op("@@")(tsquery),
            )
            .subquery()
        )
        rows = (await session.execute(select(inner).where(inner.c.recall_rank <= KNOWLEDGE_MAX_MATCHED_CHILDREN).order_by(inner.c.knowledge_segment_id, inner.c.recall_rank))).all()
        children: dict[UUID, list[KnowledgeMatchedChild]] = {}
        for row in rows:
            children.setdefault(row.knowledge_segment_id, []).append(
                KnowledgeMatchedChild(
                    child_id=row.id,
                    position=row.position,
                    route="lexical",
                    score=float(row.lexical_score),
                )
            )
        return {parent_id: tuple(items) for parent_id, items in children.items()}

    async def _final_lexical_ranks(
        self,
        *,
        project_id: UUID,
        segment_ids: list[UUID],
        lexical_query: str,
    ) -> dict[UUID, int]:
        """Global shared-place lexical ranking of every shortlisted parent.

        All shortlisted parents — semantic bases included — score against the
        same query; only positive scores rank. A row on another
        lexical_version fails loudly; a row deleted meanwhile simply has no
        lexical evidence (the final review will drop its hit anyway).
        """

        tsquery = func.to_tsquery("simple", lexical_query)
        statement = select(
            KnowledgeSegmentRow.id,
            KnowledgeSegmentRow.lexical_version,
            func.ts_rank_cd(KnowledgeSegmentRow.lexical_tsv, tsquery, 2).label("lexical_score"),
        ).where(KnowledgeSegmentRow.project_id == project_id, KnowledgeSegmentRow.id.in_(segment_ids))
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(statement)).all()
        except SQLAlchemyError:
            logger.warning("knowledge lexical scoring failed", exc_info=True)
            raise _search_failed() from None
        positive: list[tuple[float, UUID]] = []
        for row in rows:
            if row.lexical_version != KNOWLEDGE_LEXICAL_VERSION:
                raise _lexical_stale_conflict()
            score = float(row.lexical_score)
            if score > 0:
                positive.append((score, row.id))
        positive.sort(key=lambda entry: (-entry[0], entry[1]))
        ranks: dict[UUID, int] = {}
        rank = 0
        previous: float | None = None
        for index, (score, segment_id) in enumerate(positive, start=1):
            if previous is None or score != previous:
                rank = index
                previous = score
            ranks[segment_id] = rank
        return ranks

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

    async def _revalidate_dispatch(
        self,
        *,
        project_id: UUID,
        authority: KnowledgeProjectAuthority | None,
        snapshot: _SearchSnapshot,
    ) -> None:
        """Pre-dispatch guard: authority plus the strategy snapshot.

        Runs before every provider batch (and the client's internal retry),
        so neither a revoked caller nor a rebound base can cause further
        provider spend under a stale strategy.
        """

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                await _assert_snapshot_strategy(session, project_id, snapshot)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            logger.warning("knowledge search dispatch guard failed", exc_info=True)
            raise _search_failed() from None

    def _candidate_filters(
        self,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
    ) -> tuple[Any, ...]:
        return (
            KnowledgeBaseRow.id.in_(base_ids),
            # A base rebound to another embedding model between group load and
            # recall drops out here: its vectors no longer match this query
            # embedding's dimension/space.
            KnowledgeBaseRow.embedding_model_id == embedding_model_id,
            *_current_scope_filters(project_id, metadata_filters),
        )

    async def _general_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        query_vector: list[float],
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession | None = None,
    ) -> list[_Candidate]:
        """Exact cosine recall over segments that carry their own vectors.

        A window ranked per base (score, then stable identity) caps every
        base at ``per_base_budget`` rows inside SQL, so one base's rows can
        never crowd another base out of the recall result.
        """

        distance = KnowledgeSegmentRow.embedding.cosine_distance(query_vector)
        vector_score = (1 - distance).label("vector_score")
        per_base_rank = (
            func.row_number()
            .over(
                partition_by=KnowledgeBaseRow.id,
                order_by=(
                    (1 - distance).desc(),
                    KnowledgeDocumentRow.id.asc(),
                    KnowledgeSegmentRow.position.asc(),
                    KnowledgeSegmentRow.id.asc(),
                ),
            )
            .label("per_base_rank")
        )
        inner = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                vector_score,
                per_base_rank,
            )
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(
                *self._candidate_filters(project_id, embedding_model_id, base_ids, metadata_filters),
                # parent_child parents store NULL embeddings and are recalled
                # through their children instead.
                KnowledgeSegmentRow.embedding.is_not(None),
            )
            .subquery()
        )
        statement = (
            select(inner)
            .where(inner.c.per_base_rank <= per_base_budget)
            .order_by(
                inner.c.vector_score.desc(),
                inner.c.knowledge_base_id.asc(),
                inner.c.document_id.asc(),
                inner.c.position.asc(),
                inner.c.id.asc(),
            )
        )
        return await self._execute_recall(statement, session=session)

    async def _summary_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        query_vector: list[float],
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession,
        segment_ids: list[UUID] | None = None,
    ) -> list[_Candidate]:
        """Summary vectors recall real Segments under the same scope and cap."""

        vector_score = 1 - KnowledgeSegmentSummaryRow.embedding.cosine_distance(query_vector)
        inner = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                vector_score.label("vector_score"),
                func.row_number()
                .over(
                    partition_by=KnowledgeBaseRow.id,
                    order_by=(vector_score.desc(), KnowledgeDocumentRow.id.asc(), KnowledgeSegmentRow.position.asc(), KnowledgeSegmentRow.id.asc()),
                )
                .label("per_base_rank"),
            )
            .select_from(KnowledgeSegmentSummaryRow)
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentSummaryRow.knowledge_segment_id)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(
                *self._candidate_filters(project_id, embedding_model_id, base_ids, metadata_filters),
                KnowledgeBaseRow.summary_index_enabled.is_(True),
                KnowledgeSegmentSummaryRow.project_id == project_id,
                KnowledgeSegmentSummaryRow.knowledge_base_id == KnowledgeBaseRow.id,
                KnowledgeSegmentSummaryRow.knowledge_document_id == KnowledgeDocumentRow.id,
                KnowledgeSegmentSummaryRow.document_version == KnowledgeSegmentRow.document_version,
                *(() if segment_ids is None else (KnowledgeSegmentRow.id.in_(segment_ids),)),
            )
            .subquery()
        )
        statement = select(inner).where(inner.c.per_base_rank <= per_base_budget).order_by(inner.c.vector_score.desc(), inner.c.knowledge_base_id.asc(), inner.c.document_id.asc(), inner.c.position.asc(), inner.c.id.asc())
        return await self._execute_recall(statement, session=session, matched_via="summary")

    async def _parent_child_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        base_ids: list[UUID],
        query_vector: list[float],
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession | None = None,
    ) -> list[_Candidate]:
        """Child-chunk recall rolled up to parents before the reranker.

        The best child score inside each parent becomes the parent's recall
        score (the plan's 回卷 rule) before any budget applies, so a parent
        never appears twice and a parent's many children can never consume
        the base's ``per_base_budget`` parent slots.
        """

        child_distance = KnowledgeSegmentChildRow.embedding.cosine_distance(query_vector)
        vector_score = func.max(1 - child_distance).label("vector_score")
        per_base_rank = (
            func.row_number()
            .over(
                partition_by=KnowledgeBaseRow.id,
                order_by=(
                    func.max(1 - child_distance).desc(),
                    KnowledgeDocumentRow.id.asc(),
                    KnowledgeSegmentRow.position.asc(),
                    KnowledgeSegmentRow.id.asc(),
                ),
            )
            .label("per_base_rank")
        )
        inner = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                vector_score,
                per_base_rank,
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
            .subquery()
        )
        statement = (
            select(inner)
            .where(inner.c.per_base_rank <= per_base_budget)
            .order_by(
                inner.c.vector_score.desc(),
                inner.c.knowledge_base_id.asc(),
                inner.c.document_id.asc(),
                inner.c.position.asc(),
                inner.c.id.asc(),
            )
        )
        return await self._execute_recall(statement, session=session, matched_via="child")

    async def _execute_recall(
        self,
        statement: Any,
        *,
        session: AsyncSession | None = None,
        matched_via: KnowledgeMatchedVia = "segment",
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
                document_version=row.document_version,
                knowledge_base_id=row.knowledge_base_id,
                knowledge_base_name=row.knowledge_base_name,
                vector_score=float(row.vector_score),
                matched_via=matched_via,
            )
            for row in rows
        ]
