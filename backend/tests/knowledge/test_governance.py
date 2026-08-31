"""K1 gates: segment editing/creation/deletion and document governance.

Service tests run against the installed Schema V1 snapshot with a scripted
model client, proving synchronous re-embedding, word-count bookkeeping,
version-race conflicts, quotas, and batch all-or-nothing semantics. HTTP
tests pin the new route contracts over ASGI with a fake module.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeModule,
    KnowledgeSegmentCreate,
    KnowledgeSegmentUpdate,
    KnowledgeSegmentView,
    KnowledgeSettings,
)
from actweave_knowledge.contracts import KNOWLEDGE_LEXICAL_VERSION
from actweave_knowledge.documents import KnowledgeDocumentService
from actweave_knowledge.models.client import KnowledgeModelClient
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.retrieval import encode_lexical_token
from actweave_knowledge.segments import KnowledgeSegmentService
from actweave_knowledge.segments.service import MAX_SEGMENT_CONTENT_CHARS
from fastapi import FastAPI
from registry_helpers import registry_model_port, seed_embedding_model, seed_provider
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge import gateway
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.model_registry import ModelProviderModelRow

# ---------------------------------------------------------------------------
# Fakes and harness
# ---------------------------------------------------------------------------


class _ScriptedEmbedClient:
    """Embedding double returning a fixed vector; optionally runs a hook first."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.embed_calls: list[list[str]] = []
        self.before_embed = None

    async def embed(self, material, texts: list[str], *, batch_guard=None, on_batch_verified=None) -> list[list[float]]:  # noqa: ANN001
        if batch_guard is not None:
            await batch_guard()
        self.embed_calls.append(list(texts))
        if self.before_embed is not None:
            await self.before_embed()
        if on_batch_verified is not None:
            await on_batch_verified(len(texts))
        return [list(self.vector) for _ in texts]

    async def rerank(self, material, query, documents, top_n, *, batch_guard=None):  # noqa: ANN001
        raise AssertionError("segment governance never reranks")


class _RevokedAfterProviderAuthority:
    """Trusted request authority that is revoked before the write transaction."""

    def __init__(self, project_id: uuid.UUID) -> None:
        self.project_id = project_id
        self.actor_user_id = uuid.uuid4()
        self.calls = 0
        self.revoked = False

    async def revalidate(self, session: AsyncSession) -> None:
        del session
        self.calls += 1
        if self.revoked:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")


class _Harness:
    def __init__(self, engine, factory, client: _ScriptedEmbedClient, service: KnowledgeSegmentService) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.client = client
        self.service = service


async def _harness(postgres_database_url: str, **settings_overrides: object) -> _Harness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    client = _ScriptedEmbedClient([0.5, 0.5, 0.0])
    settings = KnowledgeSettings.model_validate({"enabled": False, **settings_overrides})
    service = KnowledgeSegmentService(
        session_factory=factory,
        settings=settings,
        client=client,  # type: ignore[arg-type]
        model_port=registry_model_port(),
    )
    return _Harness(engine, factory, client, service)


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
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"k1_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"k1-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


class _Seeded(SimpleNamespace):
    project_id: uuid.UUID
    base_id: uuid.UUID
    embedding_model_id: uuid.UUID
    document_id: uuid.UUID
    segment_ids: list[uuid.UUID]


async def _seed_ready_document(
    harness: _Harness,
    *,
    segments: list[str],
    base_status: str = "active",
    model_status: str = "active",
    document_status: str = "ready",
    project_id: uuid.UUID | None = None,
    chunking_mode: str = "general",
    child_chunk_size: int = 500,
    child_chunk_separator: str = "\\n",
) -> _Seeded:
    """Project + registry embedding model + base + one document with ``segments`` on v1.

    In ``parent_child`` mode parents get NULL embeddings and one seeded child
    row each, mirroring what the ingestion pipeline publishes.
    """

    provider_id = await seed_provider(harness.factory)
    embedding_model_id = await seed_embedding_model(harness.factory, provider_id, status=model_status, dimension=3)
    async with harness.factory() as session, session.begin():
        if project_id is None:
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base = KnowledgeBaseRow(
            id=uuid.uuid4(),
            project_id=project_id,
            name=f"base-{uuid.uuid4().hex[:6]}",
            embedding_model_id=embedding_model_id,
            status=base_status,
        )
        session.add(base)
        await session.flush()
        document_id = uuid.uuid4()
        document = KnowledgeDocumentRow(
            id=document_id,
            project_id=project_id,
            knowledge_base_id=base.id,
            name="手册",
            original_name="手册.md",
            storage_key=f"projects/{project_id}/knowledge/{base.id}/{document_id}.md",
            size_bytes=64,
            status=document_status,
            version=1,
            chunk_size=1000,
            chunk_overlap=100,
            chunking_mode=chunking_mode,
            child_chunk_size=child_chunk_size,
            child_chunk_separator=child_chunk_separator,
            segment_count=len(segments),
            word_count=sum(len(content) for content in segments),
        )
        session.add(document)
        await session.flush()
        parent_child = chunking_mode == "parent_child"
        segment_ids: list[uuid.UUID] = []
        for index, content in enumerate(segments, start=1):
            segment = KnowledgeSegmentRow(
                id=uuid.uuid4(),
                project_id=project_id,
                knowledge_base_id=base.id,
                knowledge_document_id=document_id,
                document_version=1,
                position=index,
                content=content,
                word_count=len(content),
                source_position={"page": index},
                embedding=None if parent_child else [1.0, 0.0, 0.0],
            )
            session.add(segment)
            segment_ids.append(segment.id)
            if parent_child:
                await session.flush()
                session.add(
                    KnowledgeSegmentChildRow(
                        id=uuid.uuid4(),
                        project_id=project_id,
                        knowledge_base_id=base.id,
                        knowledge_document_id=document_id,
                        knowledge_segment_id=segment.id,
                        document_version=1,
                        position=1,
                        content=content,
                        word_count=len(content),
                        embedding=[1.0, 0.0, 0.0],
                    )
                )
    return _Seeded(
        project_id=project_id,
        base_id=base.id,
        embedding_model_id=embedding_model_id,
        document_id=document_id,
        segment_ids=segment_ids,
    )


async def _document_row_of(harness: _Harness, document_id: uuid.UUID) -> KnowledgeDocumentRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeDocumentRow, document_id)
        assert row is not None
        return row


async def _segment_row_of(harness: _Harness, segment_id: uuid.UUID) -> KnowledgeSegmentRow | None:
    async with harness.factory() as session:
        return await session.get(KnowledgeSegmentRow, segment_id)


# ---------------------------------------------------------------------------
# Segment editing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_segment_content_reembeds_and_reconciles_word_counts(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["旧内容一二三", "第二段"])
        view = await harness.service.update_segment(
            seeded.project_id,
            seeded.segment_ids[0],
            KnowledgeSegmentUpdate(content="  全新的分段内容  "),
        )

        assert view.content == "全新的分段内容"
        assert view.word_count == len("全新的分段内容")
        assert view.enabled is True
        assert harness.client.embed_calls == [["全新的分段内容"]]

        segment = await _segment_row_of(harness, seeded.segment_ids[0])
        assert segment is not None
        assert [round(float(value), 3) for value in segment.embedding] == [0.5, 0.5, 0.0]
        document = await _document_row_of(harness, seeded.document_id)
        assert document.word_count == len("全新的分段内容") + len("第二段")
        assert document.segment_count == 2
    finally:
        await harness.engine.dispose()


async def _lexical_state(harness: _Harness, table: str, row_id: uuid.UUID, term: str) -> tuple[int, bool]:
    """(lexical_version, whether the row's tsvector matches ``term``)."""

    async with harness.factory() as session:
        row = (
            await session.execute(
                text(f"SELECT lexical_version, lexical_tsv @@ to_tsquery('simple', :token) FROM {table} WHERE id = :id"),
                {"token": encode_lexical_token(term), "id": row_id},
            )
        ).one()
    return int(row[0]), bool(row[1])


@pytest.mark.asyncio
async def test_content_writes_maintain_lexical_fields_in_the_same_transaction(postgres_database_url: str) -> None:
    """T8: edits and manual additions refresh lexical_v1 fields with the text."""

    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["旧版网络说明"])
        await harness.service.update_segment(
            seeded.project_id,
            seeded.segment_ids[0],
            KnowledgeSegmentUpdate(content="新版存储指南"),
        )
        version, hits_new = await _lexical_state(harness, "knowledge_segments", seeded.segment_ids[0], "存储")
        assert version == KNOWLEDGE_LEXICAL_VERSION
        assert hits_new is True
        _, hits_old = await _lexical_state(harness, "knowledge_segments", seeded.segment_ids[0], "网络")
        assert hits_old is False

        created = await harness.service.create_segment(
            seeded.project_id,
            seeded.document_id,
            KnowledgeSegmentCreate(content="手工新增错误码e404"),
        )
        version, hits_created = await _lexical_state(harness, "knowledge_segments", created.id, "e404")
        assert version == KNOWLEDGE_LEXICAL_VERSION
        assert hits_created is True
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_parent_child_content_edit_refreshes_child_lexical_fields(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["网络说明"], chunking_mode="parent_child")
        await harness.service.update_segment(
            seeded.project_id,
            seeded.segment_ids[0],
            KnowledgeSegmentUpdate(content="存储指南"),
        )
        parent_version, parent_hits = await _lexical_state(harness, "knowledge_segments", seeded.segment_ids[0], "存储")
        assert parent_version == KNOWLEDGE_LEXICAL_VERSION
        assert parent_hits is True
        async with harness.factory() as session:
            child_rows = (
                await session.execute(
                    text(
                        """SELECT lexical_version, lexical_tsv @@ to_tsquery('simple', :token)
                           FROM knowledge_segment_children WHERE knowledge_segment_id = :id"""
                    ),
                    {"token": encode_lexical_token("存储"), "id": seeded.segment_ids[0]},
                )
            ).all()
        assert child_rows
        assert all(int(version) == KNOWLEDGE_LEXICAL_VERSION and bool(hits) for version, hits in child_rows)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_segment_enabled_toggle_never_embeds(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段", "第二段"])
        view = await harness.service.update_segment(
            seeded.project_id,
            seeded.segment_ids[1],
            KnowledgeSegmentUpdate(enabled=False),
        )

        assert view.enabled is False
        assert harness.client.embed_calls == []
        segment = await _segment_row_of(harness, seeded.segment_ids[1])
        assert segment is not None
        assert segment.enabled is False
        # The vector survives disabling; re-enabling needs no re-embedding.
        assert [round(float(value), 3) for value in segment.embedding] == [1.0, 0.0, 0.0]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "fragment"),
    [
        (KnowledgeSegmentUpdate(), "至少提供一个"),
        (KnowledgeSegmentUpdate(content="   "), "content"),
        (KnowledgeSegmentUpdate(content="长" * (MAX_SEGMENT_CONTENT_CHARS + 1)), "content"),
    ],
)
async def test_update_segment_rejects_invalid_input(postgres_database_url: str, update: KnowledgeSegmentUpdate, fragment: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段"])
        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_segment(seeded.project_id, seeded.segment_ids[0], update)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert fragment in error.value.message
        assert harness.client.embed_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_segment_requires_ready_document_and_current_version(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        processing = await _seed_ready_document(harness, segments=["第一段"], document_status="processing")
        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_segment(processing.project_id, processing.segment_ids[0], KnowledgeSegmentUpdate(enabled=False))
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert "ready" in error.value.message

        stale = await _seed_ready_document(harness, segments=["第一段"])
        async with harness.factory() as session, session.begin():
            row = await session.get(KnowledgeDocumentRow, stale.document_id)
            assert row is not None
            row.version = 2
        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_segment(stale.project_id, stale.segment_ids[0], KnowledgeSegmentUpdate(enabled=False))
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_segment_conflicts_when_version_moves_during_embedding(postgres_database_url: str) -> None:
    """A re-ingest winning the race must never let a stale vector publish."""

    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段"])

        async def _bump_version() -> None:
            async with harness.factory() as session, session.begin():
                row = await session.get(KnowledgeDocumentRow, seeded.document_id)
                assert row is not None
                row.version = 2

        harness.client.before_embed = _bump_version
        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_segment(seeded.project_id, seeded.segment_ids[0], KnowledgeSegmentUpdate(content="编辑后的内容"))
        assert error.value.code == KNOWLEDGE_CONFLICT

        segment = await _segment_row_of(harness, seeded.segment_ids[0])
        assert segment is not None
        assert segment.content == "第一段"
        assert [round(float(value), 3) for value in segment.embedding] == [1.0, 0.0, 0.0]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "create"])
@pytest.mark.parametrize("first_response_status", [200, 429])
async def test_segment_governance_stops_batches_and_retries_after_revocation(
    postgres_database_url: str,
    operation: str,
    first_response_status: int,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["既有内容"], chunking_mode="parent_child", child_chunk_size=100)
        async with harness.factory() as session, session.begin():
            model = await session.get(ModelProviderModelRow, seeded.embedding_model_id)
            assert model is not None
            model.max_batch = 1
        authority = _RevokedAfterProviderAuthority(seeded.project_id)
        requests: list[list[str]] = []

        async def provider(request: httpx.Request) -> httpx.Response:
            inputs = json.loads(request.content)["input"]
            requests.append(inputs)
            authority.revoked = True
            return httpx.Response(first_response_status, json={"data": [{"index": index, "embedding": [1.0, 0.0, 0.0]} for index in range(len(inputs))]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            module = KnowledgeModule(
                settings=KnowledgeSettings.model_validate(
                    {
                        "enabled": True,
                        "minio": {"endpoint": "127.0.0.1:9000", "bucket": "authority-test", "access_key": "test-access", "secret_key": "test-secret"},
                    }
                ),
                session_factory=harness.factory,
                model_port=registry_model_port(),
                model_client=KnowledgeModelClient(http),
            )
            with pytest.raises(KnowledgeError) as error:
                if operation == "update":
                    await module.update_segment(seeded.project_id, seeded.segment_ids[0], KnowledgeSegmentUpdate(content="甲" * 250), authority=authority)
                else:
                    await module.create_segment(seeded.project_id, seeded.document_id, KnowledgeSegmentCreate(content="甲" * 250), authority=authority)
            assert requests == [["甲" * 100]]
            assert error.value.code == KNOWLEDGE_NOT_FOUND
            authority.revoked = False
            segments, total = await module.list_document_segments(seeded.project_id, seeded.document_id, authority=authority)
            assert total == 1
            assert [segment.content for segment in segments] == ["既有内容"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_segment_revalidates_authority_after_embedding_before_commit(postgres_database_url: str) -> None:
    """Revocation during Provider I/O must prevent the edited content from publishing."""

    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段"])
        authority = _RevokedAfterProviderAuthority(seeded.project_id)
        module = KnowledgeModule(
            settings=KnowledgeSettings.model_validate(
                {
                    "enabled": True,
                    "minio": {
                        "endpoint": "127.0.0.1:9000",
                        "bucket": "authority-test",
                        "access_key": "test-access",
                        "secret_key": "test-secret",
                    },
                }
            ),
            session_factory=harness.factory,
            model_port=registry_model_port(),
            model_client=harness.client,  # type: ignore[arg-type]
        )

        async def _revoke_during_embedding() -> None:
            authority.revoked = True

        harness.client.before_embed = _revoke_during_embedding

        with pytest.raises(KnowledgeError) as error:
            await module.update_segment(
                seeded.project_id,
                seeded.segment_ids[0],
                KnowledgeSegmentUpdate(content="不应发布的内容"),
                authority=authority,
            )

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert harness.client.embed_calls == [["不应发布的内容"]]
        segment = await _segment_row_of(harness, seeded.segment_ids[0])
        assert segment is not None
        assert segment.content == "第一段"
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Manual segments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_segment_appends_position_marks_manual_and_counts(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段", "第二段"])
        view = await harness.service.create_segment(
            seeded.project_id,
            seeded.document_id,
            KnowledgeSegmentCreate(content="手工补充的知识点"),
        )

        assert view.position == 3
        assert view.document_version == 1
        assert view.source_position == {"manual": True}
        assert view.word_count == len("手工补充的知识点")
        assert harness.client.embed_calls == [["手工补充的知识点"]]

        document = await _document_row_of(harness, seeded.document_id)
        assert document.segment_count == 3
        assert document.word_count == len("第一段") + len("第二段") + len("手工补充的知识点")
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_segment_enforces_document_quota(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url, max_segments_per_document=2)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段", "第二段"])
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_segment(seeded.project_id, seeded.document_id, KnowledgeSegmentCreate(content="超出配额"))
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_segment_requires_active_base_and_model(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        disabled_base = await _seed_ready_document(harness, segments=["第一段"], base_status="disabled")
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_segment(disabled_base.project_id, disabled_base.document_id, KnowledgeSegmentCreate(content="内容"))
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST

        disabled_model = await _seed_ready_document(harness, segments=["第一段"], model_status="disabled")
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_segment(disabled_model.project_id, disabled_model.document_id, KnowledgeSegmentCreate(content="内容"))
        assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
        assert harness.client.embed_calls == []
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Segment deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_parent_child_segment_resplits_children_and_keeps_parent_unvectored(postgres_database_url: str) -> None:
    """K3: editing a parent re-splits and re-embeds child chunks, never the parent."""

    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(
            harness,
            segments=["旧父块内容", "另一父块"],
            chunking_mode="parent_child",
            child_chunk_size=100,
            child_chunk_separator="。",
        )
        view = await harness.service.update_segment(
            seeded.project_id,
            seeded.segment_ids[0],
            KnowledgeSegmentUpdate(content="新句子甲。" + "长句乙" * 40 + "。尾句丙。"),
        )

        segment = await _segment_row_of(harness, seeded.segment_ids[0])
        assert segment is not None
        assert segment.embedding is None  # parents stay unvectored
        async with harness.factory() as session:
            children = list((await session.scalars(select(KnowledgeSegmentChildRow).where(KnowledgeSegmentChildRow.knowledge_segment_id == seeded.segment_ids[0]).order_by(KnowledgeSegmentChildRow.position))).all())
        assert len(children) >= 2
        assert all(child.content in view.content for child in children)
        assert all(child.content != "旧父块内容" for child in children)
        assert [child.position for child in children] == list(range(1, len(children) + 1))
        assert all([round(float(value), 3) for value in child.embedding] == [0.5, 0.5, 0.0] for child in children)
        # The embed call carried the child chunks, not the parent text.
        assert harness.client.embed_calls == [[child.content for child in children]]
        # The untouched sibling keeps its original child.
        async with harness.factory() as session:
            sibling_children = list((await session.scalars(select(KnowledgeSegmentChildRow).where(KnowledgeSegmentChildRow.knowledge_segment_id == seeded.segment_ids[1]))).all())
        assert [child.content for child in sibling_children] == ["另一父块"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_segment_in_parent_child_document_embeds_children_only(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(
            harness,
            segments=["既有父块"],
            chunking_mode="parent_child",
            child_chunk_size=100,
            child_chunk_separator="。",
        )
        view = await harness.service.create_segment(
            seeded.project_id,
            seeded.document_id,
            KnowledgeSegmentCreate(content="手工父块第一句。手工父块第二句。"),
        )

        assert view.position == 2
        segment = await _segment_row_of(harness, uuid.UUID(str(view.id)))
        assert segment is not None
        assert segment.embedding is None
        async with harness.factory() as session:
            children = list((await session.scalars(select(KnowledgeSegmentChildRow).where(KnowledgeSegmentChildRow.knowledge_segment_id == segment.id).order_by(KnowledgeSegmentChildRow.position))).all())
        assert children and all(child.content in view.content for child in children)
        assert harness.client.embed_calls == [[child.content for child in children]]
        document = await _document_row_of(harness, seeded.document_id)
        assert document.segment_count == 2
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_parent_child_segment_enforces_vector_entry_budget_before_embedding(
    postgres_database_url: str,
) -> None:
    harness = await _harness(
        postgres_database_url,
        max_segments_per_document=2,
    )
    try:
        seeded = await _seed_ready_document(
            harness,
            segments=["既有父块"],
            chunking_mode="parent_child",
            child_chunk_size=100,
        )

        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_segment(
                seeded.project_id,
                seeded.document_id,
                KnowledgeSegmentCreate(content="新" * 250),
            )

        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert "向量条目" in error.value.message
        assert harness.client.embed_calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_parent_child_segment_enforces_total_vector_budget_before_embedding(
    postgres_database_url: str,
) -> None:
    harness = await _harness(
        postgres_database_url,
        max_segments_per_document=2,
    )
    try:
        seeded = await _seed_ready_document(
            harness,
            segments=["父块一", "父块二"],
            chunking_mode="parent_child",
            child_chunk_size=100,
        )

        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_segment(
                seeded.project_id,
                seeded.segment_ids[0],
                KnowledgeSegmentUpdate(content="替换" * 80),
            )

        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert "向量条目" in error.value.message
        assert harness.client.embed_calls == []
        segment = await _segment_row_of(harness, seeded.segment_ids[0])
        assert segment is not None and segment.content == "父块一"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_parent_child_segment_rechecks_vector_budget_after_embedding(
    postgres_database_url: str,
) -> None:
    """A concurrent manual edit cannot consume the budget during Provider I/O."""

    harness = await _harness(
        postgres_database_url,
        max_segments_per_document=3,
    )
    try:
        seeded = await _seed_ready_document(
            harness,
            segments=["父块一", "父块二"],
            chunking_mode="parent_child",
            child_chunk_size=100,
        )

        async def _consume_last_entry() -> None:
            async with harness.factory() as session, session.begin():
                sibling = await session.get(
                    KnowledgeSegmentRow,
                    seeded.segment_ids[1],
                )
                assert sibling is not None
                session.add(
                    KnowledgeSegmentChildRow(
                        id=uuid.uuid4(),
                        project_id=seeded.project_id,
                        knowledge_base_id=seeded.base_id,
                        knowledge_document_id=seeded.document_id,
                        knowledge_segment_id=sibling.id,
                        document_version=1,
                        position=2,
                        content="并发新增子块",
                        word_count=len("并发新增子块"),
                        embedding=[1.0, 0.0, 0.0],
                    )
                )

        harness.client.before_embed = _consume_last_entry
        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_segment(
                seeded.project_id,
                seeded.segment_ids[0],
                KnowledgeSegmentUpdate(content="新" * 150),
            )

        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert len(harness.client.embed_calls) == 1
        segment = await _segment_row_of(harness, seeded.segment_ids[0])
        assert segment is not None and segment.content == "父块一"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_segment_removes_row_and_updates_document(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段", "第二段"])
        view = await harness.service.delete_segment(seeded.project_id, seeded.segment_ids[0])

        assert view.id == seeded.document_id
        assert view.segment_count == 1
        assert view.word_count == len("第二段")
        assert await _segment_row_of(harness, seeded.segment_ids[0]) is None
        assert await _segment_row_of(harness, seeded.segment_ids[1]) is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_segment_operations_scope_to_the_project(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段"])
        other_project = uuid.uuid4()
        for call in (
            harness.service.update_segment(other_project, seeded.segment_ids[0], KnowledgeSegmentUpdate(enabled=False)),
            harness.service.delete_segment(other_project, seeded.segment_ids[0]),
            harness.service.create_segment(other_project, seeded.document_id, KnowledgeSegmentCreate(content="内容")),
        ):
            with pytest.raises(KnowledgeError) as error:
                await call
            assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Document governance (rename, batch enable/disable, batch delete)
# ---------------------------------------------------------------------------


def _documents_service(harness: _Harness) -> KnowledgeDocumentService:
    settings = KnowledgeSettings.model_validate({"enabled": False})
    return KnowledgeDocumentService(
        session_factory=harness.factory,
        settings=settings,
        object_store=SimpleNamespace(),  # type: ignore[arg-type] - governance never touches storage
    )


@pytest.mark.asyncio
async def test_rename_document_changes_name_only(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段"])
        service = _documents_service(harness)
        view = await service.rename_document(seeded.project_id, seeded.document_id, "  新名字.md  ")

        assert view.name == "新名字.md"
        assert view.original_name == "手册.md"

        with pytest.raises(KnowledgeError) as error:
            await service.rename_document(seeded.project_id, seeded.document_id, "   ")
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_set_documents_enabled_is_atomic_and_all_or_nothing(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        first = await _seed_ready_document(harness, segments=["第一段"])
        second = await _seed_ready_document(harness, segments=["第二段"], project_id=first.project_id)
        service = _documents_service(harness)

        views = await service.set_documents_enabled(first.project_id, [first.document_id, second.document_id], False)
        assert [view.enabled for view in views] == [False, False]
        assert [view.id for view in views] == [first.document_id, second.document_id]

        # One unknown id fails the whole batch and flips nothing back.
        with pytest.raises(KnowledgeError) as error:
            await service.set_documents_enabled(first.project_id, [first.document_id, uuid.uuid4()], True)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
        row = await _document_row_of(harness, first.document_id)
        assert row.enabled is False

        views = await service.set_documents_enabled(first.project_id, [first.document_id, second.document_id], True)
        assert [view.enabled for view in views] == [True, True]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_documents_marks_deleting_and_enqueues_tasks(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        seeded = await _seed_ready_document(harness, segments=["第一段"])
        service = _documents_service(harness)
        views = await service.delete_documents(seeded.project_id, [seeded.document_id])

        assert views[0].status == "deleting"
        assert views[0].version == 2
        async with harness.factory() as session:
            tasks = (
                await session.scalars(
                    select(KnowledgeTaskRow).where(
                        KnowledgeTaskRow.resource_id == seeded.document_id,
                        KnowledgeTaskRow.kind == "delete_document",
                    )
                )
            ).all()
        assert len(tasks) == 1

        # A second call while the delete task is open must not enqueue another.
        views = await service.delete_documents(seeded.project_id, [seeded.document_id])
        assert views[0].status == "deleting"
        async with harness.factory() as session:
            count = len(
                (
                    await session.scalars(
                        select(KnowledgeTaskRow).where(
                            KnowledgeTaskRow.resource_id == seeded.document_id,
                            KnowledgeTaskRow.kind == "delete_document",
                        )
                    )
                ).all()
            )
        assert count == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_document_batches_reject_empty_and_oversized_id_lists(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        service = _documents_service(harness)
        project_id = uuid.uuid4()
        for call in (
            service.set_documents_enabled(project_id, [], True),
            service.delete_documents(project_id, [uuid.uuid4() for _ in range(101)]),
        ):
            with pytest.raises(KnowledgeError) as error:
                await call
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# HTTP contract for the new routes
# ---------------------------------------------------------------------------

_REQUEST_ID = "knowledge-k1-contract"
_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_BASE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_DOCUMENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_SEGMENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_NOW = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


def _document_view(**overrides: object) -> KnowledgeDocumentView:
    values: dict[str, object] = {
        "id": _DOCUMENT_ID,
        "project_id": _PROJECT_ID,
        "knowledge_base_id": _BASE_ID,
        "name": "季度报告",
        "original_name": "report.pdf",
        "media_type": "application/pdf",
        "size_bytes": 11,
        "status": "ready",
        "enabled": True,
        "version": 1,
        "chunk_size": 1000,
        "chunk_overlap": 100,
        "chunk_separator": "\\n\\n",
        "remove_extra_spaces": False,
        "remove_urls_emails": False,
        "chunking_mode": "general",
        "child_chunk_size": 500,
        "child_chunk_separator": "\\n",
        "segment_count": 1,
        "word_count": 12,
        "hit_count": 0,
        "doc_metadata": {},
        "error_message": None,
        "delete_error": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return KnowledgeDocumentView(**values)  # type: ignore[arg-type]


def _segment_view(**overrides: object) -> KnowledgeSegmentView:
    values: dict[str, object] = {
        "id": _SEGMENT_ID,
        "document_version": 1,
        "position": 1,
        "content": "分段内容",
        "word_count": 4,
        "enabled": True,
        "hit_count": 0,
        "source_position": {"manual": True},
        "created_at": _NOW,
    }
    values.update(overrides)
    return KnowledgeSegmentView(**values)  # type: ignore[arg-type]


class _FakeModule:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.error: KnowledgeError | None = None

    def _record(self, verb: str, payload: Any):  # noqa: ANN401, ANN202
        self.calls.append((verb, payload))
        if self.error is not None:
            raise self.error

    async def rename_document(self, project_id, document_id, name, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("rename", (project_id, document_id, name))
        return _document_view(name=name)

    async def set_documents_enabled(self, project_id, document_ids, enabled, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("batch_status", (project_id, list(document_ids), enabled))
        return [_document_view(id=document_id, enabled=enabled) for document_id in document_ids]

    async def delete_documents(self, project_id, document_ids, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("batch_delete", (project_id, list(document_ids)))
        return [_document_view(id=document_id, status="deleting", version=2) for document_id in document_ids]

    async def create_segment(self, project_id, document_id, create, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("create_segment", (project_id, document_id, create))
        return _segment_view(content=create.content, position=9)

    async def update_segment(self, project_id, segment_id, update, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("update_segment", (project_id, segment_id, update))
        return _segment_view(
            content=update.content if update.content is not None else "分段内容",
            enabled=update.enabled if update.enabled is not None else True,
        )

    async def delete_segment(self, project_id, segment_id, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("delete_segment", (project_id, segment_id))
        return _document_view(segment_count=0, word_count=0)


def _app(module: _FakeModule) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = ProjectContext(
        user_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
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
async def test_http_k1_routes_round_trip() -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        renamed = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}",
            json={"name": "新名字"},
        )
        batch_status = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/batch-status",
            json={"document_ids": [str(_DOCUMENT_ID)], "enabled": False},
        )
        batch_delete = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/batch-delete",
            json={"document_ids": [str(_DOCUMENT_ID)]},
        )
        created = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/segments",
            json={"content": "新增分段"},
        )
        updated = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/segments/{_SEGMENT_ID}",
            json={"enabled": False},
        )
        deleted = await client.delete(f"/api/projects/{_PROJECT_ID}/knowledge/segments/{_SEGMENT_ID}")

    assert renamed.status_code == 200
    assert renamed.json()["item"]["name"] == "新名字"
    assert batch_status.status_code == 200
    assert [item["enabled"] for item in batch_status.json()["items"]] == [False]
    assert batch_delete.status_code == 200
    assert [item["status"] for item in batch_delete.json()["items"]] == ["deleting"]
    assert created.status_code == 200
    assert created.json()["item"]["content"] == "新增分段"
    assert created.json()["item"]["word_count"] == 4
    assert updated.status_code == 200
    assert updated.json()["item"]["enabled"] is False
    assert deleted.status_code == 200
    assert deleted.json()["item"]["segment_count"] == 0

    verbs = [verb for verb, _payload in module.calls]
    assert verbs == ["rename", "batch_status", "batch_delete", "create_segment", "update_segment", "delete_segment"]
    _, (_, document_ids, enabled) = module.calls[1]
    assert document_ids == [_DOCUMENT_ID]
    assert enabled is False


@pytest.mark.asyncio
async def test_http_k1_routes_map_conflict_and_reject_bad_bodies() -> None:
    module = _FakeModule()
    module.error = KnowledgeError(KNOWLEDGE_CONFLICT, "文档内容已更新，请刷新后重试")
    async with _client(_app(module)) as client:
        conflicted = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/segments/{_SEGMENT_ID}",
            json={"content": "编辑"},
        )
        empty_batch = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/batch-status",
            json={"document_ids": [], "enabled": True},
        )
        unknown_field = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}",
            json={"name": "x", "original_name": "y"},
        )

    assert conflicted.status_code == 409
    assert conflicted.json()["detail"]["code"] == KNOWLEDGE_CONFLICT
    assert conflicted.json()["detail"]["request_id"] == _REQUEST_ID
    assert empty_batch.status_code == 422
    assert unknown_field.status_code == 422
