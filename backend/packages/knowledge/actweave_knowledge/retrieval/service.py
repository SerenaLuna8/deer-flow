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
lexical tokens (the first 128 deduplicated tokens with a hybrid target — a
longer query is truncated and reported in ``debug``, never rejected; none at
all without a hybrid target), scored by ``ts_rank_cd(..., 2)`` — general mode
on segment rows, parent_child on child rows rolled up to their parent's best
score. The two routes merge per base by ``Σ 1/(60+rank)`` before the cap
``C``, and every lexical-only candidate still gets its real cosine
(parent_child: the max over all current children). Per-base thresholds act
on native scores only; in a rerank-free group a candidate the lexical route
recalled is exempt from the cosine threshold, because an exact token match is
evidence cosine cannot see. Rows whose ``lexical_version`` does not match the
fixed derivation version fail the search loudly — the lexical route never
silently skips or backfills them at read time (``relex_document`` is the
explicit repair).

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
import time
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import cast, func, literal, select, text, true, update
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_LEXICAL_VERSION,
    KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS,
    KNOWLEDGE_MAX_MATCHED_CHILDREN,
    KNOWLEDGE_MAX_TOP_K,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_SEARCH_FAILED,
    KNOWLEDGE_STRATEGY_VERSION,
    KnowledgeCitation,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeHitDiagnostics,
    KnowledgeMatchedChild,
    KnowledgeMatchedVia,
    KnowledgeMetadataFilter,
    KnowledgeModelPort,
    KnowledgeQueryView,
    KnowledgeRecallRoute,
    KnowledgeRerankMaterial,
    KnowledgeRouteCounts,
    KnowledgeScoreKind,
    KnowledgeSearchDiagnostics,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeSearchTimings,
)
from ..models.client import KnowledgeModelClient
from ..persistence.derivations import stored_model_text
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeQueryRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeSegmentSummaryRow,
)
from .candidates import (
    BaseDefaults,
    Candidate,
    RankedCandidate,
    RecallOutcome,
    SearchGroup,
    SearchSnapshot,
    ValidatedSearch,
    effective_defaults,
)
from .filters import current_scope_filters, validated_metadata_filters
from .fusion import (
    apply_relative_cutoffs,
    calculate_candidate_k,
    calculate_per_base_budget,
    candidate_sort_key,
    merge_recall_routes,
    rank_fused,
    rerank_input,
    rerank_input_cap,
    shared_place_ranks,
    stable_sort_key,
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

_LEXICAL_ROUTE: frozenset[KnowledgeRecallRoute] = frozenset({"lexical"})

# parent_child recall takes this many nearest children per base for every
# parent slot before rolling them up to parents (see _parent_child_candidates).
PARENT_CHILD_WINDOW_FACTOR = 8


def _base_targets(base_ids: list[UUID]) -> Any:
    """``unnest(:base_ids) AS targets(base_id)`` — one lateral branch per base."""

    return select(func.unnest(cast(array(base_ids), ARRAY(PG_UUID(as_uuid=True)))).label("base_id")).subquery("targets")


def _typed_distance(column: Any, dimension: int, query_vector: list[float]) -> Any:
    """``column::vector(D) <=> :query`` — the exact expression the per-dimension
    HNSW partial indexes are built on; a dimension without an index runs the
    same expression as a plain sort."""

    return cast(column, Vector(dimension)).cosine_distance(query_vector)


async def _prepare_vector_scan(session: AsyncSession) -> None:
    """Let filtered HNSW scans keep walking until each branch's LIMIT is met.

    Without the iterative scan a heavily filtered branch returns fewer rows
    than its budget (the default 40 candidates minus filtered-out rows). The
    setting is transaction-local; pgvector < 0.8 lacks it, so the failure is
    contained in a savepoint and the scan falls back to the classic behavior.
    """

    try:
        async with session.begin_nested():
            await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    except SQLAlchemyError:
        logger.warning("pgvector iterative scan unavailable; filtered HNSW branches may under-fill")


__all__ = [
    "DEFAULT_SCORE_THRESHOLD",
    "DEFAULT_TOP_K",
    "MAX_QUERY_CHARS",
    "MAX_QUERY_PAGE_SIZE",
    "MAX_TOP_K",
    "SNIPPET_MAX_CHARS",
    "KnowledgeSearchService",
    "calculate_candidate_k",
    "calculate_per_base_budget",
    "rerank_input_cap",
]


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
        "检索范围内存在词法索引版本不一致的内容，请对相关知识库执行「重建词法索引」（relex）后重试，或改用 semantic 检索",
    )


def _validated_search(request: KnowledgeSearchRequest) -> ValidatedSearch:
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
    relative = request.relative_score_cutoff
    if relative is not None:
        if type(relative) not in (int, float) or not 0 < float(relative) <= 1:
            raise _invalid("relative_score_cutoff 必须在 (0, 1] 之间")
        relative = float(relative)
    return ValidatedSearch(
        query=query,
        top_k=top_k,
        score_threshold=threshold,
        metadata_filters=validated_metadata_filters(request.metadata_filters),
        retrieval_mode=request.retrieval_mode,
        relative_score_cutoff=relative,
    )


async def _assert_snapshot_strategy(
    session: AsyncSession,
    project_id: UUID,
    snapshot: SearchSnapshot,
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
                KnowledgeBaseRow.default_relative_cutoff,
            ).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id.in_(snapshot.model_bindings))
        )
    ).all()
    current_defaults = dict(snapshot.effective_defaults)
    for row in rows:
        actual = effective_defaults(
            BaseDefaults(
                top_k=row.default_top_k,
                score_threshold=float(row.default_score_threshold),
                retrieval_mode=row.retrieval_mode,
                summary_index_enabled=row.summary_index_enabled,
                relative_cutoff=None if row.default_relative_cutoff is None else float(row.default_relative_cutoff),
            ),
            snapshot.overrides,
        )
        expected = snapshot.effective_defaults[row.id]
        if (
            snapshot.model_bindings[row.id] != (row.embedding_model_id, row.reranker_model_id)
            or expected.score_threshold != actual.score_threshold
            or expected.retrieval_mode != actual.retrieval_mode
            or expected.summary_index_enabled != actual.summary_index_enabled
            or expected.relative_cutoff != actual.relative_cutoff
        ):
            raise KnowledgeError(KNOWLEDGE_CONFLICT, "检索策略已变更，请重新检索")
        current_defaults[row.id] = actual
    # top_k is one global limit: a smaller base default changing without
    # changing the maximum cannot alter this search's budget or result cap.
    # Missing bases keep their snapshot settings; deletion only drops hits.
    if max(item.top_k for item in current_defaults.values()) != max(item.top_k for item in snapshot.effective_defaults.values()):
        raise KnowledgeError(KNOWLEDGE_CONFLICT, "检索策略已变更，请重新检索")


def _empty_scope_diagnostics(validated: ValidatedSearch) -> KnowledgeSearchDiagnostics:
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

        defaults = {base_id: effective_defaults(base_defaults, validated) for base_id, base_defaults in defaults.items()}
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
        lexical_query_token_count = 0
        lexical_query_truncated = False
        if hybrid_base_ids:
            query_tokens = lexical_query_input(validated.query)
            if len(query_tokens) > KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS:
                # A long natural-language question must not fail the search:
                # keep the leading tokens (scan order), leave the vector route
                # untouched, and say so in the diagnostics.
                query_tokens = query_tokens[:KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS]
                lexical_query_truncated = True
            lexical_query_token_count = len(query_tokens)
            # Zero tokens leave the lexical route empty; the vector route
            # still runs.
            lexical_query = " | ".join(query_tokens) if query_tokens else None
        # The effective strategy snapshot of this search: every targeted
        # base's model bindings and resolved settings, frozen by group load.
        snapshot = SearchSnapshot(
            model_bindings={base_id: (group.embedding.model_id, group.rerank.model_id if group.rerank is not None else None) for group in groups for base_id in group.base_ids},
            effective_defaults=defaults,
            overrides=validated,
        )

        # Groups sharing an embedding model reuse one query embedding per
        # search while keeping their own per-base recall budgets.
        query_vectors: dict[UUID, list[float]] = {}
        ranked: list[RankedCandidate] = []
        semantic_candidates = 0
        lexical_candidates = 0
        summary_candidates = 0
        threshold_filtered = 0
        relative_filtered = 0
        lexical_threshold_exempt = 0
        query_embedding_cache_hits = 0
        query_embedding_cache_misses = 0
        timings = {"query_embedding_ms": 0.0, "recall_ms": 0.0, "rerank_ms": 0.0, "final_validation_ms": 0.0}

        # Stage 1 — query vectors. Group loading has just revalidated the
        # caller inside its own transaction, so the query embedding dispatches
        # on that authority; one vector per distinct embedding model.
        for group in groups:
            embedding = group.embedding
            if embedding.model_id in query_vectors:
                continue
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
                    vector = (await self._client.embed(embedding, [validated.query], kind="query"))[0]
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

        # Stage 2 — one recall transaction for every group: authority and the
        # strategy snapshot are revalidated once, every group's routes run
        # under the same database snapshot, and the lexical evidence fusion
        # may need later is scored here too. Cache hits skip Provider
        # dispatch only; this transaction-bound check always runs.
        started = time.monotonic()
        recall = await self._recall_all_groups(
            project_id=request.project_id,
            groups=groups,
            hybrid_base_ids=hybrid_base_ids,
            query_vectors=query_vectors,
            lexical_query=lexical_query,
            per_base_budget=per_base_budget,
            metadata_filters=validated.metadata_filters,
            snapshot=snapshot,
            authority=authority,
        )
        timings["recall_ms"] += (time.monotonic() - started) * 1000.0
        semantic_candidates = recall.semantic_count
        lexical_candidates = recall.lexical_count
        summary_candidates = recall.summary_count

        # Stage 3 — native scoring per group. A reranked group revalidates
        # authority and the strategy once, immediately before Segment text
        # leaves for the Provider; batches inside one call share that check.
        for group in groups:
            embedding = group.embedding
            candidates = recall.candidates_by_group.get(id(group), [])
            if not candidates:
                continue
            group_ranked: list[RankedCandidate] = []
            if group.rerank is None:
                # Rerank-free group: the native score stays the raw cosine
                # similarity in [-1,1]; a 0 threshold filters nothing. The
                # score domain is the embedding model, not "the base".
                cosine_domain = f"cosine:{embedding.model_id}"
                for candidate in candidates:
                    group_ranked.append(
                        RankedCandidate(
                            final_score=candidate.vector_score,
                            local_score_kind="cosine",
                            score_domain=cosine_domain,
                            candidate=candidate,
                        )
                    )
            else:
                await self._revalidate_dispatch(
                    project_id=request.project_id,
                    authority=authority,
                    snapshot=snapshot,
                )
                started = time.monotonic()
                # Recall may keep C parents per base; the reranker sees only
                # the best places of each base's own recall order, within the
                # group budget, and never fewer than top_k per base. Every
                # candidate that is sent gets scored, so per-base thresholds
                # still act before any top_k truncation.
                candidates = rerank_input(candidates, top_k=top_k)
                try:
                    scores = await self._client.rerank(
                        group.rerank,
                        validated.query,
                        [candidate.index_text for candidate in candidates],
                        top_n=len(candidates),
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
                        RankedCandidate(
                            final_score=score.score,
                            local_score_kind="rerank",
                            score_domain=rerank_domain,
                            candidate=candidates[score.index],
                        )
                    )
            # Per-base thresholds act on native scores only — never on the
            # fused ranking score (§8.3). In a rerank-free group the native
            # score is cosine, which says nothing about an exact token match:
            # a candidate the lexical route recalled keeps its place so hybrid
            # can do the one thing it exists for. A reranker has already judged
            # the text, so its threshold applies to every candidate.
            surviving: list[RankedCandidate] = []
            for item in group_ranked:
                threshold = defaults[item.candidate.knowledge_base_id].score_threshold
                if threshold > 0 and item.final_score < threshold:
                    if item.local_score_kind == "cosine" and "lexical" in item.candidate.recall_routes:
                        lexical_threshold_exempt += 1
                    else:
                        threshold_filtered += 1
                        continue
                surviving.append(item)
            # The relative cutoff follows each base's own best native score,
            # so it is applied per base after the absolute threshold.
            surviving, relative_dropped = apply_relative_cutoffs(surviving, defaults)
            relative_filtered += relative_dropped
            ranked.extend(surviving)

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
                # Every shortlisted parent — semantic bases included — was
                # scored by the same lexical query inside the recall
                # transaction; positive scores build one global shared-place
                # ranking over the parents that survived the thresholds
                # (§8.3 branch 3).
                lexical_ranks = shared_place_ranks({item.candidate.segment_id: recall.lexical_scores[item.candidate.segment_id] for item in ranked if recall.lexical_scores.get(item.candidate.segment_id, 0.0) > 0})
            ordered = rank_fused(ranked, lexical_ranks)
        else:
            ranked.sort(key=stable_sort_key)
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
                    model_text=candidate.index_text,
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
                    relative_filtered=relative_filtered,
                    lexical_threshold_exempt=lexical_threshold_exempt,
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
                lexical_query_token_count=lexical_query_token_count,
                lexical_query_truncated=lexical_query_truncated,
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
        snapshot: SearchSnapshot,
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
                        *current_scope_filters(project_id, metadata_filters),
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
    ) -> tuple[list[SearchGroup], dict[UUID, BaseDefaults]]:
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
            KnowledgeBaseRow.default_relative_cutoff,
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
                defaults: dict[UUID, BaseDefaults] = {}
                for base_id, embedding_model_id, reranker_model_id, default_top_k, default_score_threshold, retrieval_mode, summary_index_enabled, default_relative_cutoff in rows:
                    bases_by_pair.setdefault((embedding_model_id, reranker_model_id), []).append(base_id)
                    defaults[base_id] = BaseDefaults(
                        top_k=default_top_k,
                        score_threshold=float(default_score_threshold),
                        retrieval_mode=retrieval_mode,
                        summary_index_enabled=summary_index_enabled,
                        relative_cutoff=None if default_relative_cutoff is None else float(default_relative_cutoff),
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
            SearchGroup(
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

    async def _recall_all_groups(
        self,
        *,
        project_id: UUID,
        groups: list[SearchGroup],
        hybrid_base_ids: frozenset[UUID],
        query_vectors: dict[UUID, list[float]],
        lexical_query: str | None,
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        snapshot: SearchSnapshot,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> RecallOutcome:
        """The single recall transaction of a search.

        Authority is revalidated and the strategy snapshot re-checked once,
        before any query can load Segment content — so a revocation or
        rebinding during query embedding stops the search before text
        reaches the Reranker. Every group's routes then run under this one
        database snapshot, and the lexical evidence fusion may need is scored
        here for every recalled parent (a stale ``lexical_version`` anywhere
        in that set is a conflict, never a silent zero).
        """

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                await _assert_snapshot_strategy(session, project_id, snapshot)
                await _prepare_vector_scan(session)
                candidates_by_group: dict[int, list[Candidate]] = {}
                semantic_count = lexical_count = summary_count = 0
                for group in groups:
                    candidates, group_semantic, group_lexical, group_summary = await self._recall_group(
                        session,
                        project_id=project_id,
                        embedding_model_id=group.embedding.model_id,
                        dimension=group.embedding.dimension,
                        base_ids=group.base_ids,
                        hybrid_base_ids=hybrid_base_ids,
                        query_vector=query_vectors[group.embedding.model_id],
                        lexical_query=lexical_query,
                        per_base_budget=per_base_budget,
                        metadata_filters=metadata_filters,
                    )
                    candidates_by_group[id(group)] = candidates
                    semantic_count += group_semantic
                    lexical_count += group_lexical
                    summary_count += group_summary
                lexical_scores: dict[UUID, float] = {}
                recalled_ids = [candidate.segment_id for candidates in candidates_by_group.values() for candidate in candidates]
                if lexical_query is not None and recalled_ids:
                    lexical_scores = await self._lexical_scores(session, project_id=project_id, segment_ids=recalled_ids, lexical_query=lexical_query)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            logger.warning("knowledge recall failed", exc_info=True)
            raise _search_failed() from None
        return RecallOutcome(
            candidates_by_group=candidates_by_group,
            lexical_scores=lexical_scores,
            semantic_count=semantic_count,
            lexical_count=lexical_count,
            summary_count=summary_count,
        )

    async def _recall_group(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        dimension: int,
        base_ids: list[UUID],
        hybrid_base_ids: frozenset[UUID],
        query_vector: list[float],
        lexical_query: str | None,
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
    ) -> tuple[list[Candidate], int, int, int]:
        """Per-base recall: the semantic route, the lexical route, their merge.

        Each route caps at ``per_base_budget`` parents per base (Segment,
        Child and Summary sources deduplicated by max cosine first). A hybrid base with
        lexical tokens then merges its two routes by ``Σ 1/(60+rank)`` and
        keeps ``C`` parents; a semantic base keeps its semantic route as-is,
        so no base can consume another base's slots (§8.2). Returns the
        merged candidates plus the per-route counts (after the per-route
        caps) for diagnostics. Matched children are read under the caller's
        snapshot: they are recall evidence, never reconstructed by scanning
        children after the fact.
        """

        group_hybrid_ids = [base_id for base_id in base_ids if base_id in hybrid_base_ids]
        general = await self._general_candidates(
            project_id=project_id,
            embedding_model_id=embedding_model_id,
            dimension=dimension,
            base_ids=base_ids,
            query_vector=query_vector,
            per_base_budget=per_base_budget,
            metadata_filters=metadata_filters,
            session=session,
        )
        parents = await self._parent_child_candidates(
            project_id=project_id,
            embedding_model_id=embedding_model_id,
            dimension=dimension,
            base_ids=base_ids,
            query_vector=query_vector,
            per_base_budget=per_base_budget,
            metadata_filters=metadata_filters,
            session=session,
        )
        summaries = await self._summary_candidates(
            project_id=project_id,
            embedding_model_id=embedding_model_id,
            dimension=dimension,
            base_ids=base_ids,
            query_vector=query_vector,
            per_base_budget=per_base_budget,
            metadata_filters=metadata_filters,
            session=session,
        )
        semantic_by_segment: dict[UUID, Candidate] = {}
        for candidate in [*general, *parents, *summaries]:
            previous = semantic_by_segment.get(candidate.segment_id)
            if previous is None or candidate.vector_score > previous.vector_score:
                semantic_by_segment[candidate.segment_id] = candidate
        semantic_by_base: dict[UUID, list[Candidate]] = {}
        for candidate in semantic_by_segment.values():
            semantic_by_base.setdefault(candidate.knowledge_base_id, []).append(candidate)
        for pool in semantic_by_base.values():
            pool.sort(key=candidate_sort_key)
            del pool[per_base_budget:]

        lexical_by_base: dict[UUID, list[tuple[Candidate, float]]] = {}
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
                lexical_pool.sort(key=lambda entry: (-entry[1], *candidate_sort_key(entry[0])))
                del lexical_pool[per_base_budget:]
            lexical_ids = [candidate.segment_id for pool in lexical_by_base.values() for candidate, _ in pool]
            if lexical_ids:
                # Lexical-only parents may sit below every semantic
                # source's cap. Their threshold still needs the real
                # maximum cosine, including an enabled summary.
                lexical_summaries = await self._summary_candidates(
                    project_id=project_id,
                    embedding_model_id=embedding_model_id,
                    dimension=dimension,
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
                        (replace(summaries_by_segment[candidate.segment_id], recall_routes=candidate.recall_routes), score)
                        if candidate.segment_id in summaries_by_segment and summaries_by_segment[candidate.segment_id].vector_score > candidate.vector_score
                        else (candidate, score)
                        for candidate, score in pool
                    ]

        merged: list[Candidate] = []
        for base_id in {*semantic_by_base, *lexical_by_base}:
            semantic_pool = semantic_by_base.get(base_id, [])
            lexical_pool = lexical_by_base.get(base_id, [])
            ordered = semantic_pool if not lexical_pool else merge_recall_routes(semantic_pool, lexical_pool, per_base_budget)
            # Remember each parent's place in its base's recall order
            # before the global cosine sort erases it.
            merged.extend(replace(candidate, recall_rank=rank) for rank, candidate in enumerate(ordered, start=1))
        merged.sort(key=candidate_sort_key)

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
            *current_scope_filters(project_id, metadata_filters),
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
    ) -> list[tuple[Candidate, float]]:
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
                KnowledgeSegmentRow.index_text,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeDocumentRow.parsing_profile,
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
        return [(self._candidate_from_row(row, recall_routes=_LEXICAL_ROUTE), float(row.lexical_score)) for row in rows]

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
    ) -> list[tuple[Candidate, float]]:
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
                KnowledgeSegmentRow.index_text,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeDocumentRow.parsing_profile,
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
        return [(self._candidate_from_row(row, matched_via="child", recall_routes=_LEXICAL_ROUTE), float(row.lexical_score)) for row in rows]

    def _candidate_from_row(
        self,
        row: Any,
        *,
        matched_via: KnowledgeMatchedVia = "segment",
        recall_routes: frozenset[KnowledgeRecallRoute] = frozenset({"semantic"}),
    ) -> Candidate:
        return Candidate(
            segment_id=row.id,
            position=row.position,
            content=row.content,
            index_text=stored_model_text(
                content=row.content,
                index_text=row.index_text,
                parsing_profile=row.parsing_profile,
            ),
            source_position=dict(row.source_position),
            document_id=row.document_id,
            document_name=row.document_name,
            document_version=row.document_version,
            knowledge_base_id=row.knowledge_base_id,
            knowledge_base_name=row.knowledge_base_name,
            vector_score=float(row.vector_score),
            matched_via=matched_via,
            recall_routes=recall_routes,
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

    async def _lexical_scores(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        segment_ids: list[UUID],
        lexical_query: str,
    ) -> dict[UUID, float]:
        """Lexical evidence for fusion: ``ts_rank_cd`` of every recalled parent.

        All recalled parents — semantic bases included — score against the
        same query inside the recall transaction. A row on another
        lexical_version fails loudly instead of contributing a silent zero.
        """

        tsquery = func.to_tsquery("simple", lexical_query)
        statement = select(
            KnowledgeSegmentRow.id,
            KnowledgeSegmentRow.lexical_version,
            func.ts_rank_cd(KnowledgeSegmentRow.lexical_tsv, tsquery, 2).label("lexical_score"),
        ).where(KnowledgeSegmentRow.project_id == project_id, KnowledgeSegmentRow.id.in_(segment_ids))
        rows = (await session.execute(statement)).all()
        scores: dict[UUID, float] = {}
        for row in rows:
            if row.lexical_version != KNOWLEDGE_LEXICAL_VERSION:
                raise _lexical_stale_conflict()
            scores[row.id] = float(row.lexical_score)
        return scores

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
        snapshot: SearchSnapshot,
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
            *current_scope_filters(project_id, metadata_filters),
        )

    def _lateral_scope(
        self,
        project_id: UUID,
        embedding_model_id: UUID,
        targets: Any,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
    ) -> tuple[Any, ...]:
        """Scope of one per-base lateral branch (``targets`` = unnested base ids)."""

        return (
            KnowledgeBaseRow.id == targets.c.base_id,
            # A base rebound to another embedding model between group load and
            # recall drops out here: its vectors no longer match this query
            # embedding's dimension/space.
            KnowledgeBaseRow.embedding_model_id == embedding_model_id,
            *current_scope_filters(project_id, metadata_filters),
        )

    async def _general_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        dimension: int,
        base_ids: list[UUID],
        query_vector: list[float],
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession | None = None,
    ) -> list[Candidate]:
        """Cosine recall over segments that carry their own vectors.

        One ``LATERAL`` branch per targeted base orders by the dimension-typed
        cosine distance and stops at ``per_base_budget`` rows, so one base's
        rows can never crowd another base out of the recall result and the
        branch is eligible for the per-dimension HNSW partial index (an
        unindexed dimension runs the same statement as a sorted scan).
        """

        statement = self._general_statement(
            project_id=project_id,
            embedding_model_id=embedding_model_id,
            dimension=dimension,
            base_ids=base_ids,
            query_vector=query_vector,
            per_base_budget=per_base_budget,
            metadata_filters=metadata_filters,
        )
        return await self._execute_recall(statement, session=session)

    def _general_statement(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        dimension: int,
        base_ids: list[UUID],
        query_vector: list[float],
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
    ) -> Any:
        """The general-route recall statement (exposed for plan inspection)."""

        targets = _base_targets(base_ids)
        distance = _typed_distance(KnowledgeSegmentRow.embedding, dimension, query_vector)
        branch = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.index_text,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeDocumentRow.parsing_profile,
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                (1 - distance).label("vector_score"),
            )
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(
                *self._lateral_scope(project_id, embedding_model_id, targets, metadata_filters),
                # parent_child parents store NULL embeddings and are recalled
                # through their children instead.
                KnowledgeSegmentRow.embedding.is_not(None),
                func.vector_dims(KnowledgeSegmentRow.embedding) == dimension,
            )
            .order_by(distance.asc())
            .limit(per_base_budget)
            .lateral("hits")
        )
        return select(branch).select_from(targets.join(branch, true()))

    async def _summary_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        dimension: int,
        base_ids: list[UUID],
        query_vector: list[float],
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession,
        segment_ids: list[UUID] | None = None,
    ) -> list[Candidate]:
        """Summary vectors recall real Segments under the same scope and cap."""

        targets = _base_targets(base_ids)
        distance = _typed_distance(KnowledgeSegmentSummaryRow.embedding, dimension, query_vector)
        branch = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.index_text,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeDocumentRow.parsing_profile,
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                (1 - distance).label("vector_score"),
            )
            .select_from(KnowledgeSegmentSummaryRow)
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentSummaryRow.knowledge_segment_id)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(
                *self._lateral_scope(project_id, embedding_model_id, targets, metadata_filters),
                KnowledgeBaseRow.summary_index_enabled.is_(True),
                KnowledgeSegmentSummaryRow.project_id == project_id,
                KnowledgeSegmentSummaryRow.knowledge_base_id == KnowledgeBaseRow.id,
                KnowledgeSegmentSummaryRow.knowledge_document_id == KnowledgeDocumentRow.id,
                KnowledgeSegmentSummaryRow.document_version == KnowledgeSegmentRow.document_version,
                func.vector_dims(KnowledgeSegmentSummaryRow.embedding) == dimension,
                *(() if segment_ids is None else (KnowledgeSegmentRow.id.in_(segment_ids),)),
            )
            .order_by(distance.asc())
            .limit(per_base_budget)
            .lateral("hits")
        )
        statement = select(branch).select_from(targets.join(branch, true()))
        return await self._execute_recall(statement, session=session, matched_via="summary")

    async def _parent_child_candidates(
        self,
        *,
        project_id: UUID,
        embedding_model_id: UUID,
        dimension: int,
        base_ids: list[UUID],
        query_vector: list[float],
        per_base_budget: int,
        metadata_filters: tuple[KnowledgeMetadataFilter, ...],
        session: AsyncSession | None = None,
    ) -> list[Candidate]:
        """Child-chunk recall rolled up to parents before the reranker.

        Each base's lateral branch takes its ``per_base_budget ×
        PARENT_CHILD_WINDOW_FACTOR`` nearest children through the same
        index-eligible ordering; the best child score inside each parent then
        becomes the parent's recall score (the plan's 回卷 rule) before the
        per-base cap, so a parent never appears twice. Every parent that
        surfaces is a true top parent (any parent outside the window scores
        below the window's weakest child); a base whose leading parents own
        many near-identical children may surface fewer than ``C`` parents.
        """

        targets = _base_targets(base_ids)
        distance = _typed_distance(KnowledgeSegmentChildRow.embedding, dimension, query_vector)
        children = (
            select(
                KnowledgeSegmentChildRow.knowledge_segment_id.label("segment_id"),
                (1 - distance).label("child_score"),
            )
            .select_from(KnowledgeSegmentChildRow)
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentChildRow.knowledge_segment_id)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(
                *self._lateral_scope(project_id, embedding_model_id, targets, metadata_filters),
                func.vector_dims(KnowledgeSegmentChildRow.embedding) == dimension,
            )
            .order_by(distance.asc())
            .limit(per_base_budget * PARENT_CHILD_WINDOW_FACTOR)
            .lateral("children")
        )
        rollup = select(children.c.segment_id, func.max(children.c.child_score).label("vector_score")).select_from(targets.join(children, true())).group_by(children.c.segment_id).subquery("rollup")
        statement = (
            select(
                KnowledgeSegmentRow.id,
                KnowledgeSegmentRow.position,
                KnowledgeSegmentRow.content,
                KnowledgeSegmentRow.index_text,
                KnowledgeSegmentRow.source_position,
                KnowledgeSegmentRow.document_version,
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentRow.name.label("document_name"),
                KnowledgeDocumentRow.parsing_profile,
                KnowledgeBaseRow.id.label("knowledge_base_id"),
                KnowledgeBaseRow.name.label("knowledge_base_name"),
                rollup.c.vector_score,
            )
            .select_from(rollup)
            .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == rollup.c.segment_id)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
        )
        return await self._execute_recall(statement, session=session, matched_via="child")

    async def _execute_recall(
        self,
        statement: Any,
        *,
        session: AsyncSession | None = None,
        matched_via: KnowledgeMatchedVia = "segment",
    ) -> list[Candidate]:
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
            Candidate(
                segment_id=row.id,
                position=row.position,
                content=row.content,
                index_text=stored_model_text(
                    content=row.content,
                    index_text=row.index_text,
                    parsing_profile=row.parsing_profile,
                ),
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
