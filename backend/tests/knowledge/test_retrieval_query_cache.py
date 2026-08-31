"""M11 query-cache integration against real pgvector and replayed Provider HTTP."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest
from actweave_knowledge import KNOWLEDGE_NOT_FOUND, KnowledgeBaseCreate, KnowledgeError, KnowledgeModule, KnowledgeSettings
from actweave_knowledge.models.client import KnowledgeModelClient
from actweave_knowledge.persistence.models import KnowledgeDocumentRow, KnowledgeSegmentRow
from registry_helpers import registry_model_port
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from test_retrieval import (
    _DEFAULT_OWNER_USER_ID,
    _base_row,
    _harness,
    _query_rows,
    _request,
    _RetrievalHarness,
    _seed_models,
    _seed_project_member,
    _seed_single_base,
)

from app.knowledge.authority import ProjectKnowledgeAuthority
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context


@dataclass
class _CacheHarness:
    retrieval: _RetrievalHarness
    module: KnowledgeModule
    authority: ProjectKnowledgeAuthority
    project_id: UUID
    base_id: UUID
    embedding_id: UUID
    embedding_requests: list[dict[str, Any]]
    rerank_requests: list[dict[str, Any]]


@asynccontextmanager
async def _cache_harness(
    postgres_database_url: str,
    *,
    enabled: bool = True,
    with_reranker: bool = False,
    on_dispatch: Callable[[httpx.Request], Awaitable[None]] | None = None,
) -> AsyncIterator[_CacheHarness]:
    retrieval = await _harness(postgres_database_url)
    try:
        project_id, base_id, embedding_id, _ = await _seed_single_base(
            retrieval,
            segments=[("产品安装方法", [1.0, 0.0, 0.0]), ("产品维护方法", [0.8, 0.6, 0.0])],
            with_reranker=with_reranker,
        )
        async with retrieval.factory() as session, session.begin():
            await _seed_project_member(session, project_id, _DEFAULT_OWNER_USER_ID)
        async with retrieval.factory() as session:
            context = await resolve_project_context(session, _DEFAULT_OWNER_USER_ID, project_id, "query-cache-test")
        authority = ProjectKnowledgeAuthority(context, Capability.SHARED_ASSETS_READ)
        embedding_requests: list[dict[str, Any]] = []
        rerank_requests: list[dict[str, Any]] = []

        async def replay(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if request.url.path.endswith("/embeddings"):
                embedding_requests.append(payload)
                if on_dispatch is not None:
                    await on_dispatch(request)
                return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})
            assert request.url.path.endswith("/rerank")
            rerank_requests.append(payload)
            if on_dispatch is not None:
                await on_dispatch(request)
            return httpx.Response(200, json={"results": [{"index": index, "relevance_score": 1.0 - index * 0.1} for index in range(len(payload["documents"]))]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
            module = KnowledgeModule(
                settings=KnowledgeSettings(query_cache_enabled=enabled),
                session_factory=retrieval.factory,
                model_port=registry_model_port(),
                model_client=KnowledgeModelClient(http=http),
            )
            try:
                yield _CacheHarness(retrieval, module, authority, project_id, base_id, embedding_id, embedding_requests, rerank_requests)
            finally:
                await module.aclose()
    finally:
        await retrieval.engine.dispose()


@pytest.mark.asyncio
async def test_repeated_query_skips_embedding_provider_and_preserves_exact_hits(postgres_database_url: str) -> None:
    async with _cache_harness(postgres_database_url) as harness:
        request = _request(harness.project_id, debug=True)
        cold = await harness.module.search(request, authority=harness.authority)
        assert len(harness.embedding_requests) == 1

        warm = await harness.module.search(request, authority=harness.authority)

        assert len(harness.embedding_requests) == 1
        assert json.dumps([asdict(hit) for hit in cold.hits], default=str, sort_keys=True).encode() == json.dumps([asdict(hit) for hit in warm.hits], default=str, sort_keys=True).encode()
        assert cold.diagnostics is not None and warm.diagnostics is not None
        assert cold.diagnostics.counts.query_embedding_cache_hits == 0
        assert cold.diagnostics.counts.query_embedding_cache_misses == 1
        assert warm.diagnostics.counts.query_embedding_cache_hits == 1
        assert warm.diagnostics.counts.query_embedding_cache_misses == 0


@pytest.mark.asyncio
async def test_disabled_cache_preserves_results_but_always_dispatches_embedding(postgres_database_url: str) -> None:
    async with _cache_harness(postgres_database_url, enabled=False) as harness:
        request = _request(harness.project_id, debug=True)
        first = await harness.module.search(request, authority=harness.authority)
        second = await harness.module.search(request, authority=harness.authority)

        assert first.hits == second.hits
        assert len(harness.embedding_requests) == 2
        for result in (first, second):
            assert result.diagnostics is not None
            assert result.diagnostics.counts.query_embedding_cache_hits == 0
            assert result.diagnostics.counts.query_embedding_cache_misses == 1


async def _revoke_membership(harness: _CacheHarness) -> None:
    async with harness.retrieval.factory() as session, session.begin():
        await session.execute(
            text("UPDATE project_memberships SET status = 'removed', version = version + 1 WHERE project_id = :project_id AND user_id = :user_id"),
            {"project_id": harness.project_id, "user_id": str(harness.authority.actor_user_id)},
        )


async def _assert_only_the_warmup_was_recorded(harness: _CacheHarness) -> None:
    assert len(await _query_rows(harness.retrieval, harness.project_id)) == 1
    async with harness.retrieval.factory() as session:
        segments = list((await session.scalars(select(KnowledgeSegmentRow))).all())
        documents = list((await session.scalars(select(KnowledgeDocumentRow))).all())
    assert all(segment.hit_count == 1 for segment in segments)
    # One document supplied two returned segments during warmup.
    assert all(document.hit_count == 2 for document in documents)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_at_check", [2, 3], ids=["recall_transaction", "reranker_dispatch"])
async def test_warm_cache_retains_live_recall_and_reranker_authorization(postgres_database_url: str, revoke_at_check: int) -> None:
    async with _cache_harness(postgres_database_url, with_reranker=True) as harness:
        request = _request(harness.project_id)
        await harness.module.search(request, authority=harness.authority)

        class RevokingAuthority:
            project_id = harness.project_id
            actor_user_id = _DEFAULT_OWNER_USER_ID
            calls = 0

            async def revalidate(self, session: AsyncSession) -> None:
                self.calls += 1
                if self.calls == revoke_at_check:
                    await _revoke_membership(harness)
                await harness.authority.revalidate(session)

        authority = RevokingAuthority()
        with pytest.raises(KnowledgeError) as error:
            await harness.module.search(request, authority=authority)

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert authority.calls == revoke_at_check
        # The hot embedding cache cannot disclose any new text to a Provider.
        assert len(harness.embedding_requests) == len(harness.rerank_requests) == 1
        await _assert_only_the_warmup_was_recorded(harness)


@pytest.mark.asyncio
async def test_warm_cache_retains_final_review_after_reranker_revocation(postgres_database_url: str) -> None:
    revoke = False

    async def on_dispatch(request: httpx.Request) -> None:
        if revoke and request.url.path.endswith("/rerank"):
            await _revoke_membership(harness)

    async with _cache_harness(postgres_database_url, with_reranker=True, on_dispatch=on_dispatch) as harness:
        request = _request(harness.project_id)
        await harness.module.search(request, authority=harness.authority)
        revoke = True

        with pytest.raises(KnowledgeError) as error:
            await harness.module.search(request, authority=harness.authority)

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert len(harness.embedding_requests) == 1
        assert len(harness.rerank_requests) == 2
        await _assert_only_the_warmup_was_recorded(harness)


@pytest.mark.asyncio
async def test_concurrent_cold_queries_dispatch_independently_then_both_reuse_cache(postgres_database_url: str) -> None:
    both_dispatched = asyncio.Event()
    dispatches = 0

    async def on_dispatch(request: httpx.Request) -> None:
        nonlocal dispatches
        assert request.url.path.endswith("/embeddings")
        dispatches += 1
        if dispatches == 2:
            both_dispatched.set()
        await both_dispatched.wait()

    async with _cache_harness(postgres_database_url, on_dispatch=on_dispatch) as harness:
        request = _request(harness.project_id, debug=True)
        cold = await asyncio.wait_for(
            asyncio.gather(*(harness.module.search(request, authority=harness.authority) for _ in range(2))),
            timeout=5,
        )
        assert dispatches == 2
        assert cold[0].hits == cold[1].hits
        for result in cold:
            assert result.diagnostics is not None
            assert result.diagnostics.counts.query_embedding_cache_hits == 0
            assert result.diagnostics.counts.query_embedding_cache_misses == 1

        warm = await asyncio.wait_for(
            asyncio.gather(*(harness.module.search(request, authority=harness.authority) for _ in range(2))),
            timeout=5,
        )
        assert dispatches == 2
        for result in warm:
            assert result.hits == cold[0].hits
            assert result.diagnostics is not None
            assert result.diagnostics.counts.query_embedding_cache_hits == 1
            assert result.diagnostics.counts.query_embedding_cache_misses == 0


@pytest.mark.asyncio
async def test_cache_diagnostics_count_distinct_embedding_models_not_model_groups(postgres_database_url: str) -> None:
    async with _cache_harness(postgres_database_url) as harness:
        async with harness.retrieval.factory() as session, session.begin():
            second_embedding, reranker = await _seed_models(session)
            session.add_all(
                [
                    _base_row(harness.project_id, harness.embedding_id, reranker, name="同模型不同分组"),
                    _base_row(harness.project_id, second_embedding, None, name="另一个向量空间"),
                ]
            )
        request = _request(harness.project_id, debug=True)

        cold = await harness.module.search(request, authority=harness.authority)
        warm = await harness.module.search(request, authority=harness.authority)

        assert len(harness.embedding_requests) == 2
        assert cold.hits == warm.hits
        assert cold.diagnostics is not None and warm.diagnostics is not None
        assert cold.diagnostics.target_base_count == warm.diagnostics.target_base_count == 3
        assert cold.diagnostics.counts.query_embedding_cache_misses == 2
        assert cold.diagnostics.counts.query_embedding_cache_hits == 0
        assert warm.diagnostics.counts.query_embedding_cache_misses == 0
        assert warm.diagnostics.counts.query_embedding_cache_hits == 2


@pytest.mark.asyncio
async def test_rebuild_to_another_embedding_model_does_not_reuse_the_old_vector(postgres_database_url: str) -> None:
    async with _cache_harness(postgres_database_url) as harness:
        async with harness.retrieval.factory() as session, session.begin():
            other_embedding_id, _ = await _seed_models(session)
        async with harness.retrieval.factory() as session:
            context = await resolve_project_context(session, _DEFAULT_OWNER_USER_ID, harness.project_id, "cache-rebuild")
        edit_authority = ProjectKnowledgeAuthority(context, Capability.SHARED_ASSETS_EDIT)
        # An empty base needs no document worker to finish its model rebuild.
        base = await harness.module.create_knowledge_base(
            harness.project_id,
            KnowledgeBaseCreate(name="重建缓存隔离", embedding_model_id=harness.embedding_id),
            authority=edit_authority,
        )
        request = _request(harness.project_id, knowledge_base_ids=(base.id,), debug=True)
        before = await harness.module.search(request, authority=harness.authority)
        assert len(harness.embedding_requests) == 1

        rebuilt = await harness.module.rebuild_knowledge_base(
            harness.project_id,
            base.id,
            embedding_model_id=other_embedding_id,
            authority=edit_authority,
        )
        after = await harness.module.search(request, authority=harness.authority)

        assert rebuilt.base.embedding_model_id == other_embedding_id
        assert rebuilt.accepted_document_count == 0
        assert len(harness.embedding_requests) == 2
        assert harness.embedding_requests[0]["model"] != harness.embedding_requests[1]["model"]
        assert before.hits == after.hits == ()
        assert after.diagnostics is not None
        assert after.diagnostics.counts.query_embedding_cache_hits == 0
        assert after.diagnostics.counts.query_embedding_cache_misses == 1
