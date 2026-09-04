"""Per-dimension HNSW partial indexes and the index-eligible recall shape.

The embedding columns carry any dimension, so pgvector cannot index them
directly. Schema V1 declares one partial expression HNSW index per common
dimension on every vector table, and recall orders each base's lateral branch
by ``embedding::vector(D) <=> query`` under ``vector_dims(embedding) = D`` so
the planner can pick that index. These gates run the real schema and the real
statement builders against PostgreSQL (``EXPLAIN``), not a hand-written query.
"""

from __future__ import annotations

import uuid

import pytest
from actweave_knowledge.persistence.models import KNOWLEDGE_HNSW_INDEXED_DIMENSIONS
from actweave_knowledge.retrieval.service import KnowledgeSearchService
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from test_retrieval import _harness, _seed_single_base


@pytest.mark.asyncio
async def test_general_recall_statement_is_planned_on_the_hnsw_index(postgres_database_url: str) -> None:
    """The lateral top-C branch of general recall must be an HNSW index scan
    for an indexed dimension; the same statement for an unindexed dimension
    still plans (as a sorted scan) instead of failing."""

    harness = await _harness(postgres_database_url)
    try:
        dimension = KNOWLEDGE_HNSW_INDEXED_DIMENSIONS[0]
        vectors = [[1.0 if index == position else 0.0 for index in range(dimension)] for position in range(3)]
        project_id, base_id, embedding_id, _ = await _seed_single_base(
            harness,
            segments=[(f"段落{position}", vector) for position, vector in enumerate(vectors, start=1)],
            dimension=dimension,
            with_reranker=False,
        )
        query_vector = [1.0] + [0.0] * (dimension - 1)
        service: KnowledgeSearchService = harness.service
        async with harness.factory() as session, session.begin():
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            statement = service._general_statement(  # noqa: SLF001 - plan inspection of the real builder
                project_id=project_id,
                embedding_model_id=embedding_id,
                dimension=dimension,
                base_ids=[base_id],
                query_vector=query_vector,
                per_base_budget=20,
                metadata_filters=(),
            )
            compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
            plan = "\n".join(row[0] for row in (await session.execute(text(f"EXPLAIN (COSTS OFF) {compiled}"))).all())
        assert f"ix_knowledge_segments_embedding_hnsw_{dimension}" in plan, plan
        assert "Nested Loop" in plan and "Limit" in plan

        # The search itself still returns exact results on this tiny set.
        from test_retrieval import _request

        result = await harness.service.search(_request(project_id, score_threshold=0.0, top_k=3))
        assert [hit.citation.snippet for hit in result.hits][0] == "段落1"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_unindexed_dimension_still_recalls(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("三维段落", [1.0, 0.0, 0.0]), ("次要段落", [0.0, 1.0, 0.0])],
            dimension=3,
            with_reranker=False,
        )
        assert 3 not in KNOWLEDGE_HNSW_INDEXED_DIMENSIONS
        result = await harness.service.search(_request_for(project_id))
        assert [hit.citation.snippet for hit in result.hits] == ["三维段落"]
    finally:
        await harness.engine.dispose()


def _request_for(project_id: uuid.UUID):  # noqa: ANN202
    from test_retrieval import _request

    return _request(project_id, score_threshold=0.5, top_k=2)
