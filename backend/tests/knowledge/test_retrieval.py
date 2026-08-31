"""M5 gates: two-stage retrieval (cosine recall + optional rerank) and the search API.

Service tests run real pgvector cosine SQL against the installed Schema V1
snapshot with a scripted model client and the production
``RegistryKnowledgeModelPort`` over seeded ``model_providers`` rows, so recall
filters, (embedding, reranker) grouping, optional rerank-free cosine scoring,
thresholds and ordering are all exercised for real. Two tests drive the real
``KnowledgeModelClient`` through a mock HTTP transport to prove cross-batch
rerank inside a search. HTTP tests pin the route contract over ASGI.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from base64 import b64encode
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_RERANK_FAILED,
    KNOWLEDGE_SEARCH_FAILED,
    KNOWLEDGE_STRATEGY_VERSION,
    KnowledgeCitation,
    KnowledgeError,
    KnowledgeMetadataFilter,
    KnowledgeQueryView,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
)
from actweave_knowledge.models.client import KnowledgeModelClient, RerankScore
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeMetadataFieldRow,
    KnowledgeQueryRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
)
from actweave_knowledge.retrieval import (
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_TOP_K,
    KnowledgeSearchService,
    calculate_candidate_k,
)
from fastapi import FastAPI
from registry_helpers import (
    TEST_REGISTRY_API_KEY,
    registry_model_port,
    registry_secret_key,
)
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge import gateway
from app.knowledge.model_port import RegistryKnowledgeModelPort
from app.model_registry.secrets import protect_provider_api_key
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.model_registry import ModelProviderModelRow, ModelProviderRow
from deerflow.secrets import SecretKey

_DEFAULT_OWNER_USER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")

# ---------------------------------------------------------------------------
# Fakes and harness
# ---------------------------------------------------------------------------


class _ScriptedClient:
    """KnowledgeModelClient double: fixed query vectors, scriptable rerank.

    Keys are registry model ids — ``query_vectors`` by embedding model id,
    ``rerank_scripts`` by reranker model id.
    """

    def __init__(self) -> None:
        self.query_vectors: dict[uuid.UUID, list[float]] = {}
        self.embed_calls: list[tuple[uuid.UUID, list[str]]] = []
        self.rerank_calls: list[tuple[uuid.UUID, str, list[str], int]] = []
        self.rerank_scripts: dict[uuid.UUID, Callable[[list[str], int], list[RerankScore]]] = {}
        self.embed_error: KnowledgeError | None = None
        self.rerank_error: KnowledgeError | None = None

    async def embed(self, material, texts: list[str], *, batch_guard=None, on_batch_verified=None) -> list[list[float]]:  # noqa: ANN001
        # The real client runs the guard before dispatching; a guard failure
        # therefore means the call never reached the provider.
        if batch_guard is not None:
            await batch_guard()
        self.embed_calls.append((material.model_id, list(texts)))
        if self.embed_error is not None:
            raise self.embed_error
        if on_batch_verified is not None:
            await on_batch_verified(len(texts))
        return [list(self.query_vectors[material.model_id]) for _ in texts]

    async def rerank(self, material, query: str, documents: list[str], top_n: int, *, batch_guard=None) -> list[RerankScore]:  # noqa: ANN001
        if batch_guard is not None:
            await batch_guard()
        self.rerank_calls.append((material.model_id, query, list(documents), top_n))
        if self.rerank_error is not None:
            raise self.rerank_error
        script = self.rerank_scripts.get(material.model_id)
        if script is not None:
            return script(documents, top_n)
        # Default: keep the submitted (cosine) order with descending scores.
        return [RerankScore(index=index, score=round(1.0 - index * 0.05, 4)) for index in range(min(top_n, len(documents)))]


class _RetrievalHarness:
    def __init__(self, engine, factory, client: _ScriptedClient, service: KnowledgeSearchService) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.client = client
        self.service = service


async def _harness(postgres_database_url: str) -> _RetrievalHarness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    client = _ScriptedClient()
    service = KnowledgeSearchService(
        session_factory=factory,
        client=client,  # type: ignore[arg-type]
        model_port=registry_model_port(),
    )
    return _RetrievalHarness(engine, factory, client, service)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def _seed_project(session: AsyncSession, label: str) -> uuid.UUID:
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
        ),
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m5_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m5-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _seed_project_member(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    membership_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
        ),
        {
            "user_id": str(user_id),
            "email": f"{user_id.hex}@example.invalid",
            "username": f"m5_member_{user_id.hex[:8]}",
        },
    )
    await session.execute(
        text(
            """INSERT INTO project_memberships (
                   id, project_id, user_id, role, status, version
               ) VALUES (
                   :membership_id, :project_id, :user_id,
                   'admin', 'active', 1
               )"""
        ),
        {
            "membership_id": membership_id,
            "project_id": project_id,
            "user_id": str(user_id),
        },
    )
    return membership_id


async def _seed_models(
    session: AsyncSession,
    *,
    dimension: int = 3,
    rerank_max_batch: int = 32,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Provider + embedding + rerank registry rows inside the caller's transaction.

    The API key is encrypted with the deterministic test SecretKey so the
    production port can decrypt it. Returns (embedding_id, rerank_id).
    """

    provider_id = uuid.uuid4()
    envelope = protect_provider_api_key(
        provider_id=provider_id,
        base_url="https://provider.invalid/v1",
        api_key=TEST_REGISTRY_API_KEY,
        key=registry_secret_key(),
    )
    session.add(
        ModelProviderRow(
            id=provider_id,
            name=f"provider-{provider_id.hex[:12]}",
            base_url="https://provider.invalid/v1",
            request_timeout_seconds=30,
            api_key_nonce=envelope.nonce,
            api_key_ciphertext=envelope.ciphertext,
        )
    )
    # No ORM relationship links the two rows, so flush the Provider first.
    await session.flush()
    embedding_id = uuid.uuid4()
    rerank_id = uuid.uuid4()
    session.add(
        ModelProviderModelRow(
            id=embedding_id,
            provider_id=provider_id,
            model_type="embedding",
            model_name=f"embed-{embedding_id.hex[:12]}",
            embedding_dimension=dimension,
            max_batch=64,
            status="active",
        )
    )
    session.add(
        ModelProviderModelRow(
            id=rerank_id,
            provider_id=provider_id,
            model_type="rerank",
            model_name=f"rerank-{rerank_id.hex[:12]}",
            embedding_dimension=None,
            max_batch=rerank_max_batch,
            status="active",
        )
    )
    await session.flush()
    return embedding_id, rerank_id


def _base_row(
    project_id: uuid.UUID,
    embedding_model_id: uuid.UUID,
    reranker_model_id: uuid.UUID | None,
    *,
    name: str,
    status: str = "active",
    default_top_k: int | None = None,
    default_score_threshold: float | None = None,
) -> KnowledgeBaseRow:
    row = KnowledgeBaseRow(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        embedding_model_id=embedding_model_id,
        reranker_model_id=reranker_model_id,
        status=status,
    )
    # None keeps the schema defaults (4 / 0.2).
    if default_top_k is not None:
        row.default_top_k = default_top_k
    if default_score_threshold is not None:
        row.default_score_threshold = default_score_threshold
    return row


def _document_row(project_id: uuid.UUID, base_id: uuid.UUID, *, name: str, status: str = "ready", version: int = 1) -> KnowledgeDocumentRow:
    document_id = uuid.uuid4()
    return KnowledgeDocumentRow(
        id=document_id,
        project_id=project_id,
        knowledge_base_id=base_id,
        name=name,
        original_name=f"{name}.md",
        storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.md",
        size_bytes=64,
        status=status,
        version=version,
        chunk_size=1000,
        chunk_overlap=100,
    )


def _segment_row(
    document: KnowledgeDocumentRow,
    *,
    position: int,
    content: str,
    embedding: list[float] | None,
    document_version: int | None = None,
    source_position: dict[str, Any] | None = None,
) -> KnowledgeSegmentRow:
    return KnowledgeSegmentRow(
        id=uuid.uuid4(),
        project_id=document.project_id,
        knowledge_base_id=document.knowledge_base_id,
        knowledge_document_id=document.id,
        document_version=document_version if document_version is not None else document.version,
        position=position,
        content=content,
        source_position=source_position or {},
        embedding=embedding,
    )


def _child_row(segment: KnowledgeSegmentRow, *, position: int, content: str, embedding: list[float]) -> KnowledgeSegmentChildRow:
    return KnowledgeSegmentChildRow(
        id=uuid.uuid4(),
        project_id=segment.project_id,
        knowledge_base_id=segment.knowledge_base_id,
        knowledge_document_id=segment.knowledge_document_id,
        knowledge_segment_id=segment.id,
        document_version=segment.document_version,
        position=position,
        content=content,
        word_count=len(content),
        embedding=embedding,
    )


async def _seed_parent_child_document(
    session: AsyncSession,
    project_id: uuid.UUID,
    base_id: uuid.UUID,
    *,
    name: str,
    parents: list[tuple[str, list[tuple[str, list[float]]]]],
) -> KnowledgeDocumentRow:
    """One ready parent_child document: NULL-embedding parents plus vectored children."""

    document = _document_row(project_id, base_id, name=name)
    document.chunking_mode = "parent_child"
    session.add(document)
    await session.flush()
    for position, (content, children) in enumerate(parents, start=1):
        segment = _segment_row(document, position=position, content=content, embedding=None)
        session.add(segment)
        await session.flush()
        for child_position, (child_content, child_embedding) in enumerate(children, start=1):
            session.add(_child_row(segment, position=child_position, content=child_content, embedding=child_embedding))
    return document


async def _seed_single_base(
    harness: _RetrievalHarness,
    *,
    segments: list[tuple[str, list[float]]],
    dimension: int = 3,
    with_reranker: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Project + registry models + base + one ready document with ``segments``.

    Returns (project_id, base_id, embedding_model_id, rerank_model_id). The
    rerank model is always seeded; ``with_reranker=False`` leaves the base
    unbound so its group scores by raw cosine.
    """

    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        embedding_id, rerank_id = await _seed_models(session, dimension=dimension)
        base = _base_row(
            project_id,
            embedding_id,
            rerank_id if with_reranker else None,
            name=f"base-{uuid.uuid4().hex[:6]}",
        )
        session.add(base)
        await session.flush()
        document = _document_row(project_id, base.id, name="手册")
        session.add(document)
        await session.flush()
        for index, (content, embedding) in enumerate(segments, start=1):
            session.add(_segment_row(document, position=index, content=content, embedding=embedding))
    harness.client.query_vectors[embedding_id] = [1.0] + [0.0] * (dimension - 1)
    return project_id, base.id, embedding_id, rerank_id


def _request(project_id: uuid.UUID, **overrides: Any) -> KnowledgeSearchRequest:
    values: dict[str, Any] = {
        "project_id": project_id,
        "owner_user_id": _DEFAULT_OWNER_USER_ID,
        "query": "如何安装产品",
    }
    values.update(overrides)
    return KnowledgeSearchRequest(**values)


# ---------------------------------------------------------------------------
# candidate_k and request validation
# ---------------------------------------------------------------------------


def test_calculate_candidate_k_applies_floor_scale_and_ceiling() -> None:
    assert calculate_candidate_k(1) == 20
    assert calculate_candidate_k(4) == 20
    assert calculate_candidate_k(5) == 25
    assert calculate_candidate_k(20) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"query": "   "}, "query 不能为空"),
        ({"query": "问" * 2001}, "2000"),
        ({"top_k": 0}, "top_k"),
        ({"top_k": 21}, "top_k"),
        ({"top_k": True}, "top_k"),
        ({"score_threshold": -0.1}, "score_threshold"),
        ({"score_threshold": 1.1}, "score_threshold"),
        ({"source": "webhook"}, "source"),
        ({"metadata_filters": (KnowledgeMetadataFilter(name="  ", operator="eq", value="x"),)}, "name"),
        # The Agent tool maps missing dict keys to None fields.
        ({"metadata_filters": (KnowledgeMetadataFilter(name=None, operator="eq", value="x"),)}, "name"),  # type: ignore[arg-type]
        ({"metadata_filters": (KnowledgeMetadataFilter(name="f", operator="like", value="x"),)}, "operator"),  # type: ignore[arg-type]
        ({"metadata_filters": (KnowledgeMetadataFilter(name="f", operator="contains", value=5),)}, "contains"),
        ({"metadata_filters": (KnowledgeMetadataFilter(name="f", operator="gte", value="5"),)}, "gte"),
        ({"metadata_filters": (KnowledgeMetadataFilter(name="f", operator="eq", value=True),)}, "eq"),
        (
            {"metadata_filters": tuple(KnowledgeMetadataFilter(name=f"f{index}", operator="eq", value=1) for index in range(11))},
            "10",
        ),
        # T6: field_kind is a closed vocabulary; builtin names, operators and
        # value types are a frozen contract, so mistakes fail fast instead of
        # silently matching nothing.
        ({"metadata_filters": (KnowledgeMetadataFilter(name="f", operator="eq", value="x", field_kind="magic"),)}, "field_kind"),  # type: ignore[arg-type]
        ({"metadata_filters": (KnowledgeMetadataFilter(name="uploader", operator="eq", value="x", field_kind="builtin"),)}, "内建"),
        ({"metadata_filters": (KnowledgeMetadataFilter(name="document_name", operator="gte", value=1, field_kind="builtin"),)}, "不支持"),
        ({"metadata_filters": (KnowledgeMetadataFilter(name="uploaded_at", operator="contains", value="x", field_kind="builtin"),)}, "不支持"),
        ({"metadata_filters": (KnowledgeMetadataFilter(name="uploaded_at", operator="eq", value="昨天", field_kind="builtin"),)}, "数字"),
        ({"metadata_filters": (KnowledgeMetadataFilter(name="document_name", operator="eq", value=5, field_kind="builtin"),)}, "字符串"),
    ],
)
async def test_search_rejects_invalid_requests(postgres_database_url: str, overrides: dict[str, Any], fragment: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(uuid.uuid4(), **overrides))
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert fragment in error.value.message
        assert harness.client.embed_calls == []
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Two-stage behavior against real pgvector SQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_base_search_recalls_by_cosine_then_returns_reranked_top_k(postgres_database_url: str) -> None:
    """The reranker, not cosine, decides the final order and the citation score."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, embedding_id, rerank_id = await _seed_single_base(
            harness,
            segments=[
                ("完全匹配的段落", [1.0, 0.0, 0.0]),
                ("比较接近的段落", [0.8, 0.6, 0.0]),
                ("毫不相关的段落", [0.0, 1.0, 0.0]),
            ],
        )
        # Rerank prefers the cosine-worst candidate.
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [
            RerankScore(index=len(documents) - 1, score=0.95),
            RerankScore(index=0, score=0.60),
        ][:top_n]

        result = await harness.service.search(_request(project_id, top_k=2))

        assert [(citation.snippet, citation.score) for citation in result.citations] == [
            ("毫不相关的段落", 0.95),
            ("完全匹配的段落", 0.60),
        ]
        assert harness.client.embed_calls == [(embedding_id, ["如何安装产品"])]
        (_, _, submitted, top_n) = harness.client.rerank_calls[0]
        assert submitted == ["完全匹配的段落", "比较接近的段落", "毫不相关的段落"]  # cosine order
        # Every recalled candidate is scored so per-base thresholds can filter
        # before the global top_k cut.
        assert top_n == 3
        first = result.citations[0]
        assert first.knowledge_base_id == base_id
        assert first.knowledge_base_name.startswith("base-")
        assert first.document_name == "手册"
        assert first.segment_position == 3
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_returns_empty_without_bases_and_skips_rerank_without_candidates(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        # No bases at all: no model calls.
        async with harness.factory() as session, session.begin():
            empty_project = await _seed_project(session, uuid.uuid4().hex[:8])
        result = await harness.service.search(_request(empty_project))
        assert result.citations == ()
        assert harness.client.embed_calls == []

        # A base whose only document has no current segments: embed runs, rerank must not.
        project_id, _, embedding_id, _ = await _seed_single_base(harness, segments=[])
        result = await harness.service.search(_request(project_id))
        assert result.citations == ()
        assert harness.client.embed_calls == [(embedding_id, ["如何安装产品"])]
        assert harness.client.rerank_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_excludes_unconfigured_bases_before_model_loading_and_budgeting(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, configured_id, embedding_id, _ = await _seed_single_base(harness, segments=[("安装说明", [1.0, 0.0, 0.0])])
        unconfigured_id = uuid.uuid4()
        async with harness.factory() as session, session.begin():
            session.add(KnowledgeBaseRow(id=unconfigured_id, project_id=project_id, name="待配置", default_top_k=20, retrieval_mode="hybrid"))

        empty = await harness.service.search(_request(project_id, knowledge_base_ids=(unconfigured_id,), debug=True))
        assert empty.citations == ()
        assert empty.diagnostics is not None
        assert empty.diagnostics.target_base_count == 0
        assert empty.diagnostics.empty_reason == "not_ready"
        assert harness.client.embed_calls == []
        assert harness.client.rerank_calls == []

        result = await harness.service.search(_request(project_id, debug=True))
        assert [citation.knowledge_base_id for citation in result.citations] == [configured_id]
        assert result.diagnostics is not None
        assert result.diagnostics.target_base_count == 1
        assert result.diagnostics.effective_top_k == 4
        assert result.diagnostics.retrieval_mode == "semantic"
        assert harness.client.embed_calls == [(embedding_id, ["如何安装产品"])]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_only_sees_active_bases_ready_documents_and_current_versions(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)

            active = _base_row(project_id, embedding_id, rerank_id, name="active-base")
            disabled = _base_row(project_id, embedding_id, rerank_id, name="disabled-base", status="disabled")
            deleting = _base_row(project_id, embedding_id, rerank_id, name="deleting-base", status="deleting")
            session.add_all([active, disabled, deleting])
            await session.flush()

            ready = _document_row(project_id, active.id, name="ready", version=2)
            processing = _document_row(project_id, active.id, name="processing", status="processing")
            failed = _document_row(project_id, active.id, name="failed", status="failed")
            failed.error_message = "解析失败"
            disabled_doc = _document_row(project_id, disabled.id, name="disabled-doc")
            deleting_doc = _document_row(project_id, deleting.id, name="deleting-doc")
            session.add_all([ready, processing, failed, disabled_doc, deleting_doc])
            await session.flush()

            vector = [1.0, 0.0, 0.0]
            session.add_all(
                [
                    _segment_row(ready, position=1, content="当前版本段落", embedding=vector),
                    _segment_row(ready, position=2, content="旧版本段落", embedding=vector, document_version=1),
                    _segment_row(processing, position=1, content="处理中段落", embedding=vector),
                    _segment_row(failed, position=1, content="失败文档段落", embedding=vector),
                    _segment_row(disabled_doc, position=1, content="禁用库段落", embedding=vector),
                    _segment_row(deleting_doc, position=1, content="删除中库段落", embedding=vector),
                ]
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id))

        assert [citation.snippet for citation in result.citations] == ["当前版本段落"]
        (_, _, submitted, _) = harness.client.rerank_calls[0]
        assert submitted == ["当前版本段落"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_excludes_disabled_documents_and_disabled_segments(postgres_database_url: str) -> None:
    """K1 governance: the enabled switches gate recall without touching vectors."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="governed-base")
            session.add(base)
            await session.flush()

            visible = _document_row(project_id, base.id, name="visible")
            disabled_document = _document_row(project_id, base.id, name="disabled")
            disabled_document.enabled = False
            session.add_all([visible, disabled_document])
            await session.flush()

            vector = [1.0, 0.0, 0.0]
            enabled_segment = _segment_row(visible, position=1, content="可检索段落", embedding=vector)
            disabled_segment = _segment_row(visible, position=2, content="被禁用的段落", embedding=vector)
            disabled_segment.enabled = False
            session.add_all(
                [
                    enabled_segment,
                    disabled_segment,
                    _segment_row(disabled_document, position=1, content="禁用文档的段落", embedding=vector),
                ]
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id))
        assert [citation.snippet for citation in result.citations] == ["可检索段落"]

        # Re-enabling restores retrievability with the original vectors.
        async with harness.factory() as session, session.begin():
            await session.execute(text("UPDATE knowledge_documents SET enabled = true WHERE id = :id"), {"id": str(disabled_document.id)})
            await session.execute(text("UPDATE knowledge_segments SET enabled = true WHERE id = :id"), {"id": str(disabled_segment.id)})
        result = await harness.service.search(_request(project_id))
        assert sorted(citation.snippet for citation in result.citations) == [
            "可检索段落",
            "禁用文档的段落",
            "被禁用的段落",
        ]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_with_explicit_base_ids_uses_only_their_active_subset(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, embedding_id, rerank_id = await _seed_single_base(
            harness,
            segments=[("目标库段落", [1.0, 0.0, 0.0])],
        )
        other_project, other_base, _other_embedding, _other_rerank = await _seed_single_base(
            harness,
            segments=[("外项目段落", [1.0, 0.0, 0.0])],
        )
        async with harness.factory() as session, session.begin():
            disabled = _base_row(project_id, embedding_id, rerank_id, name="disabled-pick", status="disabled")
            session.add(disabled)

        # Foreign-project and disabled ids are silently ignored by scope filters.
        result = await harness.service.search(_request(project_id, knowledge_base_ids=(base_id, disabled.id, other_base)))
        assert [citation.snippet for citation in result.citations] == ["目标库段落"]
        assert [call[0] for call in harness.client.embed_calls] == [embedding_id]

        # An explicitly empty selection searches nothing.
        harness.client.embed_calls.clear()
        result = await harness.service.search(_request(project_id, knowledge_base_ids=()))
        assert result.citations == ()
        assert harness.client.embed_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_groups_bases_by_model_pair_and_merges_globally(postgres_database_url: str) -> None:
    """Different dimensions never meet in one SQL; results merge to a global top-k."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            small_embedding, small_rerank = await _seed_models(session, dimension=3)
            large_embedding, large_rerank = await _seed_models(session, dimension=4)
            small_base = _base_row(project_id, small_embedding, small_rerank, name="small-base")
            large_base = _base_row(project_id, large_embedding, large_rerank, name="large-base")
            session.add_all([small_base, large_base])
            await session.flush()
            small_doc = _document_row(project_id, small_base.id, name="小维度文档")
            large_doc = _document_row(project_id, large_base.id, name="大维度文档")
            session.add_all([small_doc, large_doc])
            await session.flush()
            session.add_all(
                [
                    _segment_row(small_doc, position=1, content="小维度段落", embedding=[1.0, 0.0, 0.0]),
                    _segment_row(large_doc, position=1, content="大维度段落", embedding=[1.0, 0.0, 0.0, 0.0]),
                ]
            )
        harness.client.query_vectors[small_embedding] = [1.0, 0.0, 0.0]
        harness.client.query_vectors[large_embedding] = [1.0, 0.0, 0.0, 0.0]
        harness.client.rerank_scripts[small_rerank] = lambda documents, top_n: [RerankScore(index=0, score=0.4)]
        harness.client.rerank_scripts[large_rerank] = lambda documents, top_n: [RerankScore(index=0, score=0.9)]

        result = await harness.service.search(_request(project_id, top_k=2))

        # Two different rerankers are two score domains (T7): each candidate
        # is rank 1 at home, so both fuse to 61/2 * 1/61 = 0.5 — the raw 0.9
        # and 0.4 are never compared numerically across models.
        assert {citation.snippet for citation in result.citations} == {"大维度段落", "小维度段落"}
        assert all(citation.score == pytest.approx(0.5) for citation in result.citations)
        assert all(citation.score_kind == "rank_fusion" for citation in result.citations)
        assert {hit.citation.snippet: hit.local_score for hit in result.hits} == {"大维度段落": 0.9, "小维度段落": 0.4}
        assert sorted(call[0] for call in harness.client.embed_calls) == sorted([small_embedding, large_embedding])
        assert len(harness.client.rerank_calls) == 2
    finally:
        await harness.engine.dispose()


async def _seed_two_model_groups(harness: _RetrievalHarness) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Two Providers (one embedding + one rerank each), one base per pair.

    Returns (project_id, small_embedding, small_rerank, large_embedding, large_rerank).
    """

    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        small_embedding, small_rerank = await _seed_models(session, dimension=3)
        large_embedding, large_rerank = await _seed_models(session, dimension=4)
        small_base = _base_row(project_id, small_embedding, small_rerank, name="small-base")
        large_base = _base_row(project_id, large_embedding, large_rerank, name="large-base")
        session.add_all([small_base, large_base])
        await session.flush()
        small_doc = _document_row(project_id, small_base.id, name="小维度文档")
        large_doc = _document_row(project_id, large_base.id, name="大维度文档")
        session.add_all([small_doc, large_doc])
        await session.flush()
        session.add_all(
            [
                _segment_row(small_doc, position=1, content="小一", embedding=[1.0, 0.0, 0.0]),
                _segment_row(small_doc, position=2, content="小二", embedding=[0.9, 0.1, 0.0]),
                _segment_row(large_doc, position=1, content="大一", embedding=[1.0, 0.0, 0.0, 0.0]),
                _segment_row(large_doc, position=2, content="大二", embedding=[0.9, 0.1, 0.0, 0.0]),
            ]
        )
    harness.client.query_vectors[small_embedding] = [1.0, 0.0, 0.0]
    harness.client.query_vectors[large_embedding] = [1.0, 0.0, 0.0, 0.0]
    return project_id, small_embedding, small_rerank, large_embedding, large_rerank


@pytest.mark.asyncio
async def test_global_top_k_truncates_across_model_groups(postgres_database_url: str) -> None:
    """Each domain ranks its own candidates; the fused merge still cuts to a
    global top_k, keeping both domains' first places over any second place."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, small_rerank, _, large_rerank = await _seed_two_model_groups(harness)
        harness.client.rerank_scripts[small_rerank] = lambda documents, top_n: [
            RerankScore(index=0, score=0.8),
            RerankScore(index=1, score=0.5),
        ][:top_n]
        harness.client.rerank_scripts[large_rerank] = lambda documents, top_n: [
            RerankScore(index=0, score=0.9),
            RerankScore(index=1, score=0.7),
        ][:top_n]

        result = await harness.service.search(_request(project_id, top_k=2))

        # 大一 and 小一 are each rank 1 in their own domain (fused 0.5); 大二
        # and 小二 are rank 2 (fused 61/2/62) and fall past the global top_k.
        assert {citation.snippet for citation in result.citations} == {"大一", "小一"}
        assert all(citation.score == pytest.approx(0.5) for citation in result.citations)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["embedding", "rerank"])
async def test_search_fails_when_a_bound_model_is_disabled(postgres_database_url: str, which: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, embedding_id, rerank_id = await _seed_single_base(harness, segments=[("段落", [1.0, 0.0, 0.0])])
        disabled_id = embedding_id if which == "embedding" else rerank_id
        async with harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE model_provider_models SET status = 'disabled' WHERE id = :id"),
                {"id": disabled_id},
            )

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))
        assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
        assert harness.client.embed_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_one_disabled_model_fails_the_search_even_with_a_healthy_group(postgres_database_url: str) -> None:
    """A disabled group's bases must not silently vanish from a merged result."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, small_embedding, _, _, _ = await _seed_two_model_groups(harness)
        async with harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE model_provider_models SET status = 'disabled' WHERE id = :id"),
                {"id": small_embedding},
            )

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))
        assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
        # Fails before any provider spend, including for the healthy group.
        assert harness.client.embed_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_api_key_materialization_failure_maps_to_model_unavailable(postgres_database_url: str) -> None:
    """Decrypt failures must stay inside the five-code search error contract."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("段落", [1.0, 0.0, 0.0])],
        )
        # A port holding a different SecretKey cannot open the seeded envelope.
        previous = os.environ.get("ACT_WEAVE_SECRET_KEY")
        os.environ["ACT_WEAVE_SECRET_KEY"] = b64encode(b"x" * 32).decode("ascii")
        try:
            wrong_key_port = RegistryKnowledgeModelPort(secret_key=SecretKey.from_environment())
        finally:
            if previous is None:
                del os.environ["ACT_WEAVE_SECRET_KEY"]
            else:
                os.environ["ACT_WEAVE_SECRET_KEY"] = previous
        service = KnowledgeSearchService(
            session_factory=harness.factory,
            client=harness.client,  # type: ignore[arg-type]
            model_port=wrong_key_port,
        )

        with pytest.raises(KnowledgeError) as error:
            await service.search(_request(project_id))
        assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
        assert harness.client.embed_calls == []
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Optional reranker: rerank-free groups score by raw cosine (M9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_free_base_returns_cosine_scores_and_never_calls_the_reranker(postgres_database_url: str) -> None:
    """Without a bound reranker the final score is the raw cosine similarity."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[
                ("完全对齐", [1.0, 0.0, 0.0]),
                ("比较接近", [0.8, 0.6, 0.0]),
                ("正交无关", [0.0, 1.0, 0.0]),
            ],
            with_reranker=False,
        )

        result = await harness.service.search(_request(project_id, top_k=3, score_threshold=0.0))

        assert [citation.snippet for citation in result.citations] == ["完全对齐", "比较接近", "正交无关"]
        assert result.citations[0].score == pytest.approx(1.0)
        assert result.citations[1].score == pytest.approx(0.8)
        assert result.citations[2].score == pytest.approx(0.0)
        assert harness.client.rerank_calls == []

        # The per-base default threshold (0.2) filters cosine scores too.
        result = await harness.service.search(_request(project_id, top_k=3))
        assert [citation.snippet for citation in result.citations] == ["完全对齐", "比较接近"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_rerank_free_negative_cosine_passes_zero_threshold_and_logs_negative_top_score(postgres_database_url: str) -> None:
    """Cosine scores live in [-1,1]; the query-log CHECK must accept them."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("反向段落", [-1.0, 0.0, 0.0])],
            with_reranker=False,
        )

        result = await harness.service.search(_request(project_id, score_threshold=0.0))

        assert [citation.snippet for citation in result.citations] == ["反向段落"]
        assert result.citations[0].score == pytest.approx(-1.0)
        rows = await _query_rows(harness, project_id)
        assert len(rows) == 1
        assert rows[0].top_score == pytest.approx(-1.0)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_mixed_rerank_and_rerank_free_groups_share_one_embedding_and_merge(postgres_database_url: str) -> None:
    """Bases sharing an embedding model split into per-reranker groups but
    reuse one query embedding; their scores merge into one ordering."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            reranked_base = _base_row(project_id, embedding_id, rerank_id, name="重排库")
            cosine_base = _base_row(project_id, embedding_id, None, name="余弦库")
            session.add_all([reranked_base, cosine_base])
            await session.flush()
            reranked_doc = _document_row(project_id, reranked_base.id, name="重排文档")
            cosine_doc = _document_row(project_id, cosine_base.id, name="余弦文档")
            session.add_all([reranked_doc, cosine_doc])
            await session.flush()
            session.add_all(
                [
                    _segment_row(reranked_doc, position=1, content="重排段落", embedding=[1.0, 0.0, 0.0]),
                    _segment_row(cosine_doc, position=1, content="高余弦段落", embedding=[0.9, 0.43589, 0.0]),
                    _segment_row(cosine_doc, position=2, content="低余弦段落", embedding=[0.3, 0.95394, 0.0]),
                ]
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=0, score=0.5)][:top_n]

        result = await harness.service.search(_request(project_id, top_k=3, score_threshold=0.0))

        # rerank:R and cosine:E are two score domains (T7 fusion): 重排段落 and
        # 高余弦段落 are each rank 1 at home (fused 0.5), 低余弦段落 is cosine
        # rank 2 (fused 61/2/62) — the raw 0.9/0.5/0.3 are never compared.
        assert {citation.snippet for citation in result.citations[:2]} == {"高余弦段落", "重排段落"}
        assert result.citations[2].snippet == "低余弦段落"
        assert all(citation.score == pytest.approx(0.5) for citation in result.citations[:2])
        assert result.citations[2].score == pytest.approx(61 / 2 / 62)
        assert {hit.citation.snippet: hit.local_score for hit in result.hits} == {"高余弦段落": pytest.approx(0.9), "重排段落": 0.5, "低余弦段落": pytest.approx(0.3)}
        assert len(harness.client.embed_calls) == 1
        assert len(harness.client.rerank_calls) == 1
        (_, _, submitted, _) = harness.client.rerank_calls[0]
        assert submitted == ["重排段落"]  # the cosine group never reaches the reranker
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Threshold, ordering, snippet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_threshold_default_override_zero_and_all_below(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, rerank_id = await _seed_single_base(
            harness,
            segments=[("高分段落", [1.0, 0.0, 0.0]), ("低分段落", [0.9, 0.1, 0.0])],
        )
        scripted: list[list[RerankScore]] = []

        def _script(documents: list[str], top_n: int) -> list[RerankScore]:
            return list(scripted[-1])[:top_n]

        harness.client.rerank_scripts[rerank_id] = _script

        # Default threshold 0.2 drops the 0.15 candidate.
        assert DEFAULT_SCORE_THRESHOLD == 0.2
        scripted.append([RerankScore(index=0, score=0.9), RerankScore(index=1, score=0.15)])
        result = await harness.service.search(_request(project_id))
        assert [citation.snippet for citation in result.citations] == ["高分段落"]

        # A request override raises the bar.
        scripted.append([RerankScore(index=0, score=0.45), RerankScore(index=1, score=0.3)])
        result = await harness.service.search(_request(project_id, score_threshold=0.5))
        assert result.citations == ()

        # Zero disables filtering entirely, even for negative provider scores.
        scripted.append([RerankScore(index=0, score=0.1), RerankScore(index=1, score=-0.2)])
        result = await harness.service.search(_request(project_id, score_threshold=0.0))
        assert [citation.score for citation in result.citations] == [0.1, -0.2]

        # A score exactly at the threshold is kept — only strictly-below drops.
        scripted.append([RerankScore(index=0, score=DEFAULT_SCORE_THRESHOLD), RerankScore(index=1, score=0.19)])
        result = await harness.service.search(_request(project_id))
        assert [citation.score for citation in result.citations] == [DEFAULT_SCORE_THRESHOLD]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_equal_rerank_scores_order_by_vector_score_then_position(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        # The cosine-worst segment sits at position 1: a sort key that skipped
        # vector_score and fell through to position would rank it first.
        project_id, _, _, rerank_id = await _seed_single_base(
            harness,
            segments=[
                ("向量较远", [0.6, 0.8, 0.0]),
                ("并列一号", [1.0, 0.0, 0.0]),
                ("并列二号", [1.0, 0.0, 0.0]),
            ],
        )
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=index, score=0.7) for index in range(len(documents))][:top_n]

        result = await harness.service.search(_request(project_id, top_k=3))

        # Same rerank score: vector score wins; same vector too: position wins.
        assert [citation.snippet for citation in result.citations] == ["并列一号", "并列二号", "向量较远"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_equal_rerank_and_vector_scores_order_by_document_id(postgres_database_url: str) -> None:
    """With every score level tied, the UUID levels give a total, stable order."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="同分库")
            session.add(base)
            await session.flush()
            first_doc = _document_row(project_id, base.id, name="文档甲")
            second_doc = _document_row(project_id, base.id, name="文档乙")
            session.add_all([first_doc, second_doc])
            await session.flush()
            session.add_all(
                [
                    _segment_row(first_doc, position=1, content="甲的段落", embedding=[1.0, 0.0, 0.0]),
                    _segment_row(second_doc, position=1, content="乙的段落", embedding=[1.0, 0.0, 0.0]),
                ]
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=index, score=0.7) for index in range(len(documents))][:top_n]

        result = await harness.service.search(_request(project_id, top_k=2))

        expected = ["甲的段落" if first_doc.id < second_doc.id else "乙的段落", "乙的段落" if first_doc.id < second_doc.id else "甲的段落"]
        assert [citation.snippet for citation in result.citations] == expected
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_citation_snippet_truncates_and_source_position_round_trips(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        long_content = "知" * 400
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="来源库")
            session.add(base)
            await session.flush()
            document = _document_row(project_id, base.id, name="来源文档")
            session.add(document)
            await session.flush()
            session.add(
                _segment_row(
                    document,
                    position=7,
                    content=long_content,
                    embedding=[1.0, 0.0, 0.0],
                    source_position={"page": 12},
                )
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id))

        citation = result.citations[0]
        assert citation.snippet == "知" * 320
        assert citation.source_position == {"page": 12}
        assert citation.knowledge_base_name == "来源库"
        assert citation.document_name == "来源文档"
        assert citation.segment_position == 7
        assert citation.document_id == document.id
        assert citation.segment_id is not None
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_failure_fails_the_whole_search(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(harness, segments=[("段落", [1.0, 0.0, 0.0])])
        harness.client.rerank_error = KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 响应缺少 results 数组")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))
        assert error.value.code == KNOWLEDGE_RERANK_FAILED
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_embedding_failure_fails_the_whole_search(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(harness, segments=[("段落", [1.0, 0.0, 0.0])])
        harness.client.embed_error = KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 调用失败")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))
        assert error.value.code == KNOWLEDGE_EMBEDDING_FAILED
        assert harness.client.rerank_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_database_failure_maps_to_search_failed() -> None:
    class _BrokenFactory:
        def __call__(self) -> AsyncSession:
            raise SQLAlchemyError("connection pool is gone")

    service = KnowledgeSearchService(
        session_factory=_BrokenFactory(),  # type: ignore[arg-type]
        client=_ScriptedClient(),  # type: ignore[arg-type]
        model_port=registry_model_port(),
    )

    with pytest.raises(KnowledgeError) as error:
        await service.search(_request(uuid.uuid4()))
    assert error.value.code == KNOWLEDGE_SEARCH_FAILED


@pytest.mark.asyncio
async def test_pre_embedding_authority_database_failure_maps_to_search_failed(
    postgres_database_url: str,
) -> None:
    """A short-guard DB fault fails closed before Provider query spend."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("段落", [1.0, 0.0, 0.0])],
        )

        class _DiesAfterGroups:
            def __init__(self, inner) -> None:  # noqa: ANN001
                self._inner = inner
                self._calls = 0

            def __call__(self):  # noqa: ANN204
                self._calls += 1
                if self._calls > 1:
                    raise SQLAlchemyError("pool shut down before embedding")
                return self._inner()

        service = KnowledgeSearchService(
            session_factory=_DiesAfterGroups(harness.factory),  # type: ignore[arg-type]
            client=harness.client,  # type: ignore[arg-type]
            model_port=registry_model_port(),
        )

        with pytest.raises(KnowledgeError) as error:
            await service.search(_request(project_id))
        assert error.value.code == KNOWLEDGE_SEARCH_FAILED
        assert harness.client.embed_calls == []
        assert harness.client.rerank_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cosine_recall_database_failure_maps_to_search_failed(postgres_database_url: str) -> None:
    """A factory that dies after the groups query covers the recall-stage branch."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(harness, segments=[("段落", [1.0, 0.0, 0.0])])

        class _DiesAfterFirstUse:
            def __init__(self, inner) -> None:  # noqa: ANN001
                self._inner = inner
                self._calls = 0

            def __call__(self):  # noqa: ANN204
                self._calls += 1
                if self._calls > 2:
                    raise SQLAlchemyError("pool shut down mid-request")
                return self._inner()

        service = KnowledgeSearchService(
            session_factory=_DiesAfterFirstUse(harness.factory),  # type: ignore[arg-type]
            client=harness.client,  # type: ignore[arg-type]
            model_port=registry_model_port(),
        )

        with pytest.raises(KnowledgeError) as error:
            await service.search(_request(project_id))
        assert error.value.code == KNOWLEDGE_SEARCH_FAILED
        # The failure hit recall, after the embed but before any rerank spend.
        assert len(harness.client.embed_calls) == 1
        assert harness.client.rerank_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_final_authority_database_failure_suppresses_provider_results(
    postgres_database_url: str,
) -> None:
    """Metrics are best-effort, but an unperformed final guard is not."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("不得在最终重验失败后返回", [1.0, 0.0, 0.0])],
        )

        class _DiesBeforeFinalGuard:
            def __init__(self, inner) -> None:  # noqa: ANN001
                self._inner = inner
                self._calls = 0

            def __call__(self):  # noqa: ANN204
                self._calls += 1
                if self._calls > 4:
                    raise SQLAlchemyError("pool failed before final authority guard")
                return self._inner()

        service = KnowledgeSearchService(
            session_factory=_DiesBeforeFinalGuard(harness.factory),  # type: ignore[arg-type]
            client=harness.client,  # type: ignore[arg-type]
            model_port=registry_model_port(),
        )

        class _Authority:
            actor_user_id = _DEFAULT_OWNER_USER_ID

            def __init__(self) -> None:
                self.project_id = project_id

            async def revalidate(self, session: AsyncSession) -> None:
                del session

        with pytest.raises(KnowledgeError) as error:
            await service.search(_request(project_id), authority=_Authority())

        assert error.value.code == KNOWLEDGE_SEARCH_FAILED
        assert len(harness.client.embed_calls) == 1
        assert len(harness.client.rerank_calls) == 1
        assert await _query_rows(harness, project_id) == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cosine_recall_is_limited_to_candidate_k(postgres_database_url: str) -> None:
    """Only the cosine top ``candidate_k`` segments ever reach the reranker."""

    harness = await _harness(postgres_database_url)
    try:
        # Strictly decreasing cosine similarity to the query [1, 0, 0].
        segments = [(f"段落{index:02d}", [1.0, index * 0.1, 0.0]) for index in range(1, 26)]
        project_id, _, _, _ = await _seed_single_base(harness, segments=segments)

        result = await harness.service.search(_request(project_id))

        assert calculate_candidate_k(DEFAULT_TOP_K) == 20
        [(_, _, reranked_documents, _)] = harness.client.rerank_calls
        assert reranked_documents == [f"段落{index:02d}" for index in range(1, 21)]
        assert len(result.citations) == DEFAULT_TOP_K
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Real client through a mock transport: batching and zero-vector guard
# ---------------------------------------------------------------------------


def _mock_provider(scores_by_content: dict[str, float], query_vector: list[float], rerank_requests: list[dict[str, Any]]) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={"data": [{"index": index, "embedding": query_vector} for index in range(len(payload["input"]))]},
            )
        rerank_requests.append(payload)
        ranked = sorted(
            ((index, scores_by_content[document]) for index, document in enumerate(payload["documents"])),
            key=lambda pair: pair[1],
            reverse=True,
        )[: payload["top_n"]]
        return httpx.Response(
            200,
            json={"results": [{"index": index, "relevance_score": score} for index, score in ranked]},
        )

    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_search_with_the_real_client_batches_rerank_and_merges_across_batches(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session, rerank_max_batch=2)
            base = _base_row(project_id, embedding_id, rerank_id, name="批次库")
            session.add(base)
            await session.flush()
            document = _document_row(project_id, base.id, name="批次文档")
            session.add(document)
            await session.flush()
            # Identical embeddings: recall order is the stable position order.
            for position in range(1, 6):
                session.add(_segment_row(document, position=position, content=f"候选{position}", embedding=[1.0, 0.0, 0.0]))

        scores = {"候选1": 0.5, "候选2": 0.9, "候选3": 0.1, "候选4": 0.7, "候选5": 0.3}
        rerank_requests: list[dict[str, Any]] = []
        client = KnowledgeModelClient(http=httpx.AsyncClient(transport=_mock_provider(scores, [1.0, 0.0, 0.0], rerank_requests)))
        service = KnowledgeSearchService(session_factory=factory, client=client, model_port=registry_model_port())

        result = await service.search(_request(project_id, top_k=3))

        assert [(citation.snippet, citation.score) for citation in result.citations] == [
            ("候选2", 0.9),
            ("候选4", 0.7),
            ("候选1", 0.5),
        ]
        assert [request["documents"] for request in rerank_requests] == [
            ["候选1", "候选2"],
            ["候选3", "候选4"],
            ["候选5"],
        ]
        assert [request["top_n"] for request in rerank_requests] == [2, 2, 1]
        await client.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_with_the_real_client_rejects_a_zero_query_embedding(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="零向量库")
            session.add(base)
            await session.flush()
            document = _document_row(project_id, base.id, name="文档")
            session.add(document)
            await session.flush()
            session.add(_segment_row(document, position=1, content="段落", embedding=[1.0, 0.0, 0.0]))

        client = KnowledgeModelClient(http=httpx.AsyncClient(transport=_mock_provider({}, [0.0, 0.0, 0.0], [])))
        service = KnowledgeSearchService(session_factory=factory, client=client, model_port=registry_model_port())

        with pytest.raises(KnowledgeError) as error:
            await service.search(_request(project_id))
        assert error.value.code == KNOWLEDGE_EMBEDDING_FAILED
        await client.aclose()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# K3: parent-child rollup, per-base defaults, query log, hit counts
# ---------------------------------------------------------------------------


async def _query_rows(harness: _RetrievalHarness, project_id: uuid.UUID) -> list[KnowledgeQueryRow]:
    async with harness.factory() as session:
        rows = await session.scalars(select(KnowledgeQueryRow).where(KnowledgeQueryRow.project_id == project_id).order_by(KnowledgeQueryRow.created_at))
        return list(rows.all())


@pytest.mark.asyncio
async def test_parent_child_recall_rolls_best_child_score_up_to_one_parent_candidate(postgres_database_url: str) -> None:
    """Two hits inside one parent produce one candidate carrying the best score."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="父子库")
            session.add(base)
            await session.flush()
            await _seed_parent_child_document(
                session,
                project_id,
                base.id,
                name="父子文档",
                parents=[
                    ("父块甲的完整内容", [("甲子一", [1.0, 0.0, 0.0]), ("甲子二", [0.6, 0.8, 0.0])]),
                    ("父块乙的完整内容", [("乙子一", [0.8, 0.6, 0.0])]),
                ],
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id, top_k=4))

        # Parents surface exactly once each, ordered by their best child score
        # (甲 1.0 > 乙 0.8); snippets show parent text, never child text.
        assert [citation.snippet for citation in result.citations] == ["父块甲的完整内容", "父块乙的完整内容"]
        assert [citation.segment_position for citation in result.citations] == [1, 2]
        # The reranker scored parent contents, not child chunks.
        (_, _, submitted, _) = harness.client.rerank_calls[0]
        assert submitted == ["父块甲的完整内容", "父块乙的完整内容"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_mixed_mode_recall_merges_general_segments_with_parent_rollups(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="混合库")
            session.add(base)
            await session.flush()
            general_document = _document_row(project_id, base.id, name="普通文档")
            session.add(general_document)
            await session.flush()
            session.add(_segment_row(general_document, position=1, content="普通模式段落", embedding=[0.9, 0.43589, 0.0]))
            await _seed_parent_child_document(
                session,
                project_id,
                base.id,
                name="父子文档",
                parents=[("父块内容", [("子块", [1.0, 0.0, 0.0])])],
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id, top_k=4))

        # Both modes compete in one pool: the parent (child score 1.0) beats
        # the general segment (cosine 0.9) and both reach the reranker.
        assert [citation.snippet for citation in result.citations] == ["父块内容", "普通模式段落"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_omitted_top_k_and_threshold_resolve_from_the_base_defaults(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="定制默认库", default_top_k=1, default_score_threshold=0.5)
            session.add(base)
            await session.flush()
            document = _document_row(project_id, base.id, name="文档")
            session.add(document)
            await session.flush()
            session.add(_segment_row(document, position=1, content="第一段", embedding=[1.0, 0.0, 0.0]))
            session.add(_segment_row(document, position=2, content="第二段", embedding=[0.9, 0.43589, 0.0]))
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
        scripted: list[list[RerankScore]] = []

        def _script(documents: list[str], top_n: int) -> list[RerankScore]:
            return list(scripted[-1])[:top_n]

        harness.client.rerank_scripts[rerank_id] = _script

        # default_top_k=1 truncates even though both clear the threshold.
        scripted.append([RerankScore(index=0, score=0.9), RerankScore(index=1, score=0.8)])
        result = await harness.service.search(_request(project_id))
        assert [citation.snippet for citation in result.citations] == ["第一段"]
        assert harness.client.rerank_calls[-1][3] == 2  # every candidate is scored; top_k cuts later

        # default_score_threshold=0.5 drops everything scored below it.
        scripted.append([RerankScore(index=0, score=0.45), RerankScore(index=1, score=0.3)])
        result = await harness.service.search(_request(project_id))
        assert result.citations == ()

        # Explicit request values override both defaults.
        scripted.append([RerankScore(index=0, score=0.45), RerankScore(index=1, score=0.3)])
        result = await harness.service.search(_request(project_id, top_k=2, score_threshold=0.0))
        assert [citation.score for citation in result.citations] == [0.45, 0.3]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_multi_base_defaults_widen_top_k_and_apply_each_bases_own_threshold(postgres_database_url: str) -> None:
    """Omitted top_k takes the largest per-base default; thresholds stay per base."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            strict_base = _base_row(project_id, embedding_id, rerank_id, name="严格库", default_top_k=1, default_score_threshold=0.8)
            lenient_base = _base_row(project_id, embedding_id, rerank_id, name="宽松库", default_top_k=3, default_score_threshold=0.1)
            session.add_all([strict_base, lenient_base])
            await session.flush()
            strict_document = _document_row(project_id, strict_base.id, name="严格文档")
            lenient_document = _document_row(project_id, lenient_base.id, name="宽松文档")
            session.add_all([strict_document, lenient_document])
            await session.flush()
            session.add(_segment_row(strict_document, position=1, content="严格库段落", embedding=[1.0, 0.0, 0.0]))
            session.add(_segment_row(lenient_document, position=1, content="宽松库段落甲", embedding=[0.9, 0.43589, 0.0]))
            session.add(_segment_row(lenient_document, position=2, content="宽松库段落乙", embedding=[0.8, 0.6, 0.0]))
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=index, score=0.5) for index in range(len(documents))][:top_n]

        result = await harness.service.search(_request(project_id))

        # top_k widened to max(1, 3) = 3, so both lenient segments fit; the
        # strict base's own 0.8 threshold silently drops its 0.5-scored hit.
        assert [citation.snippet for citation in result.citations] == ["宽松库段落甲", "宽松库段落乙"]
        assert harness.client.rerank_calls[-1][3] == 3
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_appends_query_log_rows_and_increments_hit_counts(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(
            harness,
            segments=[("第一段", [1.0, 0.0, 0.0]), ("第二段", [0.9, 0.43589, 0.0])],
        )

        await harness.service.search(_request(project_id, query="第一次检索", top_k=2))
        await harness.service.search(_request(project_id, query="第二次检索", top_k=1, source="agent"))

        rows = await _query_rows(harness, project_id)
        assert [(row.query, row.source, row.result_count) for row in rows] == [
            ("第一次检索", "retrieval_test", 2),
            ("第二次检索", "agent", 1),
        ]
        assert all(row.knowledge_base_ids == [str(base_id)] for row in rows)
        assert rows[0].top_score == pytest.approx(1.0)  # default rerank script scores descending from 1.0

        async with harness.factory() as session:
            segments = list((await session.scalars(select(KnowledgeSegmentRow).order_by(KnowledgeSegmentRow.position))).all())
            document = await session.scalar(select(KnowledgeDocumentRow))
        assert [segment.hit_count for segment in segments] == [2, 1]  # 第一段 cited twice
        assert document is not None and document.hit_count == 3

        # Explicitly targeting only unknown bases searches nothing and logs nothing.
        await harness.service.search(_request(project_id, query="无库检索", knowledge_base_ids=(uuid.uuid4(),)))
        assert len(await _query_rows(harness, project_id)) == 2
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_event",
    ["INSERT ON knowledge_queries", "UPDATE OF hit_count ON knowledge_segments", "UPDATE OF hit_count ON knowledge_documents"],
)
async def test_search_returns_verified_hits_when_only_statistics_writes_fail(
    postgres_database_url: str,
    failure_event: str,
) -> None:
    """History and counters roll back together without discarding safe hits."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(
            harness,
            segments=[("统计故障时仍可回答的原文", [1.0, 0.0, 0.0])],
        )
        # Fail only the selected metrics write, after the final authorization
        # and content reads succeeded, in this fixture's isolated database.
        async with harness.engine.begin() as connection:
            await connection.execute(text("CREATE FUNCTION reject_statistics_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'statistics unavailable'; END $$"))
            await connection.execute(text(f"CREATE TRIGGER reject_statistics_write BEFORE {failure_event} FOR EACH ROW EXECUTE FUNCTION reject_statistics_write()"))  # noqa: S608 - fixed parametrized trigger clauses

        result = await harness.service.search(_request(project_id))

        assert [hit.passage for hit in result.hits] == ["统计故障时仍可回答的原文"]
        history, total = await harness.service.list_recent_queries(project_id, _DEFAULT_OWNER_USER_ID, base_id)
        assert (history, total) == ([], 0)
        async with harness.factory() as session:
            segment_hits = await session.scalar(select(KnowledgeSegmentRow.hit_count))
            document_hits = await session.scalar(select(KnowledgeDocumentRow.hit_count))
        assert (segment_hits, document_hits) == (0, 0)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_fails_when_final_transaction_cannot_commit(postgres_database_url: str) -> None:
    """An outer COMMIT failure is not a best-effort statistics failure."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(
            harness,
            segments=[("最终事务未完成时不得返回", [1.0, 0.0, 0.0])],
        )
        async with harness.engine.begin() as connection:
            await connection.execute(text("CREATE FUNCTION reject_final_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'final commit unavailable'; END $$"))
            await connection.execute(text("CREATE CONSTRAINT TRIGGER reject_final_commit AFTER INSERT ON knowledge_queries DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_final_commit()"))

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))

        assert error.value.code == KNOWLEDGE_SEARCH_FAILED
        history, total = await harness.service.list_recent_queries(project_id, _DEFAULT_OWNER_USER_ID, base_id)
        assert (history, total) == ([], 0)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_zero_result_searches_still_log_with_null_top_score(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, rerank_id = await _seed_single_base(
            harness,
            segments=[("低分段", [1.0, 0.0, 0.0])],
        )
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=0, score=0.05)][:top_n]

        result = await harness.service.search(_request(project_id, query="全部低于阈值"))

        assert result.citations == ()
        rows = await _query_rows(harness, project_id)
        assert [(row.result_count, row.top_score) for row in rows] == [(0, None)]
        async with harness.factory() as session:
            segment = await session.scalar(select(KnowledgeSegmentRow))
            document = await session.scalar(select(KnowledgeDocumentRow))
        assert segment is not None and segment.hit_count == 0
        assert document is not None and document.hit_count == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_revalidates_authority_after_provider_work_before_returning_citations(
    postgres_database_url: str,
) -> None:
    """Revocation during reranking suppresses results and all search writes."""

    harness = await _harness(postgres_database_url)

    class _Authority:
        def __init__(self, project_id: uuid.UUID) -> None:
            self.project_id = project_id
            self.actor_user_id = _DEFAULT_OWNER_USER_ID
            self.calls = 0
            self.revoked = False

        async def revalidate(self, session: AsyncSession) -> None:
            del session
            self.calls += 1
            if self.revoked:
                raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")

    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("不得泄露的检索结果", [1.0, 0.0, 0.0])],
        )
        authority = _Authority(project_id)
        original_rerank = harness.client.rerank

        async def _rerank_then_revoke(material, query, documents, top_n, **hooks):  # noqa: ANN001, ANN202
            scores = await original_rerank(material, query, documents, top_n, **hooks)
            authority.revoked = True
            return scores

        harness.client.rerank = _rerank_then_revoke  # type: ignore[method-assign]

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(
                _request(project_id),
                authority=authority,
            )

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert authority.calls == 5
        assert await _query_rows(harness, project_id) == []
        async with harness.factory() as session:
            segment = await session.scalar(select(KnowledgeSegmentRow))
            document = await session.scalar(select(KnowledgeDocumentRow))
        assert segment is not None and segment.hit_count == 0
        assert document is not None and document.hit_count == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_revalidates_after_embedding_before_segment_text_reaches_reranker(
    postgres_database_url: str,
) -> None:
    """Revocation during query embedding prevents recall text from leaving PostgreSQL."""

    harness = await _harness(postgres_database_url)

    class _Authority:
        def __init__(self, project_id: uuid.UUID) -> None:
            self.project_id = project_id
            self.actor_user_id = _DEFAULT_OWNER_USER_ID
            self.revoked = False

        async def revalidate(self, session: AsyncSession) -> None:
            del session
            if self.revoked:
                raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")

    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("不得发送给 Reranker 的正文", [1.0, 0.0, 0.0])],
        )
        authority = _Authority(project_id)
        original_embed = harness.client.embed

        async def _embed_then_revoke(material, texts, **hooks):  # noqa: ANN001, ANN202
            vectors = await original_embed(material, texts, **hooks)
            authority.revoked = True
            return vectors

        harness.client.embed = _embed_then_revoke  # type: ignore[method-assign]

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(
                _request(project_id),
                authority=authority,
            )

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert harness.client.rerank_calls == []
        assert await _query_rows(harness, project_id) == []
        async with harness.factory() as session:
            segment = await session.scalar(select(KnowledgeSegmentRow))
            document = await session.scalar(select(KnowledgeDocumentRow))
        assert segment is not None and segment.hit_count == 0
        assert document is not None and document.hit_count == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_revalidates_before_each_model_group_embedding(
    postgres_database_url: str,
) -> None:
    """Revocation in group one prevents query spend against group two."""

    harness = await _harness(postgres_database_url)

    class _Authority:
        def __init__(self, project_id: uuid.UUID) -> None:
            self.project_id = project_id
            self.actor_user_id = _DEFAULT_OWNER_USER_ID
            self.revoked = False

        async def revalidate(self, session: AsyncSession) -> None:
            del session
            if self.revoked:
                raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")

    try:
        project_id, _, _, _, _ = await _seed_two_model_groups(harness)
        authority = _Authority(project_id)
        original_rerank = harness.client.rerank

        async def _first_rerank_then_revoke(  # noqa: ANN202
            material,  # noqa: ANN001
            query: str,
            documents: list[str],
            top_n: int,
            **hooks,  # noqa: ANN003
        ):
            scores = await original_rerank(material, query, documents, top_n, **hooks)
            authority.revoked = True
            return scores

        harness.client.rerank = _first_rerank_then_revoke  # type: ignore[method-assign]

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(
                _request(project_id),
                authority=authority,
            )

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert len(harness.client.embed_calls) == 1
        assert len(harness.client.rerank_calls) == 1
        assert await _query_rows(harness, project_id) == []
        async with harness.factory() as session:
            segments = list((await session.scalars(select(KnowledgeSegmentRow))).all())
            documents = list((await session.scalars(select(KnowledgeDocumentRow))).all())
        assert all(segment.hit_count == 0 for segment in segments)
        assert all(document.hit_count == 0 for document in documents)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_recent_queries_are_private_to_the_trusted_search_actor(postgres_database_url: str) -> None:
    """Two project members only see the raw queries they personally issued."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(
            harness,
            segments=[("共享知识段落", [1.0, 0.0, 0.0])],
        )
        member_a = uuid.uuid4()
        member_b = uuid.uuid4()

        await harness.service.search(
            KnowledgeSearchRequest(
                project_id=project_id,
                owner_user_id=member_a,
                query="A 的私有客户问题",
                knowledge_base_ids=(base_id,),
            )
        )
        await harness.service.search(
            KnowledgeSearchRequest(
                project_id=project_id,
                owner_user_id=member_b,
                query="B 的私有客户问题",
                knowledge_base_ids=(base_id,),
                source="agent",
            )
        )

        member_a_views, member_a_total = await harness.service.list_recent_queries(
            project_id,
            member_a,
            base_id,
        )
        member_b_views, member_b_total = await harness.service.list_recent_queries(
            project_id,
            member_b,
            base_id,
        )

        assert member_a_total == member_b_total == 1
        assert [view.query for view in member_a_views] == ["A 的私有客户问题"]
        assert [view.query for view in member_b_views] == ["B 的私有客户问题"]
        rows = await _query_rows(harness, project_id)
        assert [(row.query, row.owner_user_id) for row in rows] == [
            ("A 的私有客户问题", str(member_a)),
            ("B 的私有客户问题", str(member_b)),
        ]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_list_recent_queries_filters_by_base_paginates_and_validates(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            first_base = _base_row(project_id, embedding_id, rerank_id, name="库一")
            second_base = _base_row(project_id, embedding_id, rerank_id, name="库二")
            session.add_all([first_base, second_base])
            await session.flush()
            first_document = _document_row(project_id, first_base.id, name="文档一")
            second_document = _document_row(project_id, second_base.id, name="文档二")
            session.add_all([first_document, second_document])
            await session.flush()
            session.add(_segment_row(first_document, position=1, content="库一段落", embedding=[1.0, 0.0, 0.0]))
            session.add(_segment_row(second_document, position=1, content="库二段落", embedding=[0.9, 0.43589, 0.0]))
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        await harness.service.search(_request(project_id, query="查询一", knowledge_base_ids=(first_base.id,)))
        await harness.service.search(_request(project_id, query="查询二", knowledge_base_ids=(first_base.id,)))
        await harness.service.search(_request(project_id, query="查询三", knowledge_base_ids=(second_base.id,)))
        await harness.service.search(_request(project_id, query="查询四"))  # both bases

        views, total = await harness.service.list_recent_queries(
            project_id,
            _DEFAULT_OWNER_USER_ID,
            first_base.id,
        )
        assert total == 3
        assert [view.query for view in views] == ["查询四", "查询二", "查询一"]  # newest first
        assert all(isinstance(view, KnowledgeQueryView) for view in views)
        assert set(views[0].knowledge_base_ids) == {first_base.id, second_base.id}

        views, total = await harness.service.list_recent_queries(
            project_id,
            _DEFAULT_OWNER_USER_ID,
            second_base.id,
        )
        assert total == 2
        assert [view.query for view in views] == ["查询四", "查询三"]

        paged, total = await harness.service.list_recent_queries(
            project_id,
            _DEFAULT_OWNER_USER_ID,
            first_base.id,
            page=2,
            page_size=2,
        )
        assert total == 3
        assert [view.query for view in paged] == ["查询一"]

        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_recent_queries(
                project_id,
                _DEFAULT_OWNER_USER_ID,
                uuid.uuid4(),
            )
        assert error.value.code == KNOWLEDGE_NOT_FOUND

        # A base in another project is invisible, not just empty.
        async with harness.factory() as session, session.begin():
            other_project = await _seed_project(session, uuid.uuid4().hex[:8])
        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_recent_queries(
                other_project,
                _DEFAULT_OWNER_USER_ID,
                first_base.id,
            )
        assert error.value.code == KNOWLEDGE_NOT_FOUND

        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_recent_queries(
                project_id,
                _DEFAULT_OWNER_USER_ID,
                first_base.id,
                page=0,
            )
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_recent_queries(
                project_id,
                _DEFAULT_OWNER_USER_ID,
                first_base.id,
                page_size=101,
            )
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_disabled_parents_and_stale_versions_never_recall_through_children(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="治理库")
            session.add(base)
            await session.flush()
            document = await _seed_parent_child_document(
                session,
                project_id,
                base.id,
                name="治理文档",
                parents=[
                    ("被禁用的父块", [("禁用子块", [1.0, 0.0, 0.0])]),
                    ("正常父块", [("正常子块", [0.8, 0.6, 0.0])]),
                ],
            )
            await session.flush()
            disabled_parent = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document.id, KnowledgeSegmentRow.position == 1))
            assert disabled_parent is not None
            disabled_parent.enabled = False
            # A stale-version parent+child pair must stay invisible too.
            stale_parent = _segment_row(document, position=9, content="旧版本父块", embedding=None, document_version=99)
            session.add(stale_parent)
            await session.flush()
            session.add(_child_row(stale_parent, position=1, content="旧版本子块", embedding=[1.0, 0.0, 0.0]))
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        result = await harness.service.search(_request(project_id, top_k=4))

        assert [citation.snippet for citation in result.citations] == ["正常父块"]
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------

_REQUEST_ID = "knowledge-m5-contract"
_PROJECT_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_OWNER_USER_ID = _DEFAULT_OWNER_USER_ID


class _FakeSearchModule:
    def __init__(self) -> None:
        self.requests: list[KnowledgeSearchRequest] = []
        self.query_calls: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, int, int]] = []
        self.error: KnowledgeError | None = None
        passage = "安装前请确认电源已断开。"
        digest = hashlib.sha256(passage.encode("utf-8")).hexdigest()
        citation = KnowledgeCitation(
            knowledge_base_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
            knowledge_base_name="产品手册",
            document_id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
            document_name="安装指南.pdf",
            segment_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
            segment_position=3,
            snippet=passage,
            score=0.91,
            source_position={"page": 12},
            document_version=1,
            content_digest=digest,
            score_kind="cosine",
        )
        self.result = KnowledgeSearchResult(
            hits=(
                KnowledgeSearchHit(
                    citation=citation,
                    passage=passage,
                    document_version=1,
                    content_digest=digest,
                    local_score=0.91,
                    local_score_kind="cosine",
                    score_domain="embedding:test",
                    ranking_method="cosine",
                    ranking_score=0.91,
                ),
            )
        )

    async def search(self, request: KnowledgeSearchRequest, *, authority) -> KnowledgeSearchResult:  # noqa: ANN001
        self.requests.append(request)
        assert authority.project_id == _PROJECT_ID
        assert authority.actor_user_id == _OWNER_USER_ID
        if self.error is not None:
            raise self.error
        return self.result

    async def list_recent_queries(self, project_id: uuid.UUID, owner_user_id: uuid.UUID, base_id: uuid.UUID, *, page: int = 1, page_size: int = 20, authority):  # noqa: ANN001, ANN201
        assert authority.project_id == _PROJECT_ID
        assert authority.actor_user_id == _OWNER_USER_ID
        self.query_calls.append((project_id, owner_user_id, base_id, page, page_size))
        if self.error is not None:
            raise self.error
        view = KnowledgeQueryView(
            id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
            knowledge_base_ids=(base_id,),
            query="最近的问题",
            source="retrieval_test",
            result_count=2,
            top_score=0.87,
            created_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
        )
        return [view], 41


def _app(module: _FakeSearchModule) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = ProjectContext(
        user_id=_OWNER_USER_ID,
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id=_REQUEST_ID,
    )
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.dependency_overrides[gateway.require_project_knowledge_edit] = lambda: context
    app.state.knowledge_module = module
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_http_search_round_trips_the_module_result() -> None:
    module = _FakeSearchModule()
    base_id = uuid.uuid4()
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "如何安装", "knowledge_base_ids": [str(base_id)], "top_k": 5},
        )

    assert response.status_code == 200
    request = module.requests[0]
    assert request.project_id == _PROJECT_ID
    assert request.owner_user_id == _OWNER_USER_ID
    assert request.query == "如何安装"
    assert request.knowledge_base_ids == (base_id,)
    assert request.top_k == 5
    assert request.score_threshold is None

    citation = module.result.citations[0]
    assert response.json() == {
        "citations": [
            {
                "knowledge_base_id": str(citation.knowledge_base_id),
                "knowledge_base_name": citation.knowledge_base_name,
                "document_id": str(citation.document_id),
                "document_name": citation.document_name,
                "segment_id": str(citation.segment_id),
                "segment_position": citation.segment_position,
                "snippet": citation.snippet,
                "score": citation.score,
                "source_position": citation.source_position,
                "document_version": 1,
                "content_digest": citation.content_digest,
                "score_kind": "cosine",
            }
        ],
        "diagnostics": None,
        "request_id": _REQUEST_ID,
    }
    # The plain response never leaks the full passage: only the short quote.
    assert "passage" not in response.text


@pytest.mark.asyncio
async def test_http_search_defaults_optional_fields_and_maps_errors() -> None:
    module = _FakeSearchModule()
    async with _client(_app(module)) as client:
        ok = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/search", json={"query": "问"})

        module.error = KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 服务不可用")
        rerank_failed = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/search", json={"query": "问"})

        module.error = KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "query 不能为空")
        invalid = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/search", json={"query": "  "})

    assert ok.status_code == 200
    assert module.requests[0].knowledge_base_ids is None
    assert module.requests[0].top_k is None

    assert rerank_failed.status_code == 502
    assert rerank_failed.json()["detail"]["code"] == KNOWLEDGE_RERANK_FAILED
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == KNOWLEDGE_INVALID_REQUEST


@pytest.mark.asyncio
async def test_http_search_forwards_an_optional_score_threshold_override() -> None:
    """The retrieval test panel may override the threshold; range rules stay in the package."""

    module = _FakeSearchModule()
    async with _client(_app(module)) as client:
        omitted = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/search", json={"query": "问"})
        overridden = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "score_threshold": 0.55},
        )
        zero = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "score_threshold": 0},
        )

        module.error = KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "score_threshold 必须在 0..1 之间")
        out_of_range = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "score_threshold": 1.5},
        )

    assert omitted.status_code == 200
    assert overridden.status_code == 200
    assert zero.status_code == 200
    assert [request.score_threshold for request in module.requests[:3]] == [None, 0.55, 0.0]
    assert out_of_range.status_code == 422
    assert out_of_range.json()["detail"]["code"] == KNOWLEDGE_INVALID_REQUEST


@pytest.mark.asyncio
async def test_http_search_forwards_the_retrieval_mode_override() -> None:
    """The retrieval test panel may force semantic/hybrid for one call; the
    per-base configuration is never touched by this route."""

    module = _FakeSearchModule()
    async with _client(_app(module)) as client:
        omitted = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/search", json={"query": "问"})
        hybrid = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "retrieval_mode": "hybrid"},
        )
        invalid = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "retrieval_mode": "fancy"},
        )

    assert omitted.status_code == 200
    assert hybrid.status_code == 200
    assert [request.retrieval_mode for request in module.requests] == [None, "hybrid"]
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_http_search_bounds_the_knowledge_base_ids_list() -> None:
    """Empty means "say null instead"; oversized lists never reach the module."""

    module = _FakeSearchModule()
    async with _client(_app(module)) as client:
        empty = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "knowledge_base_ids": []},
        )
        oversized = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "knowledge_base_ids": [str(uuid.uuid4()) for _ in range(101)]},
        )
        at_limit = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "knowledge_base_ids": [str(uuid.uuid4()) for _ in range(100)]},
        )

    assert empty.status_code == 422
    assert oversized.status_code == 422
    assert at_limit.status_code == 200
    assert len(module.requests) == 1
    assert len(module.requests[0].knowledge_base_ids or ()) == 100


@pytest.mark.asyncio
async def test_http_search_labels_requests_as_retrieval_test() -> None:
    """The panel route pins the query-log source; the Agent tool pins its own."""

    module = _FakeSearchModule()
    async with _client(_app(module)) as client:
        response = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/search", json={"query": "问"})

    assert response.status_code == 200
    assert module.requests[0].source == "retrieval_test"


@pytest.mark.asyncio
async def test_http_search_debug_round_trips_the_safe_hit_diagnostics() -> None:
    """debug=true adds bounded per-hit evidence; passages and child text stay out."""

    from dataclasses import replace as dataclass_replace

    from actweave_knowledge import (
        KnowledgeHitDiagnostics,
        KnowledgeMatchedChild,
        KnowledgeRouteCounts,
        KnowledgeSearchDiagnostics,
        KnowledgeSearchTimings,
    )

    module = _FakeSearchModule()
    hit = module.result.hits[0]
    child = KnowledgeMatchedChild(
        child_id=uuid.UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        position=2,
        route="semantic",
        score=0.88,
    )
    debug_hit = dataclass_replace(hit, matched_children=(child,))
    embedding_model_id = uuid.UUID("12121212-1212-4121-8121-121212121212")
    module.result = KnowledgeSearchResult(
        hits=(debug_hit,),
        diagnostics=KnowledgeSearchDiagnostics(
            strategy_version=KNOWLEDGE_STRATEGY_VERSION,
            lexical_version=1,
            target_base_count=1,
            effective_top_k=4,
            per_base_route_budget=20,
            retrieval_mode="semantic",
            counts=KnowledgeRouteCounts(returned=1),
            timings=KnowledgeSearchTimings(),
            model_ids=(embedding_model_id,),
            ranking_method="cosine",
            hit_diagnostics=(
                KnowledgeHitDiagnostics(
                    segment_id=debug_hit.citation.segment_id,
                    local_score=debug_hit.local_score,
                    local_score_kind=debug_hit.local_score_kind,
                    score_domain=debug_hit.score_domain,
                    ranking_method=debug_hit.ranking_method,
                    ranking_score=debug_hit.ranking_score,
                    matched_children=(child,),
                ),
            ),
        ),
    )
    async with _client(_app(module)) as client:
        debug = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "debug": True},
        )
        plain = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/search", json={"query": "问"})

    assert debug.status_code == 200
    assert plain.status_code == 200
    assert module.requests[0].debug is True
    assert module.requests[1].debug is False

    diagnostics = debug.json()["diagnostics"]
    assert diagnostics["strategy_version"] == KNOWLEDGE_STRATEGY_VERSION
    assert diagnostics["target_base_count"] == 1
    assert diagnostics["effective_top_k"] == 4
    assert diagnostics["retrieval_mode"] == "semantic"
    assert diagnostics["model_ids"] == [str(embedding_model_id)]
    assert diagnostics["ranking_method"] == "cosine"
    assert diagnostics["counts"]["returned"] == 1
    assert diagnostics["timings"]["recall_ms"] == 0.0
    assert diagnostics["empty_reason"] is None
    [entry] = diagnostics["hit_diagnostics"]
    assert entry == {
        "segment_id": str(debug_hit.citation.segment_id),
        "matched_via": "segment",
        "local_score": 0.91,
        "local_score_kind": "cosine",
        "score_domain": "embedding:test",
        "ranking_method": "cosine",
        "ranking_score": 0.91,
        "matched_children": [
            {
                "child_id": str(child.child_id),
                "position": 2,
                "route": "semantic",
                "score": 0.88,
            }
        ],
    }
    # Bounded evidence only: no passage or child text in the debug payload.
    assert "安装前请确认电源已断开" not in json.dumps(diagnostics, ensure_ascii=False)


@pytest.mark.asyncio
async def test_http_search_forwards_metadata_filters_and_bounds_the_list() -> None:
    """K4: filter bodies become package DTOs; shape errors stop at the route."""

    module = _FakeSearchModule()
    async with _client(_app(module)) as client:
        omitted = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/search", json={"query": "问"})
        filtered = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={
                "query": "问",
                "metadata_filters": [
                    {"name": "部门", "operator": "eq", "value": "工程"},
                    {"name": "year", "operator": "gte", "value": 2024},
                ],
            },
        )
        bad_operator = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "metadata_filters": [{"name": "部门", "operator": "like", "value": "x"}]},
        )
        empty_list = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "metadata_filters": []},
        )
        oversized = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/search",
            json={"query": "问", "metadata_filters": [{"name": f"f{index}", "operator": "eq", "value": 1} for index in range(11)]},
        )

    assert omitted.status_code == 200
    assert module.requests[0].metadata_filters is None
    assert filtered.status_code == 200
    assert module.requests[1].metadata_filters == (
        KnowledgeMetadataFilter(name="部门", operator="eq", value="工程"),
        KnowledgeMetadataFilter(name="year", operator="gte", value=2024),
    )
    assert type(module.requests[1].metadata_filters[1].value) is int
    # Shape errors are pydantic 422s that never reach the module.
    for response in (bad_operator, empty_list, oversized):
        assert response.status_code == 422
    assert len(module.requests) == 2


@pytest.mark.asyncio
async def test_http_recent_queries_round_trip_and_error_mapping() -> None:
    module = _FakeSearchModule()
    base_id = uuid.uuid4()
    async with _client(_app(module)) as client:
        listed = await client.get(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{base_id}/queries",
            params={"page": 3, "page_size": 10},
        )

        module.error = KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
        missing = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/bases/{uuid.uuid4()}/queries")

    assert listed.status_code == 200
    assert module.query_calls[0] == (_PROJECT_ID, _OWNER_USER_ID, base_id, 3, 10)
    payload = listed.json()
    assert payload["total"] == 41
    assert payload["page"] == 3
    assert payload["page_size"] == 10
    assert payload["request_id"] == _REQUEST_ID
    assert payload["items"] == [
        {
            "id": "99999999-9999-4999-8999-999999999999",
            "knowledge_base_ids": [str(base_id)],
            "query": "最近的问题",
            "source": "retrieval_test",
            "result_count": 2,
            "top_score": 0.87,
            "created_at": "2026-08-29T12:00:00Z",
        }
    ]

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == KNOWLEDGE_NOT_FOUND


@pytest.mark.asyncio
async def test_http_search_through_the_real_module_returns_reranked_citations(postgres_database_url: str) -> None:
    """Release-gate integration: ASGI route -> KnowledgeModule -> pgvector -> rerank."""

    from actweave_knowledge import KnowledgeSettings
    from actweave_knowledge.contracts import KnowledgeMinioSettings
    from actweave_knowledge.module import KnowledgeModule

    engine = create_async_engine(postgres_database_url)
    module: KnowledgeModule | None = None
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            membership_id = await _seed_project_member(
                session,
                project_id,
                _DEFAULT_OWNER_USER_ID,
            )
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="集成库")
            session.add(base)
            await session.flush()
            document = _document_row(project_id, base.id, name="集成文档")
            session.add(document)
            await session.flush()
            session.add_all(
                [
                    _segment_row(document, position=1, content="靠前但低分", embedding=[1.0, 0.0, 0.0]),
                    _segment_row(document, position=2, content="靠后但高分", embedding=[0.9, 0.1, 0.0]),
                ]
            )

        # The reranker inverts the cosine order, proving both stages ran.
        scores = {"靠前但低分": 0.3, "靠后但高分": 0.9}
        module = KnowledgeModule(
            settings=KnowledgeSettings(
                enabled=True,
                minio=KnowledgeMinioSettings(
                    endpoint="127.0.0.1:9000",
                    bucket="actweave-knowledge-test",
                    access_key="test",
                    secret_key="test",
                ),
            ),
            session_factory=factory,
            model_port=registry_model_port(),
            model_client=KnowledgeModelClient(http=httpx.AsyncClient(transport=_mock_provider(scores, [1.0, 0.0, 0.0], []))),
        )

        app = FastAPI()
        app.include_router(gateway.project_router)
        context = ProjectContext(
            project_id=project_id,
            user_id=_DEFAULT_OWNER_USER_ID,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id=_REQUEST_ID,
        )
        app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
        app.state.knowledge_module = module
        async with _client(app) as client:
            response = await client.post(
                f"/api/projects/{project_id}/knowledge/search",
                json={"query": "如何安装", "top_k": 2},
            )

        assert response.status_code == 200
        payload = response.json()
        assert [(citation["snippet"], citation["score"]) for citation in payload["citations"]] == [
            ("靠后但高分", 0.9),
            ("靠前但低分", 0.3),
        ]
        assert payload["request_id"] == _REQUEST_ID
    finally:
        if module is not None:
            await module.aclose()
        await engine.dispose()


# ---------------------------------------------------------------------------
# K4: manual metadata filters on both recall paths
# ---------------------------------------------------------------------------


async def _seed_metadata_documents(harness: _RetrievalHarness) -> uuid.UUID:
    """Three general documents whose metadata differs; identical vectors.

    工程 doc {部门: 工程, year: 2024}, 市场 doc {部门: 市场, year: 2026},
    and one document without metadata. Every segment embeds [1,0,0] so only
    the filters decide what is recalled.
    """

    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        embedding_id, rerank_id = await _seed_models(session)
        base = _base_row(project_id, embedding_id, rerank_id, name="元数据库")
        session.add(base)
        await session.flush()

        engineering = _document_row(project_id, base.id, name="工程文档")
        engineering.doc_metadata = {"部门": "工程", "year": 2024}
        marketing = _document_row(project_id, base.id, name="市场文档")
        marketing.doc_metadata = {"部门": "市场", "year": 2026}
        bare = _document_row(project_id, base.id, name="无元数据文档")
        session.add_all([engineering, marketing, bare])
        await session.flush()

        vector = [1.0, 0.0, 0.0]
        session.add_all(
            [
                _segment_row(engineering, position=1, content="工程段落", embedding=vector),
                _segment_row(marketing, position=1, content="市场段落", embedding=vector),
                _segment_row(bare, position=1, content="无元数据段落", embedding=vector),
            ]
        )
    harness.client.query_vectors[embedding_id] = vector
    return project_id


@pytest.mark.asyncio
async def test_metadata_filters_gate_general_recall(postgres_database_url: str) -> None:
    """eq/contains/gte/lte AND onto recall; a missing key never matches."""

    harness = await _harness(postgres_database_url)
    try:
        project_id = await _seed_metadata_documents(harness)

        async def snippets(*filters: KnowledgeMetadataFilter) -> list[str]:
            result = await harness.service.search(_request(project_id, metadata_filters=filters or None))
            return sorted(citation.snippet for citation in result.citations)

        assert await snippets() == ["工程段落", "市场段落", "无元数据段落"]
        assert await snippets(KnowledgeMetadataFilter(name="部门", operator="eq", value="工程")) == ["工程段落"]
        assert await snippets(KnowledgeMetadataFilter(name="部门", operator="contains", value="场")) == ["市场段落"]
        assert await snippets(KnowledgeMetadataFilter(name="year", operator="gte", value=2025)) == ["市场段落"]
        assert await snippets(KnowledgeMetadataFilter(name="year", operator="lte", value=2025)) == ["工程段落"]
        # JSONB containment is type-exact: the number 2026 matches, "2026" doesn't.
        assert await snippets(KnowledgeMetadataFilter(name="year", operator="eq", value=2026)) == ["市场段落"]
        assert await snippets(KnowledgeMetadataFilter(name="year", operator="eq", value="2026")) == []
        # Conditions AND together.
        assert await snippets(
            KnowledgeMetadataFilter(name="部门", operator="contains", value="工"),
            KnowledgeMetadataFilter(name="year", operator="lte", value=2025),
        ) == ["工程段落"]
        assert (
            await snippets(
                KnowledgeMetadataFilter(name="部门", operator="eq", value="工程"),
                KnowledgeMetadataFilter(name="year", operator="gte", value=2025),
            )
            == []
        )
        # A range condition on a string value is a non-match, not an error.
        assert await snippets(KnowledgeMetadataFilter(name="部门", operator="gte", value=1)) == []
        # An undefined name matches nothing.
        assert await snippets(KnowledgeMetadataFilter(name="不存在", operator="eq", value="x")) == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_metadata_filters_gate_parent_child_recall(postgres_database_url: str) -> None:
    """The same conditions exclude parents recalled through their children."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="父子元数据库")
            session.add(base)
            await session.flush()

            engineering = await _seed_parent_child_document(
                session,
                project_id,
                base.id,
                name="工程父子",
                parents=[("工程父块内容", [("工程子块", [1.0, 0.0, 0.0])])],
            )
            engineering.doc_metadata = {"部门": "工程"}
            marketing = await _seed_parent_child_document(
                session,
                project_id,
                base.id,
                name="市场父子",
                parents=[("市场父块内容", [("市场子块", [1.0, 0.0, 0.0])])],
            )
            marketing.doc_metadata = {"部门": "市场"}
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        unfiltered = await harness.service.search(_request(project_id))
        assert sorted(citation.snippet for citation in unfiltered.citations) == ["工程父块内容", "市场父块内容"]

        filtered = await harness.service.search(
            _request(
                project_id,
                metadata_filters=(KnowledgeMetadataFilter(name="部门", operator="eq", value="工程"),),
            )
        )
        assert [citation.snippet for citation in filtered.citations] == ["工程父块内容"]
        # The excluded parent never reached the reranker.
        (_, _, submitted, _) = harness.client.rerank_calls[-1]
        assert submitted == ["工程父块内容"]
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# T5: real matched children, final review, debug hit diagnostics
# ---------------------------------------------------------------------------


async def _seed_matched_children_project(harness: _RetrievalHarness) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One rerank-free base mixing a parent_child and a general document.

    Parent 甲 has four children with distinct cosine scores against [1,0,0]
    (1.0, 0.8, 0.6, 0.0); parent 乙 and the general segment score 1.0.
    Returns (project_id, base_id, embedding_model_id).
    """

    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        embedding_id, _rerank_id = await _seed_models(session)
        base = _base_row(project_id, embedding_id, None, name="子命中库", default_score_threshold=0.0)
        session.add(base)
        await session.flush()
        await _seed_parent_child_document(
            session,
            project_id,
            base.id,
            name="父子文档",
            parents=[
                (
                    "父块甲的完整内容",
                    [
                        ("甲子一", [1.0, 0.0, 0.0]),
                        ("甲子二", [0.8, 0.6, 0.0]),
                        ("甲子三", [0.6, 0.8, 0.0]),
                        ("甲子四", [0.0, 1.0, 0.0]),
                    ],
                ),
                ("父块乙的完整内容", [("乙子一", [1.0, 0.0, 0.0])]),
            ],
        )
        general_document = _document_row(project_id, base.id, name="普通文档")
        session.add(general_document)
        await session.flush()
        session.add(_segment_row(general_document, position=1, content="普通模式段落", embedding=[1.0, 0.0, 0.0]))
    harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]
    return project_id, base.id, embedding_id


@pytest.mark.asyncio
async def test_parent_child_hits_carry_real_matched_children_from_the_recall_snapshot(postgres_database_url: str) -> None:
    """Hits expose the really-recalled child ids/scores; general hits stay empty."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _ = await _seed_matched_children_project(harness)

        result = await harness.service.search(_request(project_id, top_k=4))

        hits_by_snippet = {hit.citation.snippet: hit for hit in result.hits}
        assert set(hits_by_snippet) == {"父块甲的完整内容", "父块乙的完整内容", "普通模式段落"}

        async with harness.factory() as session:
            child_rows = list((await session.scalars(select(KnowledgeSegmentChildRow))).all())
        child_ids_by_content = {row.content: row.id for row in child_rows}

        strong = hits_by_snippet["父块甲的完整内容"].matched_children
        # At most three really-recalled children, best score first, never
        # reconstructed after the fact: ids must be the seeded child rows.
        assert [child.child_id for child in strong] == [
            child_ids_by_content["甲子一"],
            child_ids_by_content["甲子二"],
            child_ids_by_content["甲子三"],
        ]
        assert [child.position for child in strong] == [1, 2, 3]
        assert all(child.route == "semantic" for child in strong)
        assert [child.score for child in strong] == [
            pytest.approx(1.0),
            pytest.approx(0.8),
            pytest.approx(0.6),
        ]

        [only] = hits_by_snippet["父块乙的完整内容"].matched_children
        assert only.child_id == child_ids_by_content["乙子一"]
        assert only.score == pytest.approx(1.0)

        assert hits_by_snippet["普通模式段落"].matched_children == ()
    finally:
        await harness.engine.dispose()


def _rerank_side_effect(harness: _RetrievalHarness, side_effect) -> None:  # noqa: ANN001
    """Run ``side_effect()`` after the reranker scores, before the final review."""

    original_rerank = harness.client.rerank
    fired = False

    async def _rerank_then_mutate(material, query, documents, top_n, **hooks):  # noqa: ANN001, ANN202
        nonlocal fired
        scores = await original_rerank(material, query, documents, top_n, **hooks)
        if not fired:
            fired = True
            await side_effect()
        return scores

    harness.client.rerank = _rerank_then_mutate  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_final_review_drops_hits_whose_content_changed_during_provider_wait(postgres_database_url: str) -> None:
    """A segment edited between recall and return is dropped, never backfilled."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("第一段原文", [1.0, 0.0, 0.0]), ("第二段原文", [0.9, 0.43589, 0.0])],
        )

        async def _edit_first_segment() -> None:
            async with harness.factory() as session, session.begin():
                segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.content == "第一段原文"))
                assert segment is not None
                segment.content = "第一段被并发编辑后的新内容"

        _rerank_side_effect(harness, _edit_first_segment)

        result = await harness.service.search(_request(project_id, top_k=2, query="并发编辑"))

        # The stale hit is dropped without backfilling; fewer than top_k return.
        assert [hit.citation.snippet for hit in result.hits] == ["第二段原文"]

        rows = await _query_rows(harness, project_id)
        assert [(row.query, row.result_count) for row in rows] == [("并发编辑", 1)]
        async with harness.factory() as session:
            edited = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.position == 1))
            kept = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.position == 2))
        assert edited is not None and edited.hit_count == 0
        assert kept is not None and kept.hit_count == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["segment_disabled", "document_disabled", "document_reingested"])
async def test_final_review_drops_rows_that_left_recall_scope_during_provider_wait(postgres_database_url: str, which: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        # Two documents so a document-level change only strips its own hit.
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="复核范围库")
            session.add(base)
            await session.flush()
            target_document = _document_row(project_id, base.id, name="目标文档")
            bystander_document = _document_row(project_id, base.id, name="陪跑文档")
            session.add_all([target_document, bystander_document])
            await session.flush()
            session.add(_segment_row(target_document, position=1, content="目标段落", embedding=[1.0, 0.0, 0.0]))
            session.add(_segment_row(bystander_document, position=1, content="陪跑段落", embedding=[0.9, 0.43589, 0.0]))
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        async def _mutate() -> None:
            async with harness.factory() as session, session.begin():
                segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.content == "目标段落"))
                assert segment is not None
                if which == "segment_disabled":
                    segment.enabled = False
                elif which == "document_disabled":
                    document = await session.get(KnowledgeDocumentRow, segment.knowledge_document_id)
                    assert document is not None
                    document.enabled = False
                else:
                    document = await session.get(KnowledgeDocumentRow, segment.knowledge_document_id)
                    assert document is not None
                    document.version = document.version + 1

        _rerank_side_effect(harness, _mutate)

        result = await harness.service.search(_request(project_id, top_k=2))

        assert [hit.citation.snippet for hit in result.hits] == ["陪跑段落"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_final_review_reapplies_metadata_filters_but_keeps_renamed_documents(postgres_database_url: str) -> None:
    """Metadata reassignment during rerank drops the hit; a rename never does."""

    harness = await _harness(postgres_database_url)
    try:
        project_id = await _seed_metadata_documents(harness)

        async def _reassign_and_rename() -> None:
            async with harness.factory() as session, session.begin():
                engineering = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.name == "工程文档"))
                assert engineering is not None
                # Reassignment does not bump the content generation, so only
                # the re-applied hard filter can catch it.
                engineering.doc_metadata = {"部门": "已转出", "year": 2024}
                engineering.name = "工程文档（已改名）"

        _rerank_side_effect(harness, _reassign_and_rename)

        result = await harness.service.search(
            _request(
                project_id,
                metadata_filters=(KnowledgeMetadataFilter(name="部门", operator="eq", value="工程"),),
            )
        )
        assert result.hits == ()

        # A rename alone (no metadata change) never drops the hit: the second
        # search filters on the year the renamed document still carries.
        result = await harness.service.search(
            _request(
                project_id,
                metadata_filters=(KnowledgeMetadataFilter(name="year", operator="lte", value=2024),),
            )
        )
        assert [hit.citation.snippet for hit in result.hits] == ["工程段落"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["embedding", "reranker"])
async def test_final_review_conflicts_when_the_base_rebinds_models_mid_search(postgres_database_url: str, which: str) -> None:
    """A strategy change during provider work is a conflict, not a silent result."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, _ = await _seed_single_base(
            harness,
            segments=[("换绑期间的段落", [1.0, 0.0, 0.0])],
        )

        async def _rebind() -> None:
            async with harness.factory() as session, session.begin():
                other_embedding_id, other_rerank_id = await _seed_models(session)
                base = await session.get(KnowledgeBaseRow, base_id)
                assert base is not None
                if which == "embedding":
                    base.embedding_model_id = other_embedding_id
                else:
                    base.reranker_model_id = other_rerank_id

        _rerank_side_effect(harness, _rebind)

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))

        assert error.value.code == KNOWLEDGE_CONFLICT
        assert await _query_rows(harness, project_id) == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting", "new_value"),
    [("default_score_threshold", 0.9), ("default_top_k", 1), ("retrieval_mode", "hybrid")],
)
@pytest.mark.parametrize("provider_stage", ["embedding", "rerank"])
async def test_search_conflicts_when_effective_base_settings_change_during_provider_wait(
    postgres_database_url: str,
    setting: str,
    new_value: Any,
    provider_stage: str,
) -> None:
    """Results computed under replaced effective settings must be retried."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, rerank_id = await _seed_single_base(
            harness,
            segments=[("旧策略下的命中", [1.0, 0.0, 0.0])],
        )
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=0, score=0.5)]

        async def _change_settings() -> None:
            async with harness.factory() as session, session.begin():
                base = await session.get(KnowledgeBaseRow, base_id)
                assert base is not None
                setattr(base, setting, new_value)

        if provider_stage == "rerank":
            _rerank_side_effect(harness, _change_settings)
        else:
            original_embed = harness.client.embed

            async def _embed_then_change(material, texts, **hooks):  # noqa: ANN001, ANN202
                vectors = await original_embed(material, texts, **hooks)
                await _change_settings()
                return vectors

            harness.client.embed = _embed_then_change  # type: ignore[method-assign]

        with pytest.raises(KnowledgeError) as error:
            await harness.service.search(_request(project_id))

        assert error.value.code == KNOWLEDGE_CONFLICT
        if provider_stage == "embedding":
            assert harness.client.rerank_calls == []
        history, total = await harness.service.list_recent_queries(project_id, _DEFAULT_OWNER_USER_ID, base_id)
        assert (history, total) == ([], 0)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting", "new_value", "request_override"),
    [
        ("default_score_threshold", 0.9, {"score_threshold": 0}),
        ("default_top_k", 1, {"top_k": 2}),
        ("retrieval_mode", "hybrid", {"retrieval_mode": "semantic"}),
    ],
)
async def test_search_request_override_ignores_changes_to_unused_base_defaults(
    postgres_database_url: str,
    setting: str,
    new_value: Any,
    request_override: dict[str, Any],
) -> None:
    """A base edit must not conflict with a parameter pinned by this request."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _, rerank_id = await _seed_single_base(
            harness,
            segments=[("请求覆盖参数下的命中", [1.0, 0.0, 0.0])],
        )
        harness.client.rerank_scripts[rerank_id] = lambda documents, top_n: [RerankScore(index=0, score=0.5)]

        async def _change_unused_default() -> None:
            async with harness.factory() as session, session.begin():
                base = await session.get(KnowledgeBaseRow, base_id)
                assert base is not None
                setattr(base, setting, new_value)

        _rerank_side_effect(harness, _change_unused_default)

        result = await harness.service.search(_request(project_id, **request_override))

        assert [hit.passage for hit in result.hits] == ["请求覆盖参数下的命中"]
        history, total = await harness.service.list_recent_queries(project_id, _DEFAULT_OWNER_USER_ID, base_id)
        assert total == 1
        assert history[0].result_count == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_search_ignores_default_top_k_changes_that_leave_the_effective_limit_unchanged(postgres_database_url: str) -> None:
    """A multi-base search uses the largest default, not every default as a cap."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, embedding_id, rerank_id = await _seed_single_base(
            harness,
            segments=[("较小默认值库的命中", [1.0, 0.0, 0.0])],
        )
        async with harness.factory() as session, session.begin():
            larger_default_base = _base_row(project_id, embedding_id, rerank_id, name="默认八条", default_top_k=8)
            session.add(larger_default_base)

        async def _lower_nonmaximal_default() -> None:
            async with harness.factory() as session, session.begin():
                base = await session.get(KnowledgeBaseRow, base_id)
                assert base is not None
                base.default_top_k = 2

        _rerank_side_effect(harness, _lower_nonmaximal_default)

        result = await harness.service.search(_request(project_id, debug=True))

        assert [hit.passage for hit in result.hits] == ["较小默认值库的命中"]
        assert result.diagnostics is not None
        assert result.diagnostics.effective_top_k == 8
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_final_review_drops_hits_whose_matched_children_were_replaced(postgres_database_url: str) -> None:
    """Child identities are re-verified: swapped child rows invalidate the hit."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            embedding_id, rerank_id = await _seed_models(session)
            base = _base_row(project_id, embedding_id, rerank_id, name="子替换库")
            session.add(base)
            await session.flush()
            await _seed_parent_child_document(
                session,
                project_id,
                base.id,
                name="父子文档",
                parents=[("父块内容", [("子块", [1.0, 0.0, 0.0])])],
            )
        harness.client.query_vectors[embedding_id] = [1.0, 0.0, 0.0]

        async def _swap_child_rows() -> None:
            async with harness.factory() as session, session.begin():
                child = await session.scalar(select(KnowledgeSegmentChildRow))
                assert child is not None
                replacement = KnowledgeSegmentChildRow(
                    id=uuid.uuid4(),
                    project_id=child.project_id,
                    knowledge_base_id=child.knowledge_base_id,
                    knowledge_document_id=child.knowledge_document_id,
                    knowledge_segment_id=child.knowledge_segment_id,
                    document_version=child.document_version,
                    position=child.position,
                    content=child.content,
                    word_count=child.word_count,
                    embedding=[1.0, 0.0, 0.0],
                )
                await session.delete(child)
                await session.flush()
                session.add(replacement)

        _rerank_side_effect(harness, _swap_child_rows)

        result = await harness.service.search(_request(project_id))

        assert result.hits == ()
        rows = await _query_rows(harness, project_id)
        assert [row.result_count for row in rows] == [0]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_debug_search_projects_hit_diagnostics_for_the_final_hits_only(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, embedding_id = await _seed_matched_children_project(harness)

        plain = await harness.service.search(_request(project_id, top_k=2))
        assert plain.diagnostics is None

        result = await harness.service.search(_request(project_id, top_k=2, debug=True))

        diagnostics = result.diagnostics
        assert diagnostics is not None
        assert diagnostics.strategy_version == KNOWLEDGE_STRATEGY_VERSION
        assert diagnostics.target_base_count == 1
        assert diagnostics.effective_top_k == 2
        assert diagnostics.retrieval_mode == "semantic"
        assert diagnostics.model_ids == (embedding_id,)
        assert diagnostics.ranking_method == "cosine"

        assert len(result.hits) == 2
        assert [entry.segment_id for entry in diagnostics.hit_diagnostics] == [hit.citation.segment_id for hit in result.hits]
        for entry, hit in zip(diagnostics.hit_diagnostics, result.hits, strict=True):
            assert entry.local_score == hit.local_score
            assert entry.local_score_kind == hit.local_score_kind
            assert entry.score_domain == hit.score_domain
            assert entry.ranking_method == hit.ranking_method
            assert entry.ranking_score == hit.ranking_score
            assert entry.matched_children == hit.matched_children
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# T6: builtin filter fields (document authority columns)
# ---------------------------------------------------------------------------


async def _seed_builtin_filter_documents(harness: _RetrievalHarness) -> uuid.UUID:
    """Two documents whose authority columns differ; identical vectors.

    发布说明: original 发布说明.PDF, uploaded 2026-08-01, custom metadata
    {"file_type": "规范"}. 安装手册: original install.md, uploaded 2026-08-20.
    A custom field named file_type coexists with the builtin one.
    """

    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        embedding_id, _ = await _seed_models(session)
        base = _base_row(project_id, embedding_id, None, name="内建过滤库")
        session.add(base)
        await session.flush()
        session.add(
            KnowledgeMetadataFieldRow(
                id=uuid.uuid4(),
                project_id=project_id,
                knowledge_base_id=base.id,
                name="file_type",
                field_type="string",
            )
        )

        release = _document_row(project_id, base.id, name="发布说明")
        release.original_name = "发布说明.PDF"
        release.created_at = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
        release.doc_metadata = {"file_type": "规范"}
        manual = _document_row(project_id, base.id, name="安装手册")
        manual.original_name = "install.md"
        manual.created_at = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
        session.add_all([release, manual])
        await session.flush()

        vector = [1.0, 0.0, 0.0]
        session.add_all(
            [
                _segment_row(release, position=1, content="发布段落", embedding=vector),
                _segment_row(manual, position=1, content="安装段落", embedding=vector),
            ]
        )
    harness.client.query_vectors[embedding_id] = vector
    return project_id


@pytest.mark.asyncio
async def test_builtin_filters_project_document_authority_columns(postgres_database_url: str) -> None:
    """Builtin conditions read the live document columns, never doc_metadata."""

    harness = await _harness(postgres_database_url)
    try:
        project_id = await _seed_builtin_filter_documents(harness)

        async def _snippets(*filters: KnowledgeMetadataFilter) -> list[str]:
            result = await harness.service.search(_request(project_id, metadata_filters=filters))
            return sorted(hit.citation.snippet for hit in result.hits)

        name_hits = await _snippets(KnowledgeMetadataFilter(name="document_name", operator="contains", value="发布", field_kind="builtin"))
        assert name_hits == ["发布段落"]

        # The extension is matched case-insensitively (.PDF → pdf).
        type_hits = await _snippets(KnowledgeMetadataFilter(name="file_type", operator="eq", value="pdf", field_kind="builtin"))
        assert type_hits == ["发布段落"]

        cutoff = datetime(2026, 8, 10, 0, 0, tzinfo=UTC).timestamp()
        newer = await _snippets(KnowledgeMetadataFilter(name="uploaded_at", operator="gte", value=cutoff, field_kind="builtin"))
        assert newer == ["安装段落"]
        older = await _snippets(KnowledgeMetadataFilter(name="uploaded_at", operator="lte", value=cutoff, field_kind="builtin"))
        assert older == ["发布段落"]

        everyone = await _snippets(KnowledgeMetadataFilter(name="source_type", operator="eq", value="file_upload", field_kind="builtin"))
        assert everyone == ["发布段落", "安装段落"]
        nobody = await _snippets(KnowledgeMetadataFilter(name="source_type", operator="eq", value="s3", field_kind="builtin"))
        assert nobody == []

        # The custom field of the same name addresses doc_metadata, not the
        # extension: only 发布说明 carries {"file_type": "规范"}.
        custom_hits = await _snippets(KnowledgeMetadataFilter(name="file_type", operator="eq", value="规范"))
        assert custom_hits == ["发布段落"]
        custom_pdf = await _snippets(KnowledgeMetadataFilter(name="file_type", operator="eq", value="pdf"))
        assert custom_pdf == []

        # Builtin and custom conditions AND together like any other filters.
        combined = await _snippets(
            KnowledgeMetadataFilter(name="document_name", operator="contains", value="发布", field_kind="builtin"),
            KnowledgeMetadataFilter(name="file_type", operator="eq", value="规范"),
        )
        assert combined == ["发布段落"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_final_review_reapplies_builtin_filters_after_document_rename(postgres_database_url: str) -> None:
    """A rename during provider wait must not leak past a document_name filter."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _, _, _ = await _seed_single_base(
            harness,
            segments=[("发布相关段落", [1.0, 0.0, 0.0])],
        )
        async with harness.factory() as session, session.begin():
            document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id))
            assert document is not None
            document.name = "发布说明"

        name_filter = KnowledgeMetadataFilter(name="document_name", operator="contains", value="发布", field_kind="builtin")

        # Positive control: without concurrent mutation the filter matches.
        control = await harness.service.search(_request(project_id, metadata_filters=(name_filter,)))
        assert [hit.citation.snippet for hit in control.hits] == ["发布相关段落"]

        async def _rename_document() -> None:
            async with harness.factory() as session, session.begin():
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id))
                assert row is not None
                row.name = "安装说明"

        _rerank_side_effect(harness, _rename_document)

        result = await harness.service.search(_request(project_id, metadata_filters=(name_filter,)))

        assert result.hits == ()
        rows = await _query_rows(harness, project_id)
        assert [row.result_count for row in rows] == [1, 0]
    finally:
        await harness.engine.dispose()
