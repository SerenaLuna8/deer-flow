"""M11 summary recall remains a bounded source of real Segment citations."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict

import httpx
import pytest
from actweave_knowledge import KNOWLEDGE_CONFLICT, KnowledgeError, KnowledgeMetadataFilter
from actweave_knowledge.persistence.models import KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeSegmentRow, KnowledgeSegmentSummaryRow
from sqlalchemy import select
from test_lexical_retrieval import _derive_lexical
from test_retrieval import _base_row, _child_row, _document_row, _harness, _query_rows, _request, _seed_single_base, _segment_row
from test_retrieval_query_cache import _cache_harness


def _vector(score: float) -> list[float]:
    return [score, math.sqrt(1.0 - score * score), 0.0]


def _summary(segment: KnowledgeSegmentRow, score: float = 1.0) -> KnowledgeSegmentSummaryRow:
    return KnowledgeSegmentSummaryRow(
        id=uuid.uuid4(),
        project_id=segment.project_id,
        knowledge_base_id=segment.knowledge_base_id,
        knowledge_document_id=segment.knowledge_document_id,
        knowledge_segment_id=segment.id,
        document_version=segment.document_version,
        content="SUMMARY_ONLY_MARKER 故障原因归纳",
        source_content_digest=hashlib.sha256(segment.content.encode()).hexdigest(),
        embedding=_vector(score),
    )


@pytest.mark.asyncio
async def test_disabling_summary_index_during_warm_search_conflicts_before_citations_return(postgres_database_url: str) -> None:
    toggle = False

    async def on_dispatch(request: httpx.Request) -> None:
        if toggle and request.url.path.endswith("/rerank"):
            async with harness.retrieval.factory() as session, session.begin():
                base = await session.get(KnowledgeBaseRow, harness.base_id)
                base.summary_index_enabled = False

    async with _cache_harness(postgres_database_url, with_reranker=True, on_dispatch=on_dispatch) as harness:
        async with harness.retrieval.factory() as session, session.begin():
            base = await session.get(KnowledgeBaseRow, harness.base_id)
            base.summary_index_enabled = True
            segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_base_id == harness.base_id, KnowledgeSegmentRow.position == 2))
            session.add(_summary(segment))
        request = _request(harness.project_id, debug=True)
        warmup = await harness.module.search(request, authority=harness.authority)
        assert any(hit.matched_via == "summary" for hit in warmup.diagnostics.hit_diagnostics)
        toggle = True

        with pytest.raises(KnowledgeError) as error:
            await harness.module.search(request, authority=harness.authority)

        assert error.value.code == KNOWLEDGE_CONFLICT
        assert len(harness.embedding_requests) == 1
        assert len(await _query_rows(harness.retrieval, harness.project_id)) == 1


@pytest.mark.asyncio
async def test_summary_recalls_a_segment_outside_the_content_vector_budget_without_quoting_summary(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(
            harness,
            segments=[("源段落的真实内容", _vector(0.1)), *[(f"普通段落{index}", _vector(0.8)) for index in range(20)]],
            with_reranker=False,
        )
        async with harness.factory() as session, session.begin():
            base = await session.get(KnowledgeBaseRow, base_id)
            base.summary_index_enabled = True
            segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_base_id == base_id, KnowledgeSegmentRow.position == 1))
            session.add(_summary(segment))

        result = await harness.service.search(_request(project_id, query="故障原因", top_k=1, score_threshold=0.95, debug=True))

        assert [hit.passage for hit in result.hits] == ["源段落的真实内容"]
        assert result.hits[0].local_score == pytest.approx(1.0)
        assert result.diagnostics is not None
        assert result.diagnostics.counts.summary_candidates == 1
        assert result.diagnostics.counts.semantic_candidates == 20
        assert result.diagnostics.hit_diagnostics[0].matched_via == "summary"
        assert "SUMMARY_ONLY_MARKER" not in json.dumps(asdict(result), ensure_ascii=False, default=str)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "content_score", "summary_score", "expected_source", "expected_score"),
    [
        ("general", 0.9, 0.5, "segment", 0.9),
        ("general", 0.2, 0.9, "summary", 0.9),
        ("general", 0.8, 0.8, "segment", 0.8),
        ("parent_child", 0.9, 0.5, "child", 0.9),
        ("parent_child", 0.2, 0.9, "summary", 0.9),
        ("parent_child", 0.8, 0.8, "child", 0.8),
    ],
)
async def test_semantic_score_and_attribution_follow_the_max_source(postgres_database_url: str, mode: str, content_score: float, summary_score: float, expected_source: str, expected_score: float) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(harness, segments=[("真实父段正文", _vector(content_score))], with_reranker=False)
        async with harness.factory() as session, session.begin():
            base = await session.get(KnowledgeBaseRow, base_id)
            base.summary_index_enabled = True
            segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_base_id == base_id))
            if mode == "parent_child":
                document = await session.get(KnowledgeDocumentRow, segment.knowledge_document_id)
                document.chunking_mode = "parent_child"
                segment.embedding = None
                session.add(_child_row(segment, position=1, content="子块命中内容", embedding=_vector(content_score)))
            session.add(_summary(segment, summary_score))

        result = await harness.service.search(_request(project_id, debug=True, score_threshold=0.6))

        assert len(result.hits) == 1
        assert result.hits[0].local_score == pytest.approx(expected_score, abs=1e-6)
        assert result.diagnostics.hit_diagnostics[0].matched_via == expected_source
        assert result.diagnostics.counts.semantic_candidates == 1
        assert result.diagnostics.counts.summary_candidates == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["general", "parent_child"])
async def test_lexical_only_candidate_still_gets_its_true_summary_cosine(postgres_database_url: str, mode: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(
            harness,
            segments=[("精确故障码ZX904", _vector(0.2)), *[(f"普通内容{index}", _vector(0.9)) for index in range(20)]],
            with_reranker=False,
        )
        async with harness.factory() as session, session.begin():
            base = await session.get(KnowledgeBaseRow, base_id)
            base.summary_index_enabled = True
            base.retrieval_mode = "hybrid"
            segments = list((await session.scalars(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_base_id == base_id))).all())
            for segment in segments:
                exact = segment.position == 1
                session.add(_summary(segment, 0.7 if exact else 0.95))
                if mode == "parent_child":
                    document = await session.get(KnowledgeDocumentRow, segment.knowledge_document_id)
                    document.chunking_mode = "parent_child"
                    segment.embedding = None
                    session.add(_child_row(segment, position=1, content=segment.content, embedding=_vector(0.2 if exact else 0.9)))
        await _derive_lexical(harness, project_id)

        result = await harness.service.search(_request(project_id, query="ZX904", top_k=1, score_threshold=0.6, debug=True))

        assert [hit.passage for hit in result.hits] == ["精确故障码ZX904"]
        assert result.hits[0].local_score == pytest.approx(0.7, abs=1e-6)
        assert result.diagnostics.hit_diagnostics[0].matched_via == "summary"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exclusion",
    ["switch_off", "base_disabled", "segment_disabled", "document_disabled", "document_not_ready", "stale_segment", "stale_summary", "wrong_summary_project", "metadata"],
)
async def test_summary_route_applies_hard_scope_filters_before_candidate_budget(postgres_database_url: str, exclusion: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(harness, segments=[("受治理的真实内容", _vector(0.1))], with_reranker=False)
        async with harness.factory() as session, session.begin():
            base = await session.get(KnowledgeBaseRow, base_id)
            base.summary_index_enabled = exclusion != "switch_off"
            segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_base_id == base_id))
            document = await session.get(KnowledgeDocumentRow, segment.knowledge_document_id)
            summary = _summary(segment)
            if exclusion == "base_disabled":
                base.status = "disabled"
            elif exclusion == "segment_disabled":
                segment.enabled = False
            elif exclusion == "document_disabled":
                document.enabled = False
            elif exclusion == "document_not_ready":
                document.status = "failed"
                document.error_message = "派生失败"
            elif exclusion == "stale_segment":
                document.version = 2
            elif exclusion == "stale_summary":
                summary.document_version = 2
            elif exclusion == "wrong_summary_project":
                summary.project_id = uuid.uuid4()
            session.add(summary)

        filters = (KnowledgeMetadataFilter(name="document_name", field_kind="builtin", operator="eq", value="其他文档"),) if exclusion == "metadata" else None
        result = await harness.service.search(_request(project_id, score_threshold=0.95, metadata_filters=filters, debug=True))

        assert result.hits == ()
        assert result.diagnostics.counts.summary_candidates == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_summary_and_content_sources_deduplicate_before_each_base_budget(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, embedding_id, reranker_id = await _seed_single_base(
            harness,
            segments=[(f"大库段落{index}", _vector(0.9 if index < 20 else 0.1)) for index in range(30)],
        )
        async with harness.factory() as session, session.begin():
            base = await session.get(KnowledgeBaseRow, base_id)
            base.summary_index_enabled = True
            for segment in await session.scalars(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_base_id == base_id)):
                session.add(_summary(segment, 1.0 if segment.position > 10 else 0.1))
            other_base = _base_row(project_id, embedding_id, reranker_id, name="小库")
            other_base.summary_index_enabled = True
            session.add(other_base)
            await session.flush()
            document = _document_row(project_id, other_base.id, name="小库文档")
            session.add(document)
            await session.flush()
            segment = _segment_row(document, position=1, content="小库候选", embedding=_vector(0.1))
            session.add(segment)
            await session.flush()
            session.add(_summary(segment))

        result = await harness.service.search(_request(project_id, top_k=1, debug=True))

        assert result.diagnostics.per_base_route_budget == 20
        assert result.diagnostics.counts.semantic_candidates == 21
        assert result.diagnostics.counts.summary_candidates == 21
        sent_documents = harness.client.rerank_calls[0][2]
        assert len(sent_documents) == len(set(sent_documents)) == 21
        assert "小库候选" in sent_documents
        assert all("SUMMARY_ONLY_MARKER" not in document for document in sent_documents)
    finally:
        await harness.engine.dispose()
