"""T8 — the lexical route, hybrid recall, and the final three-branch ranking.

A base opts in with ``retrieval_mode='hybrid'`` (requests may override the
mode for one call, never persisting it). The lexical route recalls by
parameterized OR tsquery over the lexical_v1 derivation — general mode on
segment rows, parent_child on child rows rolled up to their parent — and
merges with the semantic route per base via ``Σ 1/(60+rank)`` before the
per-base cap ``C``. Lexical-only candidates still get their real cosine
(parent_child: the max over all current children) so native thresholds keep
acting on native scores. The final ordering follows design §8.3: a unified
non-null reranker keeps native scores (the lexical route only widens
recall), while hybrid without one fuses ``61/2 * (1/(60+domain_rank) +
1/(60+lexical_rank))`` where every shortlisted parent — semantic bases
included — is scored by the same lexical query. Stale ``lexical_version``
rows fail loudly; queries over 128 deduplicated tokens are rejected only
when a hybrid base is targeted.

Tests run against the installed Schema V1 snapshot with real tsvector/GIN
SQL, reusing the retrieval harness.
"""

from __future__ import annotations

import uuid

import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS,
    KnowledgeError,
)
from actweave_knowledge.models.client import RerankScore
from actweave_knowledge.retrieval import lexical_index_input
from sqlalchemy import text
from test_retrieval import (
    _base_row,
    _child_row,
    _document_row,
    _harness,
    _query_rows,
    _request,
    _RetrievalHarness,
    _seed_models,
    _seed_project,
    _segment_row,
)


def _fused(domain_rank: int, lexical_rank: int | None = None) -> float:
    second = 1.0 / (60.0 + lexical_rank) if lexical_rank is not None else 0.0
    return 61.0 / 2.0 * (1.0 / (60.0 + domain_rank) + second)


def _unit_vector(x: float, dimension: int = 3) -> list[float]:
    import math

    vector = [x, math.sqrt(max(0.0, 1.0 - x * x))]
    return vector + [0.0] * (dimension - 2)


async def _derive_lexical(harness: _RetrievalHarness, project_id: uuid.UUID) -> None:
    """Backfill what the publish path would have written for seeded rows."""

    async with harness.factory() as session, session.begin():
        for table in ("knowledge_segments", "knowledge_segment_children"):
            rows = (
                await session.execute(
                    text(f"SELECT id, content FROM {table} WHERE project_id = :project_id"),  # noqa: S608
                    {"project_id": project_id},
                )
            ).all()
            for row_id, content in rows:
                await session.execute(
                    text(f"UPDATE {table} SET lexical_tsv = to_tsvector('simple', :input), lexical_version = 1 WHERE id = :id"),  # noqa: S608
                    {"input": lexical_index_input(content), "id": row_id},
                )


async def _seed_hybrid_general_base(
    harness: _RetrievalHarness,
    *,
    retrieval_mode: str = "hybrid",
    with_reranker: bool = False,
    segments: list[tuple[str, list[float]]],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """One base in ``retrieval_mode`` with derived lexical fields.

    Returns (project_id, base_id, embedding_id, rerank_id).
    """

    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        embedding_id, rerank_id = await _seed_models(session)
        base = _base_row(project_id, embedding_id, rerank_id if with_reranker else None, name=f"库-{uuid.uuid4().hex[:6]}")
        base.retrieval_mode = retrieval_mode
        session.add(base)
        await session.flush()
        document = _document_row(project_id, base.id, name="手册")
        session.add(document)
        await session.flush()
        for index, (content, embedding) in enumerate(segments, start=1):
            session.add(_segment_row(document, position=index, content=content, embedding=embedding))
    await _derive_lexical(harness, project_id)
    harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
    return project_id, base.id, embedding_id, rerank_id


# ---------------------------------------------------------------------------
# Hybrid recall and fusion (branch 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_lifts_an_exact_token_match_that_cosine_ranks_low(postgres_database_url: str) -> None:
    """The headline: an exact error-code match wins through the lexical rank
    term even though its cosine is far behind."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_hybrid_general_base(
            harness,
            segments=[
                ("错误码e404排查手册", _unit_vector(0.2)),
                ("普通安装说明", _unit_vector(0.9)),
            ],
        )

        result = await harness.service.search(_request(project_id, query="e404", score_threshold=0.0, debug=True))

        assert [hit.citation.snippet for hit in result.hits] == ["错误码e404排查手册", "普通安装说明"]
        exact, plain = result.hits
        # cosine domain: 普通 rank 1, e404 rank 2; lexical: e404 rank 1.
        assert exact.citation.score == pytest.approx(_fused(2, 1))
        assert plain.citation.score == pytest.approx(_fused(1))
        assert exact.citation.score_kind == "rank_fusion"
        assert exact.local_score_kind == "cosine"
        assert exact.local_score == pytest.approx(0.2, abs=1e-6)
        diagnostics = result.diagnostics
        assert diagnostics is not None
        assert diagnostics.retrieval_mode == "hybrid"
        assert diagnostics.ranking_method == "rank_fusion"
        assert diagnostics.counts.semantic_candidates == 2
        assert diagnostics.counts.lexical_candidates == 1
        assert diagnostics.heterogeneous_without_lexical_evidence is False
        [row] = await _query_rows(harness, project_id)
        assert row.top_score == pytest.approx(_fused(2, 1))
        assert row.top_score_kind == "rank_fusion"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_semantic_mode_never_builds_a_lexical_query_or_caps_tokens(postgres_database_url: str) -> None:
    """The same data in semantic mode keeps native cosine ordering, and the
    128-token limit does not apply without a hybrid target."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_hybrid_general_base(
            harness,
            retrieval_mode="semantic",
            segments=[
                ("错误码e404排查手册", _unit_vector(0.2)),
                ("普通安装说明", _unit_vector(0.9)),
            ],
        )

        result = await harness.service.search(_request(project_id, query="e404", score_threshold=0.0, debug=True))

        assert [hit.citation.snippet for hit in result.hits] == ["普通安装说明", "错误码e404排查手册"]
        assert all(hit.citation.score_kind == "cosine" for hit in result.hits)
        assert result.diagnostics is not None
        assert result.diagnostics.retrieval_mode == "semantic"
        assert result.diagnostics.counts.lexical_candidates == 0

        # Far more than 128 deduplicated tokens, accepted in semantic mode.
        long_query = " ".join(f"tok{index}" for index in range(KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS + 20))
        wide = await harness.service.search(_request(project_id, query=long_query, score_threshold=0.0))
        assert [hit.citation.snippet for hit in wide.hits]  # searched, not rejected
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_request_mode_override_applies_to_this_call_only(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_hybrid_general_base(
            harness,
            retrieval_mode="semantic",
            segments=[
                ("错误码e404排查手册", _unit_vector(0.2)),
                ("普通安装说明", _unit_vector(0.9)),
            ],
        )

        lifted = await harness.service.search(_request(project_id, query="e404", score_threshold=0.0, retrieval_mode="hybrid"))
        assert lifted.hits[0].citation.snippet == "错误码e404排查手册"
        assert lifted.hits[0].citation.score_kind == "rank_fusion"

        # The override was never persisted: the next default call is semantic.
        native = await harness.service.search(_request(project_id, query="e404", score_threshold=0.0))
        assert native.hits[0].citation.snippet == "普通安装说明"
        assert native.hits[0].citation.score_kind == "cosine"
        async with harness.factory() as session:
            stored_mode = await session.scalar(text("SELECT retrieval_mode FROM knowledge_bases WHERE id = :id"), {"id": base_id})
        assert stored_mode == "semantic"

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id, retrieval_mode="fancy"))  # type: ignore[arg-type]
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_hybrid_forced_semantic_override_skips_the_lexical_route(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_hybrid_general_base(
            harness,
            retrieval_mode="hybrid",
            segments=[
                ("错误码e404排查手册", _unit_vector(0.2)),
                ("普通安装说明", _unit_vector(0.9)),
            ],
        )

        result = await harness.service.search(_request(project_id, query="e404", score_threshold=0.0, retrieval_mode="semantic", debug=True))

        assert result.hits[0].citation.snippet == "普通安装说明"
        assert result.diagnostics is not None
        assert result.diagnostics.retrieval_mode == "semantic"
        assert result.diagnostics.counts.lexical_candidates == 0
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Query token cap and zero-token queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_query_over_128_tokens_is_rejected_with_a_narrowing_hint(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_hybrid_general_base(
            harness,
            segments=[("普通安装说明", _unit_vector(0.9))],
        )

        long_query = " ".join(f"tok{index}" for index in range(KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS + 1))
        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id, query=long_query))

        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert str(KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS) in error.value.message
        assert "semantic" in error.value.message
        # Rejected before any provider call or query-log row.
        assert harness.client.embed_calls == []
        assert await _query_rows(harness, project_id) == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_zero_token_query_keeps_hybrid_on_the_vector_route(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_hybrid_general_base(
            harness,
            segments=[("普通安装说明", _unit_vector(0.9))],
        )

        result = await harness.service.search(_request(project_id, query="？？？", score_threshold=0.0, debug=True))

        assert [hit.citation.snippet for hit in result.hits] == ["普通安装说明"]
        assert result.diagnostics is not None
        assert result.diagnostics.retrieval_mode == "hybrid"
        assert result.diagnostics.counts.lexical_candidates == 0
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Stale lexical_version fails loudly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_lexical_version_fails_the_hybrid_search_loudly(postgres_database_url: str) -> None:
    """A hybrid target with underived rows must conflict, never silently skip."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_hybrid_general_base(
            harness,
            segments=[
                ("错误码e404排查手册", _unit_vector(0.2)),
                ("普通安装说明", _unit_vector(0.9)),
            ],
        )
        async with harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_segments SET lexical_version = 0 WHERE project_id = :project_id AND content = '普通安装说明'"),
                {"project_id": project_id},
            )

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id, query="e404", score_threshold=0.0))
        assert error.value.code == KNOWLEDGE_CONFLICT
        assert "词法" in error.value.message
        assert await _query_rows(harness, project_id) == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_mixed_search_conflicts_when_a_semantic_bases_shortlisted_row_is_underived(postgres_database_url: str) -> None:
    """Fusion scores every shortlisted parent lexically — a semantic base's
    underived row inside the shortlist is a loud conflict, not a silent 0."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, _ = await _seed_models(session)
            hybrid_base = _base_row(project_id, embedding_id, None, name="混合库")
            hybrid_base.retrieval_mode = "hybrid"
            semantic_base = _base_row(project_id, embedding_id, None, name="语义库")
            session.add_all([hybrid_base, semantic_base])
            await session.flush()
            hybrid_doc = _document_row(project_id, hybrid_base.id, name="混合文档")
            semantic_doc = _document_row(project_id, semantic_base.id, name="语义文档")
            session.add_all([hybrid_doc, semantic_doc])
            await session.flush()
            session.add(_segment_row(hybrid_doc, position=1, content="错误码e404排查手册", embedding=_unit_vector(0.5)))
            session.add(_segment_row(semantic_doc, position=1, content="语义库e404相关内容", embedding=_unit_vector(0.7)))
        # Only the hybrid base's rows are derived; the semantic base's row
        # still carries the version-0 placeholder.
        async with harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_segments SET lexical_tsv = to_tsvector('simple', :input), lexical_version = 1 WHERE knowledge_base_id = :base_id"),
                {"input": lexical_index_input("错误码e404排查手册"), "base_id": hybrid_base.id},
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id, query="e404", score_threshold=0.0))
        assert error.value.code == KNOWLEDGE_CONFLICT
        assert "词法" in error.value.message
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Native thresholds and unified-reranker widening (branches 1 and thresholds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lexical_only_hit_still_obeys_the_cosine_threshold(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_hybrid_general_base(
            harness,
            segments=[("错误码e404排查手册", _unit_vector(0.1))],
        )

        result = await harness.service.search(_request(project_id, query="e404", score_threshold=0.5, debug=True))

        assert result.hits == ()
        assert result.diagnostics is not None
        assert result.diagnostics.counts.lexical_candidates == 1
        assert result.diagnostics.counts.threshold_filtered == 1
        assert result.diagnostics.empty_reason == "filtered_out"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_unified_reranker_keeps_native_scores_while_lexical_widens_recall(postgres_database_url: str) -> None:
    """Branch 1 with a hybrid base: the lexical route saves a candidate the
    semantic cap dropped, the shared reranker scores it, ordering stays native."""

    harness = await _harness(postgres_database_url)
    try:
        # 21 well-matching plain segments push the exact-token segment out of
        # the semantic route's per-base cap (C = 20 for top_k <= 4).
        segments: list[tuple[str, list[float]]] = [(f"普通段落{index:02d}", _unit_vector(0.9 - 0.004 * index)) for index in range(21)]
        segments.append(("错误码e404排查手册", _unit_vector(0.05)))
        project_id, _, _, rerank_id = await _seed_hybrid_general_base(
            harness,
            with_reranker=True,
            segments=segments,
        )
        async with harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_segments SET index_text = 'model:' || content WHERE project_id = :project_id"),
                {"project_id": project_id},
            )
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=index, score=0.95 if "e404" in document else 0.5) for index, document in enumerate(documents)][:top_n]

        result = await harness.service.search(_request(project_id, query="e404", top_k=1, debug=True))

        assert [hit.citation.snippet for hit in result.hits] == ["错误码e404排查手册"]
        top = result.hits[0]
        assert top.citation.score == pytest.approx(0.95)  # native rerank, no fusion
        assert top.citation.score_kind == "rerank"
        assert top.ranking_method == "rerank"
        [(_, _, submitted, _)] = harness.client.rerank_calls
        assert "model:错误码e404排查手册" in submitted
        assert all(document.startswith("model:") for document in submitted)
        assert result.diagnostics is not None
        assert result.diagnostics.ranking_method == "rerank"
        assert result.diagnostics.retrieval_mode == "hybrid"
        assert result.diagnostics.counts.lexical_candidates >= 1
        assert result.diagnostics.heterogeneous_without_lexical_evidence is False
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_route_rrf_merge_keeps_the_lexical_winner_inside_the_per_base_cap(postgres_database_url: str) -> None:
    """Per-base recall merges the two routes with Σ1/(60+rank) before C: the
    lexical-only winner stays, the weakest semantic tail is displaced."""

    harness = await _harness(postgres_database_url)
    try:
        segments: list[tuple[str, list[float]]] = [(f"普通段落{index:02d}", _unit_vector(0.9 - 0.004 * index)) for index in range(20)]
        segments.append(("错误码e404排查手册", _unit_vector(0.0)))
        project_id, _, _, _ = await _seed_hybrid_general_base(
            harness,
            segments=segments,
        )

        result = await harness.service.search(_request(project_id, query="e404", score_threshold=0.0, top_k=4, debug=True))

        snippets = [hit.citation.snippet for hit in result.hits]
        assert snippets[0] == "错误码e404排查手册"  # lexical rank 1 dominates fusion
        assert result.diagnostics is not None
        # 21 seeded parents, cap C=20: the merged pool kept the lexical winner
        # and dropped the weakest semantic-only parent.
        merged_total = result.diagnostics.counts.semantic_candidates + result.diagnostics.counts.lexical_candidates
        assert result.diagnostics.counts.lexical_candidates == 1
        assert merged_total <= 21
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# parent_child: lexical children roll up, cosine comes from all children
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_child_lexical_rolls_up_and_takes_max_cosine_over_all_children(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, _ = await _seed_models(session)
            base = _base_row(project_id, embedding_id, None, name="父子库")
            base.retrieval_mode = "hybrid"
            session.add(base)
            await session.flush()
            document = _document_row(project_id, base.id, name="父子文档")
            document.chunking_mode = "parent_child"
            session.add(document)
            await session.flush()
            parent_a = _segment_row(document, position=1, content="错误码e404排查。常规处理流程。", embedding=None)
            parent_b = _segment_row(document, position=2, content="安装指南总览。", embedding=None)
            session.add_all([parent_a, parent_b])
            await session.flush()
            session.add(_child_row(parent_a, position=1, content="e404报错处理", embedding=_unit_vector(0.1)))
            session.add(_child_row(parent_a, position=2, content="常规处理流程", embedding=_unit_vector(0.6)))
            session.add(_child_row(parent_b, position=1, content="安装指南首选", embedding=_unit_vector(0.9)))
        await _derive_lexical(harness, project_id)
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id, query="e404", score_threshold=0.0, debug=True))

        assert [hit.citation.segment_id for hit in result.hits] == [parent_a.id, parent_b.id]
        top = result.hits[0]
        # cosine domain: B rank 1 (0.9), A rank 2 (0.6); lexical: A rank 1.
        assert top.citation.score == pytest.approx(_fused(2, 1))
        # The parent's native cosine is the max over ALL current children,
        # not just the lexical match's own low cosine.
        assert top.local_score == pytest.approx(0.6, abs=1e-6)
        assert top.local_score_kind == "cosine"
        # The lexical child match is real recall evidence on the hit.
        lexical_children = [child for child in top.matched_children if child.route == "lexical"]
        assert lexical_children and lexical_children[0].position == 1
        assert any(child.route == "semantic" for child in top.matched_children)
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Mixed semantic + hybrid targets share the lexical scoring evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_shortlisted_parent_gets_lexical_evidence_not_just_hybrid_recalls(postgres_database_url: str) -> None:
    """A semantic base's shortlisted parent scores lexically too (same query),
    while the semantic base never contributes lexical-route candidates."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, _ = await _seed_models(session)
            hybrid_base = _base_row(project_id, embedding_id, None, name="混合库")
            hybrid_base.retrieval_mode = "hybrid"
            semantic_base = _base_row(project_id, embedding_id, None, name="语义库")
            session.add_all([hybrid_base, semantic_base])
            await session.flush()
            hybrid_doc = _document_row(project_id, hybrid_base.id, name="混合文档")
            semantic_doc = _document_row(project_id, semantic_base.id, name="语义文档")
            session.add_all([hybrid_doc, semantic_doc])
            await session.flush()
            session.add(_segment_row(hybrid_doc, position=1, content="混合库e404广泛说明与背景铺垫", embedding=_unit_vector(0.5)))
            session.add(_segment_row(hybrid_doc, position=2, content="混合库普通内容", embedding=_unit_vector(0.8)))
            session.add(_segment_row(semantic_doc, position=1, content="语义库e404精准记录", embedding=_unit_vector(0.7)))
        await _derive_lexical(harness, project_id)
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id, query="e404", score_threshold=0.0, top_k=3, debug=True))

        by_snippet = {hit.citation.snippet: hit for hit in result.hits}
        # Both e404 parents carry a positive lexical term (> the 0.5 ceiling
        # of a domain-rank-only score); the plain parent cannot exceed it.
        assert by_snippet["语义库e404精准记录"].ranking_score > 0.5
        assert by_snippet["混合库e404广泛说明与背景铺垫"].ranking_score > 0.5
        assert by_snippet["混合库普通内容"].ranking_score <= 0.5
        assert result.diagnostics is not None
        # The lexical route ran only against the hybrid base.
        assert result.diagnostics.counts.lexical_candidates == 1
        assert result.diagnostics.heterogeneous_without_lexical_evidence is False
    finally:
        await harness.engine.dispose()
