"""T7 — per-base candidate budgets, score domains, rank fusion and safe debug.

The budget formula ``C = min(B, floor(G/N))`` with ``B = min(100, max(20,
5*top_k))`` and ``G = 400`` gives every targeted base its own recall slots, so
one large base can no longer starve the others out of the reranker. The final
ordering branches on the strategy the targeted bases bind (design §8.3): a
single comparable score domain keeps its native ordering, while heterogeneous
domains fall back to explainable rank fusion — never a numeric comparison of
different models' raw scores. The per-search strategy snapshot is re-checked
before every provider dispatch and inside the final review, so a mid-search
rebinding of any targeted base becomes ``KNOWLEDGE_CONFLICT``. Debug
diagnostics report actual counts, monotonic stage timings and the empty
reason. Tests run against the installed Schema V1 snapshot with real pgvector
SQL (shared harness from ``test_retrieval``).
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET,
    KNOWLEDGE_INVALID_REQUEST,
    KnowledgeError,
)
from actweave_knowledge.models.client import KnowledgeModelClient, RerankScore
from actweave_knowledge.persistence.models import KnowledgeBaseRow
from actweave_knowledge.retrieval import (
    KnowledgeSearchService,
)
from registry_helpers import registry_model_port
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_retrieval import (
    _base_row,
    _document_row,
    _harness,
    _query_rows,
    _request,
    _rerank_side_effect,
    _RetrievalHarness,
    _seed_models,
    _seed_project,
    _segment_row,
)

_RANK_1_FUSED = 61.0 / 2.0 / 61.0  # 0.5: domain rank 1, no lexical evidence
_RANK_2_FUSED = 61.0 / 2.0 / 62.0
_RANK_3_FUSED = 61.0 / 2.0 / 63.0


def _unit_vector(x: float, dimension: int = 3) -> list[float]:
    """A unit vector whose cosine against the query [1, 0, 0, ...] is ``x``."""

    vector = [x, math.sqrt(max(0.0, 1.0 - x * x))]
    return vector + [0.0] * (dimension - 2)


# ---------------------------------------------------------------------------
# Budget formula and global rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_more_target_bases_than_the_global_budget_reject_before_any_model_call(postgres_database_url: str) -> None:
    """C < 1 is an explicit refusal to narrow, never a silently ignored base."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, _ = await _seed_models(session)
            for index in range(KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET + 1):
                session.add(_base_row(project_id, embedding_id, None, name=f"库{index:03d}"))

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))

        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert str(KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET) in error.value.message
        assert "knowledge_base_ids" in error.value.message
        assert harness.client.embed_calls == []
        assert await _query_rows(harness, project_id) == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_a_large_base_cannot_starve_a_small_base_out_of_recall(postgres_database_url: str) -> None:
    """Per-base C replaces the group-shared budget: the small base's candidate
    reaches the shared reranker even when the large base has 25 rows with
    better cosine scores."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            large = _base_row(project_id, embedding_id, rerank_id, name="大库")
            small = _base_row(project_id, embedding_id, rerank_id, name="小库")
            session.add_all([large, small])
            await session.flush()
            large_doc = _document_row(project_id, large.id, name="大库文档")
            small_doc = _document_row(project_id, small.id, name="小库文档")
            session.add_all([large_doc, small_doc])
            await session.flush()
            for position in range(1, 26):
                session.add(
                    _segment_row(
                        large_doc,
                        position=position,
                        content=f"大库段落{position:02d}",
                        embedding=_unit_vector(0.9 - 0.004 * (position - 1)),
                    )
                )
            session.add(_segment_row(small_doc, position=1, content="小库黑马段落", embedding=_unit_vector(0.5)))
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=index, score=0.99 if document == "小库黑马段落" else 0.9 - 0.02 * index) for index, document in enumerate(documents)][:top_n]

        result = await harness.service.search(_request(project_id, top_k=1, debug=True))

        assert [(hit.citation.snippet, hit.citation.score) for hit in result.hits] == [("小库黑马段落", 0.99)]
        assert result.hits[0].citation.score_kind == "rerank"  # one shared domain: native
        diagnostics = result.diagnostics
        assert diagnostics is not None
        assert diagnostics.per_base_route_budget == 20  # C = min(B=20, floor(400/2))
        assert diagnostics.counts.semantic_candidates == 21  # 20 capped + 1
        assert diagnostics.counts.returned == 1
        [(_, _, submitted, _)] = harness.client.rerank_calls
        # Recall keeps 20 + 1 parents, but the reranker input budget for
        # top_k=1 is min(100, 10) split across two bases: 5 + 1 passages.
        assert len(submitted) == 6
        assert "小库黑马段落" in submitted
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# §8.3 branch 1/2: single comparable score domain keeps native ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_shared_reranker_across_embeddings_keeps_native_ordering(postgres_database_url: str) -> None:
    """All targeted bases bound to the same non-null reranker: one score
    domain, native rerank scores, no fusion — and the log stays same-source."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_a, shared_rerank = await _seed_models(session, dimension=3)
            embedding_b, _ = await _seed_models(session, dimension=4)
            base_a = _base_row(project_id, embedding_a, shared_rerank, name="甲库")
            base_b = _base_row(project_id, embedding_b, shared_rerank, name="乙库")
            session.add_all([base_a, base_b])
            await session.flush()
            doc_a = _document_row(project_id, base_a.id, name="甲文档")
            doc_b = _document_row(project_id, base_b.id, name="乙文档")
            session.add_all([doc_a, doc_b])
            await session.flush()
            session.add(_segment_row(doc_a, position=1, content="甲库段落", embedding=[1.0, 0.0, 0.0]))
            session.add(_segment_row(doc_b, position=1, content="乙库段落", embedding=[1.0, 0.0, 0.0, 0.0]))
        harness.client.query_vectors[embedding_a] = [1.0, 0.0, 0.0]
        harness.client.query_vectors[embedding_b] = [1.0, 0.0, 0.0, 0.0]
        scores = {"甲库段落": 0.6, "乙库段落": 0.9}
        harness.client.rerank_scripts[shared_rerank] = lambda documents, top_n: [RerankScore(index=index, score=scores[document]) for index, document in enumerate(documents)][:top_n]

        result = await harness.service.search(_request(project_id, top_k=2, debug=True))

        assert [(hit.citation.snippet, hit.citation.score, hit.citation.score_kind) for hit in result.hits] == [
            ("乙库段落", 0.9, "rerank"),
            ("甲库段落", 0.6, "rerank"),
        ]
        assert all(hit.ranking_method == "rerank" and hit.ranking_score == hit.local_score for hit in result.hits)
        assert result.diagnostics is not None
        assert result.diagnostics.ranking_method == "rerank"
        assert result.diagnostics.heterogeneous_without_lexical_evidence is False
        [row] = await _query_rows(harness, project_id)
        assert row.top_score == pytest.approx(0.9)
        assert row.top_score_kind == "rerank"  # provenance from the same returned hit
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# §8.3 branch 3: heterogeneous domains fuse by shared RANK places
# ---------------------------------------------------------------------------


async def _seed_two_reranker_domains(harness: _RetrievalHarness) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One embedding, two bases with different rerankers → two score domains.

    甲域库 carries 高分段落 (rerank 0.9) and 低分段落 (rerank 0.7); 乙域库
    carries 乙域段落 (rerank 0.8). Returns (project_id, base_a_id, base_b_id).
    """

    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        embedding_id, rerank_a = await _seed_models(session)
        _, rerank_b = await _seed_models(session)
        base_a = _base_row(project_id, embedding_id, rerank_a, name="甲域库")
        base_b = _base_row(project_id, embedding_id, rerank_b, name="乙域库")
        session.add_all([base_a, base_b])
        await session.flush()
        doc_a = _document_row(project_id, base_a.id, name="甲域文档")
        doc_b = _document_row(project_id, base_b.id, name="乙域文档")
        session.add_all([doc_a, doc_b])
        await session.flush()
        session.add(_segment_row(doc_a, position=1, content="高分段落", embedding=[1.0, 0.0, 0.0]))
        session.add(_segment_row(doc_a, position=2, content="低分段落", embedding=_unit_vector(0.9)))
        session.add(_segment_row(doc_b, position=1, content="乙域段落", embedding=[1.0, 0.0, 0.0]))
    harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
    scores = {"高分段落": 0.9, "低分段落": 0.7, "乙域段落": 0.8}
    script = lambda documents, top_n: [RerankScore(index=index, score=scores[document]) for index, document in enumerate(documents)][:top_n]  # noqa: E731
    harness.client.rerank_scripts[rerank_a] = script
    harness.client.rerank_scripts[rerank_b] = script
    return project_id, base_a.id, base_b.id


@pytest.mark.asyncio
async def test_heterogeneous_rerankers_fuse_by_domain_rank_and_reuse_the_query_embedding(postgres_database_url: str) -> None:
    """Two rerank domains: fused score is 61/2 * 1/(60+rank), never a raw
    cross-model comparison; the native evidence stays on the hit."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_a_id, base_b_id = await _seed_two_reranker_domains(harness)

        result = await harness.service.search(_request(project_id, top_k=3, debug=True))

        # One embedding model across both groups: the query is embedded once.
        assert len(harness.client.embed_calls) == 1

        by_snippet = {hit.citation.snippet: hit for hit in result.hits}
        assert by_snippet["高分段落"].citation.score == pytest.approx(_RANK_1_FUSED)
        assert by_snippet["乙域段落"].citation.score == pytest.approx(_RANK_1_FUSED)
        assert by_snippet["低分段落"].citation.score == pytest.approx(_RANK_2_FUSED)
        assert result.hits[2].citation.snippet == "低分段落"
        # Equal fused scores order by resource identity (base UUID first).
        expected_head = ["高分段落", "乙域段落"] if base_a_id < base_b_id else ["乙域段落", "高分段落"]
        assert [hit.citation.snippet for hit in result.hits[:2]] == expected_head

        assert all(hit.citation.score_kind == "rank_fusion" for hit in result.hits)
        assert all(hit.ranking_method == "rank_fusion" for hit in result.hits)
        # Native evidence is preserved next to the fused ordering score.
        assert by_snippet["高分段落"].local_score == pytest.approx(0.9)
        assert by_snippet["高分段落"].local_score_kind == "rerank"

        diagnostics = result.diagnostics
        assert diagnostics is not None
        assert diagnostics.ranking_method == "rank_fusion"
        assert diagnostics.heterogeneous_without_lexical_evidence is True

        [row] = await _query_rows(harness, project_id)
        assert row.top_score == pytest.approx(_RANK_1_FUSED)
        assert row.top_score_kind == "rank_fusion"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cosine_domains_fuse_and_zero_threshold_passes_negative_scores(postgres_database_url: str) -> None:
    """NULL-reranker bases on different embeddings are separate cosine domains;
    a 0 threshold filters nothing (negative cosine fuses at its domain rank),
    and a positive threshold still acts on the native score, not the fusion."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_a, _ = await _seed_models(session, dimension=3)
            embedding_b, _ = await _seed_models(session, dimension=4)
            base_a = _base_row(project_id, embedding_a, None, name="正向库", default_score_threshold=0.0)
            base_b = _base_row(project_id, embedding_b, None, name="负向库", default_score_threshold=0.0)
            session.add_all([base_a, base_b])
            await session.flush()
            doc_a = _document_row(project_id, base_a.id, name="正向文档")
            doc_b = _document_row(project_id, base_b.id, name="负向文档")
            session.add_all([doc_a, doc_b])
            await session.flush()
            session.add(_segment_row(doc_a, position=1, content="正向段落", embedding=[1.0, 0.0, 0.0]))
            session.add(_segment_row(doc_b, position=1, content="负向段落", embedding=[-1.0, 0.0, 0.0, 0.0]))
        harness.client.query_vectors[embedding_a] = [1.0, 0.0, 0.0]
        harness.client.query_vectors[embedding_b] = [1.0, 0.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id, top_k=2, debug=True))

        by_snippet = {hit.citation.snippet: hit for hit in result.hits}
        assert set(by_snippet) == {"正向段落", "负向段落"}
        # Both are rank 1 inside their own cosine domain: equal fused scores.
        assert all(hit.citation.score == pytest.approx(_RANK_1_FUSED) for hit in result.hits)
        assert by_snippet["负向段落"].local_score == pytest.approx(-1.0)
        assert by_snippet["负向段落"].local_score_kind == "cosine"
        assert by_snippet["负向段落"].citation.score_kind == "rank_fusion"
        assert result.diagnostics is not None
        assert result.diagnostics.heterogeneous_without_lexical_evidence is True

        filtered = await harness.service.search(_request(project_id, top_k=2, score_threshold=0.3, debug=True))

        # The threshold removed the negative-cosine candidate before ranking,
        # but the strategy still spans two domains: the branch stays fusion.
        assert [hit.citation.snippet for hit in filtered.hits] == ["正向段落"]
        assert filtered.hits[0].citation.score == pytest.approx(_RANK_1_FUSED)
        assert filtered.diagnostics is not None
        assert filtered.diagnostics.counts.threshold_filtered == 1
        assert filtered.diagnostics.ranking_method == "rank_fusion"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_equal_native_scores_share_a_rank_and_identity_orders_them_stably(postgres_database_url: str) -> None:
    """RANK semantics inside a domain: 0.9, 0.9, 0.7 rank as 1, 1, 3 — equal
    native scores share one fused score, and identity only orders equals."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_a, rerank_a = await _seed_models(session, dimension=3)
            embedding_b, _ = await _seed_models(session, dimension=4)
            base_a = _base_row(project_id, embedding_a, rerank_a, name="重排域库")
            base_b = _base_row(project_id, embedding_b, None, name="余弦域库")
            session.add_all([base_a, base_b])
            await session.flush()
            doc_a = _document_row(project_id, base_a.id, name="重排文档")
            doc_b = _document_row(project_id, base_b.id, name="余弦文档")
            session.add_all([doc_a, doc_b])
            await session.flush()
            session.add(_segment_row(doc_a, position=1, content="并列甲", embedding=[1.0, 0.0, 0.0]))
            session.add(_segment_row(doc_a, position=2, content="并列乙", embedding=_unit_vector(0.95)))
            session.add(_segment_row(doc_a, position=3, content="第三名", embedding=_unit_vector(0.9)))
            session.add(_segment_row(doc_b, position=1, content="余弦独占", embedding=[0.8, 0.6, 0.0, 0.0]))
        harness.client.query_vectors[embedding_a] = [1.0, 0.0, 0.0]
        harness.client.query_vectors[embedding_b] = [1.0, 0.0, 0.0, 0.0]
        scores = {"并列甲": 0.9, "并列乙": 0.9, "第三名": 0.7}
        harness.client.rerank_scripts[rerank_a] = lambda documents, top_n: [RerankScore(index=index, score=scores[document]) for index, document in enumerate(documents)][:top_n]

        result = await harness.service.search(_request(project_id, top_k=4, debug=True))
        repeat = await harness.service.search(_request(project_id, top_k=4, debug=True))

        by_snippet = {hit.citation.snippet: hit for hit in result.hits}
        assert by_snippet["并列甲"].citation.score == pytest.approx(_RANK_1_FUSED)
        assert by_snippet["并列乙"].citation.score == pytest.approx(_RANK_1_FUSED)
        assert by_snippet["余弦独占"].citation.score == pytest.approx(_RANK_1_FUSED)
        # Shared first place means the next place is 3, never 2.
        assert by_snippet["第三名"].citation.score == pytest.approx(_RANK_3_FUSED)
        assert result.hits[3].citation.snippet == "第三名"
        # Identity keeps equal fusions in one deterministic order across runs.
        assert [hit.citation.segment_id for hit in repeat.hits] == [hit.citation.segment_id for hit in result.hits]
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Strategy snapshot: re-checked before dispatch and inside the final review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebinding_an_unmatched_target_base_mid_search_still_conflicts(postgres_database_url: str) -> None:
    """The snapshot covers every targeted base — not only the bases that
    produced hits — so no rebinding can hide behind an empty result."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base_with_hits = _base_row(project_id, embedding_id, rerank_id, name="命中库")
            silent_base = _base_row(project_id, embedding_id, rerank_id, name="无命中库")
            session.add_all([base_with_hits, silent_base])
            await session.flush()
            document = _document_row(project_id, base_with_hits.id, name="命中文档")
            session.add(document)
            await session.flush()
            session.add(_segment_row(document, position=1, content="命中段落", embedding=[1.0, 0.0, 0.0]))
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
        silent_base_id = silent_base.id

        async def _rebind_silent_base() -> None:
            async with harness.factory() as session, session.begin():
                _, other_rerank = await _seed_models(session)
                row = await session.get(KnowledgeBaseRow, silent_base_id)
                assert row is not None
                row.reranker_model_id = other_rerank

        _rerank_side_effect(harness, _rebind_silent_base)

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))

        assert error.value.code == KNOWLEDGE_CONFLICT
        assert await _query_rows(harness, project_id) == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_rebinding_between_rerank_batches_conflicts_at_the_final_review(postgres_database_url: str) -> None:
    """One rerank call shares a single pre-dispatch strategy check; a
    rebinding that lands between its batches is caught by the final review
    and the search fails with a conflict instead of mixing two strategies."""

    engine = create_async_engine(postgres_database_url)
    client: KnowledgeModelClient | None = None
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session, rerank_max_batch=1)
            base = _base_row(project_id, embedding_id, rerank_id, name="批间改绑库")
            session.add(base)
            await session.flush()
            document = _document_row(project_id, base.id, name="批间文档")
            session.add(document)
            await session.flush()
            session.add(_segment_row(document, position=1, content="候选一", embedding=[1.0, 0.0, 0.0]))
            session.add(_segment_row(document, position=2, content="候选二", embedding=[1.0, 0.0, 0.0]))
        base_id = base.id

        rerank_requests: list[dict[str, Any]] = []

        async def _handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if request.url.path.endswith("/embeddings"):
                return httpx.Response(
                    200,
                    json={"data": [{"index": index, "embedding": [1.0, 0.0, 0.0]} for index in range(len(payload["input"]))]},
                )
            rerank_requests.append(payload)
            # Rebind after serving this batch; the final review must refuse
            # to return scores computed under the replaced strategy.
            async with factory() as session, session.begin():
                _, other_rerank = await _seed_models(session)
                row = await session.get(KnowledgeBaseRow, base_id)
                assert row is not None
                row.reranker_model_id = other_rerank
            return httpx.Response(
                200,
                json={"results": [{"index": index, "relevance_score": 0.9} for index in range(len(payload["documents"]))]},
            )

        client = KnowledgeModelClient(http=httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
        service = KnowledgeSearchService(session_factory=factory, client=client, model_port=registry_model_port())

        with pytest.raises(KnowledgeError) as error:
            await service.search(_request(project_id, top_k=2))

        assert error.value.code == KNOWLEDGE_CONFLICT
        assert len(rerank_requests) == 2  # both batches ran under one pre-dispatch check
    finally:
        if client is not None:
            await client.aclose()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Safe debug: actual counts, stage timings and the empty reason
# ---------------------------------------------------------------------------
